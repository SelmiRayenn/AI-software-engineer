from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
from app.db.base import Base
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkTask,
    IndexedFile,
    IndexedSymbol,
    Repository,
    RepositoryIndex,
)


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture()
def run(db: Session) -> AgentRun:
    repository = Repository(
        name="calculator",
        owner="example",
        url="https://github.com/example/calculator",
    )
    task = BenchmarkTask(
        repository=repository,
        issue_number=1,
        issue_title="Handle calculator errors",
        base_commit="1" * 40,
        setup_commands=[],
        test_commands=[],
        status="ready",
    )
    run = AgentRun(
        benchmark_task=task,
        model_provider="mock",
        model_name="mock-model",
        status="running",
    )
    db.add(run)
    db.flush()
    add_index(db, run)
    db.commit()
    return run


@pytest.fixture()
def tools(db: Session, run: AgentRun, workspace: Path) -> AgentWorkspaceTools:
    return AgentWorkspaceTools(
        db=db,
        agent_run_id=run.id,
        workspace_path=workspace,
    )


def test_retrieval_returns_indexed_file_metadata_and_matches(
    tools: AgentWorkspaceTools,
) -> None:
    result = tools.retrieve_relevant_files("Calculator error")

    assert result.query == "Calculator error"
    assert result.files[0].file_path == "src/calculator.py"
    assert result.files[0].language == "Python"
    assert result.files[0].file_kind == "source"
    assert result.files[0].matched_symbols == ["Calculator"]
    assert result.files[0].matched_snippets[0].line_number == 1
    assert result.files[0].relevance_score > 0
    assert result.files[0].size_bytes == len(
        b"class Calculator:\n    raise CalculationError('bad input')\n"
    )


def test_retrieval_score_ordering_uses_symbol_and_file_kind_boosts(
    tools: AgentWorkspaceTools,
) -> None:
    source_result = tools.retrieve_relevant_files("calculator module error")
    test_result = tools.retrieve_relevant_files("test failure")

    assert source_result.files[0].file_path == "src/calculator.py"
    assert test_result.files[0].file_path == "tests/test_calculator.py"
    assert source_result.files[0].relevance_score >= source_result.files[1].relevance_score
    assert test_result.files[0].relevance_score >= test_result.files[1].relevance_score


def test_retrieval_enforces_result_limit(tools: AgentWorkspaceTools) -> None:
    result = tools.retrieve_relevant_files("calculator", limit=1)

    assert len(result.files) == 1


@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_retrieval_rejects_invalid_queries(
    tools: AgentWorkspaceTools,
    query: str,
) -> None:
    with pytest.raises(ToolSafetyError):
        tools.retrieve_relevant_files(query)


def test_retrieval_logs_agent_event(
    db: Session,
    run: AgentRun,
    tools: AgentWorkspaceTools,
) -> None:
    tools.retrieve_relevant_files("calculator", limit=2)

    event = db.scalar(
        select(AgentEvent).where(
            AgentEvent.agent_run_id == run.id,
            AgentEvent.event_type == "agent_tool_call",
        )
    )
    assert event is not None
    assert event.payload_json["tool_name"] == "retrieve_relevant_files"
    assert event.payload_json["input"] == {"query": "calculator", "limit": 2, "semantic": False}
    assert event.payload_json["success"] is True
    assert event.payload_json["result_count"] == 2
    assert event.payload_json["files_read"] == [
        "src/calculator.py",
        "tests/test_calculator.py",
    ]


def test_retrieval_is_scoped_to_run_and_excludes_protected_paths(
    db: Session,
    run: AgentRun,
    tools: AgentWorkspaceTools,
) -> None:
    current_index = run.repository_index
    current_index.files.append(
        indexed_file(
            ".gold/solution.py",
            "gold calculator fix",
            file_type="source",
        )
    )
    current_index.files.append(indexed_file("../outside.py", "calculator outside workspace"))
    other_run = AgentRun(
        benchmark_task=run.benchmark_task,
        model_provider="mock",
        model_name="other-model",
        status="running",
    )
    db.add(other_run)
    db.flush()
    other_index = RepositoryIndex(
        agent_run_id=other_run.id,
        workspace_id="other-workspace",
        index_version=1,
        indexed_at=datetime.now(UTC),
        file_count=1,
        total_size_bytes=20,
        checksum="f" * 64,
        skipped_counts={},
    )
    other_index.files.append(
        indexed_file("private/ultimate_calculator.py", "calculator calculator")
    )
    db.add(other_index)
    db.commit()

    result = tools.retrieve_relevant_files("calculator", limit=50)

    paths = [file.file_path for file in result.files]
    assert ".gold/solution.py" not in paths
    assert "../outside.py" not in paths
    assert "private/ultimate_calculator.py" not in paths


def add_index(db: Session, run: AgentRun) -> None:
    index = RepositoryIndex(
        agent_run_id=run.id,
        workspace_id="test-workspace",
        index_version=1,
        indexed_at=datetime.now(UTC),
        file_count=3,
        total_size_bytes=200,
        checksum="a" * 64,
        skipped_counts={},
    )
    source = indexed_file(
        "src/calculator.py",
        "class Calculator:\n    raise CalculationError('bad input')\n",
        file_type="source",
    )
    source.symbols.append(
        IndexedSymbol(
            name="Calculator",
            kind="class",
            line_number=1,
            end_line_number=2,
        )
    )
    index.files.extend(
        [
            source,
            indexed_file(
                "tests/test_calculator.py",
                "def test_calculator_error():\n    assert calculator_raises()\n",
                file_type="test",
            ),
            indexed_file(
                "docs/calculator.md",
                "Calculator usage and test failure behavior.\n",
                file_type="docs",
                language="Markdown",
            ),
        ]
    )
    db.add(index)


def indexed_file(
    path: str,
    content: str,
    *,
    file_type: str = "source",
    language: str = "Python",
    size_bytes: int | None = None,
) -> IndexedFile:
    return IndexedFile(
        file_path=path,
        extension=Path(path).suffix,
        size_bytes=size_bytes if size_bytes is not None else len(content.encode("utf-8")),
        language=language,
        file_type=file_type,
        preview=content[:500],
        content=content,
        checksum="b" * 64,
        imports=[],
        python_parse_error=False,
    )
