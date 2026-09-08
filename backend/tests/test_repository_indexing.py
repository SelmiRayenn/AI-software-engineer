import hashlib
import json
import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import (
    AgentRun,
    BenchmarkTask,
    GoldPatch,
    IndexedFile,
    IndexedSymbol,
    Repository,
    RepositoryIndex,
)
from app.repository_indexing import IndexLimits, RepositoryIndexService
from app.repository_indexing.errors import IndexLimitError, IndexSafetyError, IndexWorkspaceError
from app.repository_indexing.workspace import IndexWorkspace
from app.sandbox.workspace import SandboxWorkspaceManager, SandboxWorkspaceMetadata

PYTHON_SOURCE = """import os as operating_system
from pathlib import Path

LIMIT = 42

async def calculate(value):
    import math
    return value + LIMIT

class Calculator:
    def method(self):
        return 1

raise RuntimeError("indexing must never execute this module")
"""


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SandboxWorkspaceManager:
    root = tmp_path / "sandboxes"
    monkeypatch.setattr(settings, "sandbox_workspace_root", str(root))
    return SandboxWorkspaceManager(retain_workspaces=True)


@pytest.fixture()
def workspace(manager: SandboxWorkspaceManager) -> SandboxWorkspaceMetadata:
    workspace = manager.create_workspace(prefix="agent-run")
    workspace.repo_path.mkdir()
    return workspace


@pytest.fixture()
def run(db: Session, workspace: SandboxWorkspaceMetadata) -> AgentRun:
    repository = Repository(
        name="index-example", owner="example", url="https://github.com/example/x"
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=1,
        issue_title="Example",
        base_commit="1" * 40,
        status="ready",
        test_commands=[],
        setup_commands=[],
    )
    run = AgentRun(
        benchmark_task=task,
        model_provider="mock",
        model_name="mock-model",
        status="completed",
        workspace_id=workspace.workspace_id,
        workspace_path=str(workspace.repo_path),
    )
    db.add(run)
    db.commit()
    return run


@pytest.fixture()
def service(db: Session, run: AgentRun) -> RepositoryIndexService:
    return RepositoryIndexService(db=db, agent_run_id=run.id)


@pytest.fixture()
def client(db: Session) -> Generator[TestClient, None, None]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client


def write(workspace: SandboxWorkspaceMetadata, path: str, content: str | bytes) -> Path:
    target = workspace.repo_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return target


def test_python_metadata_symbols_and_imports(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
) -> None:
    write(workspace, "src/calculator.py", PYTHON_SOURCE)
    write(workspace, "tests/test_calculator.py", "def test_add():\n    assert True\n")
    write(workspace, "README.md", "# Example\n")
    write(workspace, "pyproject.toml", '[project]\nname = "example"\n')

    index = service.create_index()
    files = {file.file_path: file for file in service.list_files()}
    calculator = files["src/calculator.py"]

    assert index.file_count == 4
    assert calculator.extension == ".py"
    assert calculator.size_bytes == len(PYTHON_SOURCE.encode("utf-8"))
    assert calculator.language == "Python"
    assert calculator.preview == PYTHON_SOURCE
    assert calculator.checksum == hashlib.sha256(PYTHON_SOURCE.encode()).hexdigest()
    assert calculator.imports == [
        "import os as operating_system",
        "from pathlib import Path",
        "import math",
    ]
    assert [(symbol.name, symbol.kind, symbol.line_number) for symbol in calculator.symbols] == [
        ("LIMIT", "variable", 4),
        ("calculate", "function", 6),
        ("Calculator", "class", 10),
    ]
    assert [files[path].file_type for path in files] == ["docs", "config", "source", "test"]
    assert db.scalar(select(func.count()).select_from(IndexedSymbol)) == 4


def test_ignored_and_gold_paths_are_pruned(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
) -> None:
    for directory in [".git", "node_modules", ".venv", "__pycache__", "dist", "build"]:
        write(workspace, f"nested/{directory}/do_not_index.py", "HIDDEN_DATA = True")
    for path in [
        ".gold/solution.py",
        ".benchmark/gold/solution.py",
        "nested/.gold_solution/fix.py",
        "gold_patch.diff",
        "gold_solution.patch",
        "nested/gold_patch.json",
        ".env",
    ]:
        write(workspace, path, "HIDDEN_DATA")
    write(workspace, "src/allowed.py", "answer = 42")

    index = service.create_index()

    assert [file.file_path for file in service.list_files()] == ["src/allowed.py"]
    assert index.skipped_counts["ignored_entries"] == 6
    assert index.skipped_counts["protected_entries"] == 7
    assert service.search_files("HIDDEN_DATA") == []


