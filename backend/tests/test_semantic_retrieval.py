import hashlib
import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.tools import AgentWorkspaceTools
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.embeddings import EmbeddingsError, EmbeddingUsage, MockEmbeddingsProvider
from app.main import create_app
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    ChunkEmbedding,
    GoldPatch,
    IndexedChunk,
    Repository,
    RepositoryIndex,
)
from app.repository_indexing import RepositoryIndexService, RepositoryRetrievalService
from app.repository_indexing.chunking import chunk_file, embedding_text
from app.repository_indexing.extraction import extract_file
from app.repository_indexing.semantic import RepositoryEmbeddingsService
from app.sandbox import SandboxWorkspaceManager


class ConceptProvider(MockEmbeddingsProvider):
    """Known vectors let tests distinguish synonym matches from lexical overlap."""

    def __init__(self) -> None:
        super().__init__(app_settings=settings)
        self.inputs: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.inputs.extend(texts)
        self.last_usage = EmbeddingUsage(0, 0.0)
        return [
            [1.0, 0.0, 0.0] if "currency" in text or "funds" in text else [0.0, 1.0, 0.0]
            for text in texts
        ]


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openai.OpenAI", Mock(side_effect=AssertionError("Unexpected network call"))
    )
    for name, value in {
        "enable_real_embeddings": False,
        "embeddings_provider": "mock",
        "embedding_auto_build": False,
        "embedding_vector_dimensions": 3,
        "embedding_batch_size": 2,
        "embedding_chunk_size_chars": 1000,
        "embedding_chunk_overlap_chars": 100,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture()
def indexed_run(db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRun:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path / "sandboxes", retain_workspaces=True)
    monkeypatch.setattr(settings, "sandbox_workspace_root", str(manager.workspace_root))
    workspace = manager.create_workspace(prefix="agent-run")
    workspace.repo_path.mkdir()
    (workspace.repo_path / "wallet.py").write_text(
        "def withdraw():\n    return 'currency'\n", encoding="utf-8"
    )
    (workspace.repo_path / "weather.py").write_text(
        "def forecast():\n    return 'rain'\n", encoding="utf-8"
    )
    task = BenchmarkTask(
        repository=Repository(name="tiny", owner="example", url="https://github.com/example/tiny"),
        issue_number=1,
        issue_title="Fix withdraw",
        base_commit="1" * 40,
        status="ready",
        setup_commands=[],
        test_commands=[],
    )
    run = AgentRun(
        benchmark_task=task,
        model_provider="mock",
        model_name="mock-model",
        status="running",
        workspace_id=workspace.workspace_id,
        workspace_path=str(workspace.repo_path),
    )
    db.add(run)
    db.commit()
    RepositoryIndexService(db=db, agent_run_id=run.id).create_index()
    return run


def build(db: Session, run: AgentRun, provider=None):
    return RepositoryEmbeddingsService(
        db=db,
        agent_run_id=run.id,
        provider=provider or ConceptProvider(),
    ).build()


def test_chunks_preserve_content_lines_symbols_and_bound_long_lines(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_chunk_size_chars", 128)
    monkeypatch.setattr(settings, "embedding_chunk_overlap_chars", 20)
    content = "def transfer():\n    value = '" + "x" * 400 + "'\n    return value\n"
    file = extract_file("src/wallet.py", content.encode())
    chunks = chunk_file(file)
    assert len(chunks) > 1
    reconstructed = chunks[0].content + "".join(chunk.content[20:] for chunk in chunks[1:])
    assert reconstructed == content
    assert all(len(chunk.content) <= 128 for chunk in chunks)
    assert all("transfer" in chunk.symbol_names for chunk in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 3
    assert "File: src/wallet.py" in embedding_text(file, chunks[0])
    assert all(
        chunk.checksum == hashlib.sha256(chunk.content.encode()).hexdigest() for chunk in chunks
    )


def test_embedding_storage_is_repeatable_and_records_usage(db, indexed_run) -> None:
    result = build(db, indexed_run)
    assert result.chunk_count == 2
    assert result.dimensions == 3
    assert result.input_tokens == 0
    assert result.estimated_cost == 0
    assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) == 2
    first_hashes = list(
        db.scalars(select(ChunkEmbedding.input_checksum).order_by(ChunkEmbedding.input_checksum))
    )
    build(db, indexed_run)
    assert db.scalar(select(func.count()).select_from(IndexedChunk)) == 2
    assert first_hashes == list(
        db.scalars(select(ChunkEmbedding.input_checksum).order_by(ChunkEmbedding.input_checksum))
    )
    usage = db.scalar(select(AgentEvent).where(AgentEvent.event_type == "embedding_call_completed"))
    assert usage.payload_json["purpose"] == "build"
    assert "currency" not in json.dumps(usage.payload_json)


def test_semantic_synonym_match_and_hybrid_scoring(db, indexed_run) -> None:
    provider = ConceptProvider()
    build(db, indexed_run, provider)
    service = RepositoryRetrievalService(
        db=db, agent_run_id=indexed_run.id, embeddings_provider=provider
    )
    assert service.retrieve("funds").files == []
    semantic = service.retrieve("funds", semantic=True)
    assert semantic.retrieval_mode == "hybrid"
    assert [file.file_path for file in semantic.files] == ["wallet.py"]
    assert semantic.files[0].semantic_score == 1.0
    assert semantic.files[0].lexical_score == 0
    assert semantic.files[0].matched_symbols == ["withdraw"]
    assert "currency" in semantic.files[0].matched_snippets[0].text
    lexical = service.retrieve("currency")
    hybrid = service.retrieve("currency", semantic=True, limit=1)
    assert hybrid.files[0].relevance_score == lexical.files[0].relevance_score + 10


def test_lexical_default_and_missing_embeddings_do_not_call_provider(db, indexed_run) -> None:
    provider = ConceptProvider()
    service = RepositoryRetrievalService(
        db=db, agent_run_id=indexed_run.id, embeddings_provider=provider
    )
    lexical = service.retrieve("wallet")
    fallback = service.retrieve("wallet", semantic=True)
    assert fallback.files == lexical.files
    assert fallback.fallback_reason == "embeddings_unavailable"
    assert provider.inputs == []
    build(db, indexed_run, provider)
    provider.inputs.clear()
    service.retrieve("wallet")
    assert provider.inputs == []


@pytest.mark.parametrize("change", ["model", "dimensions", "checksum", "bad_vector"])
def test_incompatible_embeddings_fall_back_without_query_call(db, indexed_run, change) -> None:
    provider = ConceptProvider()
    build(db, indexed_run, provider)
    for row in db.scalars(select(ChunkEmbedding)):
        if change == "model":
            row.model_name = "another-model"
        elif change == "dimensions":
            row.dimensions = 2
        elif change == "checksum":
            row.input_checksum = "0" * 64
        else:
            row.vector = [0, 0, 0]
    db.commit()
    provider.inputs.clear()
    result = RepositoryRetrievalService(
        db=db,
        agent_run_id=indexed_run.id,
        embeddings_provider=provider,
    ).retrieve("wallet", semantic=True)
    assert result.retrieval_mode == "lexical"
    assert result.files[0].file_path == "wallet.py"
    assert provider.inputs == []


def test_disabled_real_provider_and_query_failure_fall_back(db, indexed_run, monkeypatch) -> None:
    build(db, indexed_run)
    monkeypatch.setattr(settings, "embeddings_provider", "openai")
    result = RepositoryRetrievalService(db=db, agent_run_id=indexed_run.id).retrieve(
        "wallet", semantic=True
    )
    assert result.fallback_reason == "embedding_provider_unavailable"
    provider = ConceptProvider()
    provider.embed_texts = Mock(side_effect=EmbeddingsError("unavailable"))
    result = RepositoryRetrievalService(
        db=db,
        agent_run_id=indexed_run.id,
        embeddings_provider=provider,
    ).retrieve("wallet", semantic=True)
    assert result.retrieval_mode == "lexical"
    assert result.files[0].file_path == "wallet.py"


def test_failed_build_keeps_previous_embeddings(db, indexed_run, monkeypatch) -> None:
    build(db, indexed_run)
    original = set(db.scalars(select(ChunkEmbedding.id)))
    provider = ConceptProvider()
    provider.embed_texts = Mock(side_effect=[[[1, 0, 0]], EmbeddingsError("second batch failed")])
    monkeypatch.setattr(settings, "embedding_batch_size", 1)
    with pytest.raises(EmbeddingsError):
        build(db, indexed_run, provider)
    assert set(db.scalars(select(ChunkEmbedding.id))) == original


def test_gold_unsafe_binary_and_oversized_files_never_embedded(db, indexed_run) -> None:
    provider = ConceptProvider()
    index = db.scalar(select(RepositoryIndex))
    for path in [".gold/solution.py", "../outside.py", ".env", "dist/output.py"]:
        file = extract_file("placeholder.py", b"PRIVATE_SENTINEL")
        file.file_path = path
        index.files.append(file)
    binary = extract_file("image.txt", b"placeholder")
    binary.content = "BINARY_SENTINEL\x00"
    oversized = extract_file("large.py", b"placeholder")
    oversized.content = "OVERSIZED_SENTINEL" + "x" * 256_000
    index.files.extend([binary, oversized])
    db.add(
        GoldPatch(
            benchmark_task_id=indexed_run.benchmark_task_id,
            changed_files=["wallet.py"],
            patch_text="GOLD_SENTINEL",
            test_files=[],
        )
    )
    db.commit()
    result = build(db, indexed_run, provider)
    assert result.chunk_count == 2
    assert all("SENTINEL" not in text for text in provider.inputs)
    assert any("wallet.py" in text for text in provider.inputs)


def test_reindex_removes_obsolete_embeddings(db, indexed_run) -> None:
    build(db, indexed_run)
    RepositoryIndexService(db=db, agent_run_id=indexed_run.id).create_index()
    assert db.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0
    assert db.scalar(select(func.count()).select_from(IndexedChunk)) == 0


def test_other_run_embeddings_cannot_be_retrieved(db, indexed_run) -> None:
    build(db, indexed_run)
    other = AgentRun(
        benchmark_task_id=indexed_run.benchmark_task_id,
        model_provider="mock",
        model_name="mock",
        status="running",
        workspace_id=indexed_run.workspace_id,
        workspace_path=indexed_run.workspace_path,
    )
    db.add(other)
    db.commit()
    RepositoryIndexService(db=db, agent_run_id=other.id).create_index()
    provider = ConceptProvider()
    result = RepositoryRetrievalService(
        db=db, agent_run_id=other.id, embeddings_provider=provider
    ).retrieve(
        "funds",
        semantic=True,
    )
    assert result.files == []
    assert result.fallback_reason == "embeddings_unavailable"
    assert provider.inputs == []


def test_semantic_tool_and_api_use_index_and_log_mode(db, indexed_run, monkeypatch) -> None:
    provider = ConceptProvider()
    monkeypatch.setattr(
        "app.repository_indexing.semantic.create_embeddings_provider", lambda **_: provider
    )
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.post(f"/agent-runs/{indexed_run.id}/index/embeddings")
        assert response.status_code == 200
        assert response.json()["chunk_count"] == 2
        response = client.get(
            f"/agent-runs/{indexed_run.id}/index/search", params={"q": "funds", "semantic": True}
        )
        assert response.status_code == 200
        assert [file["file_path"] for file in response.json()] == ["wallet.py"]
        assert (
            client.get(f"/agent-runs/{indexed_run.id}/index/search", params={"q": "funds"}).json()
            == []
        )
    tools = AgentWorkspaceTools(
        db=db, agent_run_id=indexed_run.id, workspace_path=indexed_run.workspace_path
    )
    result = tools.retrieve_relevant_files("funds", semantic=True)
    assert result.files[0].file_path == "wallet.py"
    event = db.scalar(select(AgentEvent).where(AgentEvent.event_type == "agent_tool_call"))
    assert event.payload_json["retrieval_mode"] == "hybrid"
    assert event.payload_json["input"]["semantic"] is True


def test_embedding_api_reports_disabled_configuration(db, indexed_run, monkeypatch) -> None:
    monkeypatch.setattr(settings, "embeddings_provider", "openai")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.post(f"/agent-runs/{indexed_run.id}/index/embeddings")
    assert response.status_code == 409
    assert "ENABLE_REAL_EMBEDDINGS" in response.json()["detail"]


def test_build_batches_respect_total_utf8_request_limit(db, indexed_run, monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_chunk_size_chars", 1500)
    monkeypatch.setattr(settings, "embedding_batch_size", 128)
    # Multibyte text tests the byte budget independently of the text-count limit.
    index = db.scalar(select(RepositoryIndex))
    index.files.append(extract_file("unicode.py", ("# " + chr(0x1F600) * 40_000).encode()))
    db.commit()
    provider = ConceptProvider()
    original = provider.embed_texts
    provider.embed_texts = Mock(side_effect=original)
    build(db, indexed_run, provider)
    assert provider.embed_texts.call_count >= 2
    for call in provider.embed_texts.call_args_list:
        texts = call.args[0]
        assert sum(len(text.encode()) for text in texts) <= 128_000
        assert all(len(text.encode()) <= 8000 for text in texts)
