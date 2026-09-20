import base64
from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.benchmark_task_ingestion import get_github_service
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.github import GitHubService
from app.main import app
from app.models import BenchmarkTask, GoldPatch, HiddenEvalTest, Repository

engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db() -> Generator[Session, None, None]:
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_github_service] = mock_github_service

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_create_benchmark_task_from_github_stores_gold_and_hides_solution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = client.post("/benchmark-tasks/from-github", json=request_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["issue_title"] == "Division by zero is unclear"
    assert payload["repository"]["owner"] == "example"
    assert payload["base_commit"] == "requested-base-sha"
    assert "patch_text" not in payload
    assert "changed_files" not in payload
    assert "fix_commit" not in payload
    assert "linked_pr_url" not in payload

    with TestingSessionLocal() as db:
        task = db.scalars(select(BenchmarkTask)).one()
        gold_patch = db.scalars(select(GoldPatch)).one()

        assert task.setup_commands == ["python -m pip install -e ."]
        assert task.test_commands == ["pytest"]
        assert task.notes == "Good starter benchmark."
        assert task.pull_request_number == 43
        assert task.fix_commit == "merge-sha"
        assert task.linked_pr_url == "https://github.com/example/calculator/pull/43"
        assert task.issue_comments[0]["body"] == "I can reproduce this."
        assert gold_patch.benchmark_task_id == task.id
        assert gold_patch.changed_files == ["calculator/core.py", "tests/test_core.py"]
        assert gold_patch.test_files == ["tests/test_core.py"]
        assert "diff --git a/calculator/core.py b/calculator/core.py" in gold_patch.patch_text

    normal_task_response = client.get("/api/v1/benchmark-tasks")
    assert normal_task_response.status_code == 200
    normal_task = normal_task_response.json()[0]
    assert "patch_text" not in normal_task
    assert "changed_files" not in normal_task
    assert "fix_commit" not in normal_task
    assert "linked_pr_url" not in normal_task

    monkeypatch.setattr(settings, "trusted_operator_token", "test-operator")
    gold_response = client.get(
        f"/evaluation/benchmark-tasks/{payload['id']}/gold-patch",
        headers={"X-Operator-Token": "test-operator"},
    )
    assert gold_response.status_code == 200
    assert gold_response.json()["patch_text"].startswith("diff --git")


def test_repository_is_reused_instead_of_duplicated(client: TestClient) -> None:
    first = client.post("/benchmark-tasks/from-github", json=request_payload())
    second = client.post(
        "/benchmark-tasks/from-github",
        json={
            **request_payload(),
            "issue_number": 43,
            "pull_request_number": 44,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200

    with TestingSessionLocal() as db:
        repository_count = db.scalar(select(func.count()).select_from(Repository))
        task_count = db.scalar(select(func.count()).select_from(BenchmarkTask))

    assert repository_count == 1
    assert task_count == 2


def test_hidden_tests_are_created_only_when_explicitly_enabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without_flag = client.post("/benchmark-tasks/from-github", json=request_payload())
    assert without_flag.status_code == 200
    with TestingSessionLocal() as db:
        assert list(db.scalars(select(HiddenEvalTest))) == []

    monkeypatch.setattr(settings, "trusted_operator_token", "operator-token")
    response = client.post(
        "/benchmark-tasks/from-github",
        headers={"X-Operator-Token": "operator-token"},
        json={
            **request_payload(),
            "issue_number": 43,
            "pull_request_number": 44,
            "create_hidden_tests_from_pr_tests": True,
        },
    )

    assert response.status_code == 200
    assert "hidden" not in response.text.lower()
    with TestingSessionLocal() as db:
        hidden_test = db.scalars(select(HiddenEvalTest)).one()
        assert hidden_test.name == "PR #44: tests/test_core.py"
        assert hidden_test.commands == [
            "python -m pytest -q .benchmark-hidden-eval/tests/test_core.py"
        ]
        assert hidden_test.files_payload == {
            "tests/test_core.py": "def test_divide_by_zero():\n    assert True\n"
        }

    normal_task_response = client.get("/api/v1/benchmark-tasks")
    assert normal_task_response.status_code == 200
    assert "test_divide_by_zero" not in normal_task_response.text
    assert "hidden_eval" not in normal_task_response.text


def test_hidden_test_creation_flag_requires_operator_access(client: TestClient) -> None:
    response = client.post(
        "/benchmark-tasks/from-github",
        json={**request_payload(), "create_hidden_tests_from_pr_tests": True},
    )

    assert response.status_code in {403, 503}


def mock_github_service() -> GitHubService:
    return GitHubService(
        api_base_url="https://api.github.test",
        client=httpx.Client(
            base_url="https://api.github.test",
            headers=service_headers_without_token(),
            transport=httpx.MockTransport(handler),
        ),
    )


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    accept = request.headers.get("accept", "")

    if path == "/repos/example/calculator":
        return json_response(repository_payload())
    if path in {"/repos/example/calculator/issues/42", "/repos/example/calculator/issues/43"}:
        return json_response(issue_payload(number=int(path.rsplit("/", 1)[-1])))
    if path in {
        "/repos/example/calculator/issues/42/comments",
        "/repos/example/calculator/issues/43/comments",
    }:
        return json_response([comment_payload()])
    if path in {"/repos/example/calculator/pulls/43", "/repos/example/calculator/pulls/44"}:
        if accept == "application/vnd.github.v3.diff":
            return text_response(pull_request_diff())
        return json_response(pull_request_payload(number=int(path.rsplit("/", 1)[-1])))
    if path in {
        "/repos/example/calculator/pulls/43/files",
        "/repos/example/calculator/pulls/44/files",
    }:
        return json_response(files_payload())
    if path in {
        "/repos/example/calculator/pulls/43/commits",
        "/repos/example/calculator/pulls/44/commits",
    }:
        return json_response(commits_payload())
    if path == "/repos/example/calculator/contents/tests/test_core.py":
        assert request.url.params["ref"] == "head-sha"
        content = base64.b64encode(b"def test_divide_by_zero():\n    assert True\n").decode("ascii")
        return json_response({"type": "file", "encoding": "base64", "content": content})

    return json_response({"message": "not found"}, status_code=404)


def request_payload() -> dict[str, object]:
    return {
        "repository_url": "https://github.com/example/calculator",
        "issue_number": 42,
        "pull_request_number": 43,
        "base_commit": "requested-base-sha",
        "setup_commands": ["python -m pip install -e ."],
        "test_commands": ["pytest"],
        "notes": "Good starter benchmark.",
    }


def service_headers_without_token() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-benchmark-platform",
    }


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload)


def text_response(payload: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, text=payload)


def repository_payload() -> dict[str, object]:
    return {
        "name": "calculator",
        "full_name": "example/calculator",
        "owner": {"login": "example"},
        "clone_url": "https://github.com/example/calculator.git",
        "html_url": "https://github.com/example/calculator",
        "default_branch": "main",
        "language": "Python",
        "description": "Tiny calculator",
        "private": False,
    }


def issue_payload(number: int = 42) -> dict[str, object]:
    return {
        "number": number,
        "title": "Division by zero is unclear",
        "body": "Division by zero should produce a clear error.",
        "state": "closed",
        "html_url": f"https://github.com/example/calculator/issues/{number}",
        "user": {"login": "reporter", "html_url": "https://github.com/reporter"},
        "labels": [{"name": "bug"}],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": "2026-01-03T00:00:00Z",
    }


def comment_payload() -> dict[str, object]:
    return {
        "id": 1001,
        "body": "I can reproduce this.",
        "html_url": "https://github.com/example/calculator/issues/42#issuecomment-1001",
        "user": {"login": "maintainer", "html_url": "https://github.com/maintainer"},
        "created_at": "2026-01-01T01:00:00Z",
        "updated_at": "2026-01-01T01:00:00Z",
    }


def pull_request_payload(number: int = 43) -> dict[str, object]:
    return {
        "number": number,
        "title": "Handle division by zero",
        "body": "Fixes #42",
        "state": "closed",
        "html_url": f"https://github.com/example/calculator/pull/{number}",
        "merged": True,
        "merge_commit_sha": "merge-sha",
        "base": {"ref": "main", "sha": "base-sha"},
        "head": {"ref": "fix-division", "sha": "head-sha"},
        "user": {"login": "contributor", "html_url": "https://github.com/contributor"},
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T01:00:00Z",
        "closed_at": "2026-01-02T02:00:00Z",
        "merged_at": "2026-01-02T02:00:00Z",
    }


def files_payload() -> list[dict[str, object]]:
    return [
        {
            "filename": "calculator/core.py",
            "status": "modified",
            "additions": 2,
            "deletions": 0,
            "changes": 2,
            "patch": "@@ -1,2 +1,4 @@",
        },
        {
            "filename": "tests/test_core.py",
            "status": "modified",
            "additions": 5,
            "deletions": 0,
            "changes": 5,
            "patch": "@@ -1,1 +1,6 @@",
        },
    ]


def commits_payload() -> list[dict[str, object]]:
    return [
        {
            "sha": "commit-sha",
            "html_url": "https://github.com/example/calculator/commit/commit-sha",
            "commit": {
                "message": "Handle division by zero",
                "author": {
                    "name": "Contributor",
                    "date": "2026-01-02T00:30:00Z",
                },
            },
        }
    ]


def pull_request_diff() -> str:
    return (
        "diff --git a/calculator/core.py b/calculator/core.py\n"
        "--- a/calculator/core.py\n"
        "+++ b/calculator/core.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        raise ValueError('divisor must not be zero')\n"
        "     return a / b\n"
    )