@pytest.mark.parametrize(
    "data", [b"\x89PNG\r\n\x1a\n", b"hello\x00world", b"\xff\xfe", b"\x01\x02"]
)
def test_binary_files_are_skipped(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    data: bytes,
) -> None:
    write(workspace, "image.py", data)
    index = service.create_index()
    assert index.file_count == 0
    assert index.skipped_counts["binary_files"] == 1


def test_oversized_file_is_skipped(db: Session, run: AgentRun, workspace: SandboxWorkspaceMetadata):
    write(workspace, "large.py", "x" * 51)
    write(workspace, "small.py", "x = 1")
    service = RepositoryIndexService(
        db=db, agent_run_id=run.id, limits=IndexLimits(max_file_bytes=50)
    )
    assert service.create_index().skipped_counts == {"oversized_files": 1}
    assert [file.file_path for file in service.list_files()] == ["small.py"]


def test_file_growth_during_read_is_bounded_and_preserves_previous_index(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = write(workspace, "app.py", "VALUE = 1\n")
    original_checksum = service.create_index().checksum
    identity = (target.stat().st_dev, target.stat().st_ino)
    original_read = os.read
    captured_bytes = 0
    grew = False

    def grow_before_read(fd: int, count: int) -> bytes:
        nonlocal captured_bytes, grew
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity:
            return original_read(fd, count)
        if not grew:
            with target.open("ab") as stream:
                stream.write(b"#" * 5_000)
            grew = True
        data = original_read(fd, count)
        captured_bytes += len(data)
        return data

    limited = RepositoryIndexService(
        db=db,
        agent_run_id=run.id,
        limits=IndexLimits(max_file_bytes=64),
    )
    with monkeypatch.context() as patch:
        patch.setattr(os, "read", grow_before_read)
        with pytest.raises(IndexWorkspaceError, match="changed during indexing"):
            limited.create_index()
    assert grew
    assert captured_bytes == 65
    assert db.scalar(select(RepositoryIndex)).checksum == original_checksum


def test_gold_database_data_is_excluded_but_normal_source_remains_indexable(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
    client: TestClient,
) -> None:
    write(workspace, "src/calculator.py", PYTHON_SOURCE)
    db.add(
        GoldPatch(
            benchmark_task_id=run.benchmark_task_id,
            changed_files=["src/calculator.py", "hidden_fix_only.py"],
            patch_text="HIDDEN_DATABASE_SOLUTION",
            test_files=["hidden_gold_test.py"],
        )
    )
    db.commit()

    service.create_index()

    assert [file.file_path for file in service.list_files()] == ["src/calculator.py"]
    for secret in ["HIDDEN_DATABASE_SOLUTION", "hidden_fix_only.py", "hidden_gold_test.py"]:
        assert service.search_files(secret) == []
        assert secret not in client.get(f"/agent-runs/{run.id}/index/files").text
    assert db.scalar(select(IndexedFile.content)) == PYTHON_SOURCE


def test_syntax_error_and_python_encoding_remain_searchable(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
) -> None:
    write(workspace, "broken.py", "def incomplete(:")
    write(workspace, "encoded.py", b"# coding: latin-1\nNAME = 'caf\xe9'\n")
    service.create_index()
    broken, encoded = service.list_files()
    assert broken.python_parse_error is True
    assert broken.symbols == []
    assert encoded.python_parse_error is False
    assert encoded.symbols[0].name == "NAME"
    assert service.search_files("incomplete", field="text")[0].file_path == "broken.py"


@pytest.mark.parametrize(
    "field,query", [("path", "CALCULATOR.PY"), ("text", "needle"), ("symbol", "calculate")]
)
def test_search_path_text_and_symbol(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    field: str,
    query: str,
) -> None:
    write(
        workspace,
        "src/calculator.py",
        "# " + "x" * 600 + "\n# needle\ndef calculate():\n    pass\n",
    )
    write(workspace, "other.py", "irrelevant = True")
    service.create_index()
    assert [file.file_path for file in service.search_files(query, field=field)] == [
        "src/calculator.py"
    ]
    assert [file.file_path for file in service.search_files(query)] == ["src/calculator.py"]
    assert len(service.search_files(query)[0].preview) == 500


def test_search_escapes_sql_wildcards_and_is_run_scoped(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
) -> None:
    write(workspace, "literal_percent.py", "# 100%\n")
    write(workspace, "plain.py", "plain = True")
    service.create_index()
    assert [file.file_path for file in service.search_files("%", field="text")] == [
        "literal_percent.py"
    ]
    assert service.search_files("' OR 1=1 --") == []
    other_run = AgentRun(
        benchmark_task_id=run.benchmark_task_id,
        model_provider="mock",
        model_name="mock-model",
        status="completed",
        workspace_id=run.workspace_id,
        workspace_path=run.workspace_path,
    )
    db.add(other_run)
    db.commit()
    other_service = RepositoryIndexService(db=db, agent_run_id=other_run.id)
    write(workspace, "private.py", "other_run_only = True")
    other_service.create_index()
    assert service.search_files("other_run_only") == []
    assert len(other_service.search_files("other_run_only")) == 1


def test_reindex_is_deterministic_replaces_stale_files_and_cascades_symbols(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
) -> None:
    write(workspace, "a.py", "def first():\n    pass")
    write(workspace, "b.py", "def second():\n    pass")
    first = service.create_index()
    first_id, checksum = first.id, first.checksum
    second = service.create_index()
    assert (second.id, second.checksum) == (first_id, checksum)
    assert db.scalar(select(func.count()).select_from(RepositoryIndex)) == 1
    assert db.scalar(select(func.count()).select_from(IndexedSymbol)) == 2
    (workspace.repo_path / "b.py").unlink()
    write(workspace, "a.py", "def renamed():\n    pass")
    third = service.create_index()
    assert third.checksum != checksum
    assert [file.file_path for file in service.list_files()] == ["a.py"]
    assert db.scalar(select(func.count()).select_from(IndexedSymbol)) == 1
    assert service.search_files("second", field="symbol") == []


@pytest.mark.parametrize(
    "limits",
    [IndexLimits(max_files=1), IndexLimits(max_total_bytes=15), IndexLimits(max_entries=1)],
)
def test_limit_failure_preserves_previous_index(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
    limits: IndexLimits,
) -> None:
    write(workspace, "a.py", "message = 'original'")
    original = service.create_index().checksum
    write(workspace, "b.py", "message = 'new'")
    limited = RepositoryIndexService(db=db, agent_run_id=run.id, limits=limits)
    with pytest.raises(IndexLimitError):
        limited.create_index()
    assert db.scalar(select(RepositoryIndex)).checksum == original
    assert [file.file_path for file in service.list_files()] == ["a.py"]


@pytest.mark.parametrize(
    "path", ["../outside.py", "/etc/passwd", "C:/outside.py", "..\\outside.py", ".gold/solution.py"]
)
def test_direct_reader_rejects_unsafe_paths(
    workspace: SandboxWorkspaceMetadata,
    manager: SandboxWorkspaceManager,
    path: str,
) -> None:
    reader = IndexWorkspace(
        root=manager.workspace_root,
        workspace_id=workspace.workspace_id,
        workspace_path=str(workspace.repo_path),
    )
    with pytest.raises(IndexSafetyError):
        reader.read_file(path, 100)


def test_outside_and_traversal_workspace_paths_rejected(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
    tmp_path: Path,
) -> None:
    for path in [str(tmp_path), str(workspace.repo_path / ".." / "repo")]:
        run.workspace_path = path
        db.commit()
        with pytest.raises(IndexSafetyError):
            service.create_index()
    assert db.scalar(select(RepositoryIndex)) is None


def test_other_workspace_and_mismatched_metadata_rejected(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    run: AgentRun,
    manager: SandboxWorkspaceManager,
) -> None:
    other = manager.create_workspace(prefix="agent-run")
    other.repo_path.mkdir()
    run.workspace_path = str(other.repo_path)
    db.commit()
    with pytest.raises(IndexSafetyError):
        service.create_index()
    run.workspace_path = str(workspace.repo_path)
    db.commit()
    metadata = json.loads(workspace.metadata_path.read_text())
    metadata["workspace_id"] = other.workspace_id
    workspace.metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(IndexSafetyError):
        service.create_index()


def test_links_do_not_expose_outside_or_hidden_gold_data(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SECRET")
    gold = write(workspace, ".gold/solution.py", "GOLD_SECRET")
    try:
        (workspace.repo_path / "outside.txt").symlink_to(outside)
        (workspace.repo_path / "alias.py").symlink_to(gold)
        (workspace.repo_path / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating symbolic links")
    index = service.create_index()
    assert index.file_count == 0
    assert index.skipped_counts["unsafe_entries"] == 3


def test_hardlinks_are_skipped(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SECRET")
    os.link(outside, workspace.repo_path / "alias.txt")
    assert service.create_index().file_count == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_directory_junction_is_not_followed(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("OUTSIDE_SECRET")
    junction = workspace.repo_path / "external"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        assert service.create_index().skipped_counts["unsafe_entries"] == 1
        assert service.list_files() == []
        assert (outside / "secret.py").read_text() == "OUTSIDE_SECRET"
    finally:
        junction.rmdir()


def test_failed_replacement_rolls_back_deleted_rows(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(workspace, "app.py", "def original():\n    pass")
    original_checksum = service.create_index().checksum
    write(workspace, "app.py", "def updated():\n    pass")

    def fail_commit():
        raise RuntimeError("simulated database write failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="database write failure"):
            service.create_index()
    assert db.scalar(select(RepositoryIndex)).checksum == original_checksum
    assert len(service.search_files("original", field="symbol")) == 1
    assert service.search_files("updated", field="symbol") == []


def test_index_survives_workspace_cleanup_and_run_deletion_cascades(
    service: RepositoryIndexService,
    workspace: SandboxWorkspaceMetadata,
    manager: SandboxWorkspaceManager,
    db: Session,
    run: AgentRun,
) -> None:
    write(workspace, "app.py", "def calculate():\n    pass")
    service.create_index()
    manager.cleanup_path(workspace.workspace_path)
    assert len(service.search_files("calculate")) == 1
    with pytest.raises(IndexWorkspaceError):
        service.create_index()
    assert len(service.list_files()) == 1
    db.delete(run)
    db.commit()
    for model in [RepositoryIndex, IndexedFile, IndexedSymbol]:
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_index_api_round_trip(
    client: TestClient, run: AgentRun, workspace: SandboxWorkspaceMetadata
):
    write(workspace, "calculator.py", PYTHON_SOURCE)
    write(workspace, ".gold/patch.txt", "HIDDEN_GOLD_CONTENT")
    response = client.post(f"/agent-runs/{run.id}/index")
    assert response.status_code == 200
    assert response.json()["file_count"] == 1
    assert "workspace_path" not in response.json()
    for route in ["files", "search?q=calculate&field=symbol"]:
        response = client.get(f"/agent-runs/{run.id}/index/{route}")
        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["file_path"] == "calculator.py"
        assert "content" not in payload[0]
        assert "HIDDEN_GOLD_CONTENT" not in response.text
    assert client.get(f"/agent-runs/{run.id}/index/files?offset=1").json() == []


def test_index_api_missing_and_invalid_requests(client: TestClient, run: AgentRun):
    for route in ["", "/files", "/search?q=anything"]:
        method = client.post if route == "" else client.get
        assert method(f"/agent-runs/{uuid4()}/index{route}").status_code == 404
    assert client.get(f"/agent-runs/{run.id}/index/files").status_code == 404
    for query in ["q=", "q=%20", "q=x&field=unknown", "q=x&limit=201", "q=x&offset=-1"]:
        assert client.get(f"/agent-runs/{run.id}/index/search?{query}").status_code == 422


def test_index_api_missing_workspace(client: TestClient, run: AgentRun, db: Session):
    run.workspace_path = None
    db.commit()
    response = client.post(f"/agent-runs/{run.id}/index")
    assert response.status_code == 409
    assert "prepared sandbox workspace" in response.json()["detail"]
