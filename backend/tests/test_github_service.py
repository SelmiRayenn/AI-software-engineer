from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.github import GitHubService, parse_github_repo_url
from app.main import app


def test_parse_github_repo_urls() -> None:
    examples = [
        ("https://github.com/example/calculator", ("example", "calculator")),
        ("https://github.com/example/calculator.git", ("example", "calculator")),
        ("github.com/example/calculator", ("example", "calculator")),
        ("git@github.com:example/calculator.git", ("example", "calculator")),
        ("https://github.com/example/calculator/issues/42", ("example", "calculator")),
    ]

    for url, expected in examples:
        parsed = parse_github_repo_url(url)
        assert (parsed.owner, parsed.name) == expected


def test_parse_github_repo_url_rejects_non_github_url() -> None:
    with pytest.raises(ValueError, match="Only github.com"):
        parse_github_repo_url("https://gitlab.com/example/calculator")


def test_preview_issue_parses_mocked_responses_without_token() -> None:
    seen_authorization_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization_headers.append(request.headers.get("authorization"))

        match request.url.path:
            case "/repos/example/calculator":
                return json_response(repository_payload())
            case "/repos/example/calculator/issues/42":
                return json_response(issue_payload())
            case "/repos/example/calculator/issues/42/comments":
                return json_response([comment_payload()])
            case "/repos/example/calculator/issues/42/timeline":
                return json_response([timeline_cross_reference_payload()])
            case "/repos/example/calculator/pulls/43":
                return json_response(pull_request_payload())
            case "/repos/example/calculator/pulls/43/files":
                return json_response(files_payload())
            case "/repos/example/calculator/pulls/43/commits":
                return json_response(commits_payload())

        return json_response({"message": "not found"}, status_code=404)

    service = GitHubService(
        token=None,
        api_base_url="https://api.github.test",
        client=httpx.Client(
            base_url="https://api.github.test",
            headers=service_headers_without_token(),
            transport=httpx.MockTransport(handler),
        ),
    )

    preview = service.preview_issue(
        repository_url="https://github.com/example/calculator",
        issue_number=42,
    )

    assert preview.repository.full_name == "example/calculator"
    assert preview.issue.title == "Division by zero is unclear"
    assert preview.comments[0].body == "I can reproduce this."
    assert [pr.number for pr in preview.linked_pull_requests] == [43]
    assert preview.benchmark_task_hint.base_commit == "base-sha"
    assert preview.benchmark_task_hint.fix_commit == "merge-sha"
    assert preview.benchmark_task_hint.changed_files == [
        "calculator/core.py",
        "tests/test_core.py",
    ]
    assert preview.benchmark_task_hint.test_files == ["tests/test_core.py"]
    assert all(header is None for header in seen_authorization_headers)


def test_preview_pull_request_parses_mocked_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/repos/example/calculator":
                return json_response(repository_payload())
            case "/repos/example/calculator/pulls/43":
                return json_response(pull_request_payload())
            case "/repos/example/calculator/pulls/43/files":
                return json_response(files_payload())
            case "/repos/example/calculator/pulls/43/commits":
                return json_response(commits_payload())

        return json_response({"message": "not found"}, status_code=404)

    service = GitHubService(
        api_base_url="https://api.github.test",
        client=httpx.Client(
            base_url="https://api.github.test",
            headers=service_headers_without_token(),
            transport=httpx.MockTransport(handler),
        ),
    )

    preview = service.preview_pull_request(
        repository_url="https://github.com/example/calculator",
        pull_request_number=43,
    )

    assert preview.pull_request.number == 43
    assert preview.pull_request.files[0].filename == "calculator/core.py"
    assert preview.pull_request.commits[0].sha == "commit-sha"
    assert preview.benchmark_task_hint.base_commit == "base-sha"
    assert preview.benchmark_task_hint.linked_pr_url.endswith("/pull/43")


def test_preview_issue_endpoint_uses_structured_service_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubService:
        def preview_issue(self, repository_url: str, issue_number: int):
            assert repository_url == "https://github.com/example/calculator"
            assert issue_number == 42
            return GitHubService(
                api_base_url="https://api.github.test",
                client=httpx.Client(
                    base_url="https://api.github.test",
                    headers=service_headers_without_token(),
                    transport=httpx.MockTransport(endpoint_handler),
                ),
            ).preview_issue(repository_url, issue_number)

    monkeypatch.setattr("app.api.routes.github.GitHubService", FakeGitHubService)

    response = TestClient(app).post(
        "/github/preview-issue",
        json={
            "repository_url": "https://github.com/example/calculator",
            "issue_number": 42,
        },
    )

    assert response.status_code == 200
    assert response.json()["benchmark_task_hint"]["linked_pr_url"].endswith("/pull/43")


def test_preview_pull_request_endpoint_uses_structured_service_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubService:
        def preview_pull_request(self, repository_url: str, pull_request_number: int):
            assert repository_url == "https://github.com/example/calculator"
            assert pull_request_number == 43
            return GitHubService(
                api_base_url="https://api.github.test",
                client=httpx.Client(
                    base_url="https://api.github.test",
                    headers=service_headers_without_token(),
                    transport=httpx.MockTransport(endpoint_pr_handler),
                ),
            ).preview_pull_request(repository_url, pull_request_number)

    monkeypatch.setattr("app.api.routes.github.GitHubService", FakeGitHubService)

    response = TestClient(app).post(
        "/github/preview-pr",
        json={
            "repository_url": "https://github.com/example/calculator",
            "pull_request_number": 43,
        },
    )

    assert response.status_code == 200
    assert response.json()["benchmark_task_hint"]["base_commit"] == "base-sha"


def endpoint_handler(request: httpx.Request) -> httpx.Response:
    match request.url.path:
        case "/repos/example/calculator":
            return json_response(repository_payload())
        case "/repos/example/calculator/issues/42":
            return json_response(issue_payload())
        case "/repos/example/calculator/issues/42/comments":
            return json_response([])
        case "/repos/example/calculator/issues/42/timeline":
            return json_response([timeline_cross_reference_payload()])
        case "/repos/example/calculator/pulls/43":
            return json_response(pull_request_payload())
        case "/repos/example/calculator/pulls/43/files":
            return json_response(files_payload())
        case "/repos/example/calculator/pulls/43/commits":
            return json_response(commits_payload())
    return json_response({"message": "not found"}, status_code=404)


def endpoint_pr_handler(request: httpx.Request) -> httpx.Response:
    match request.url.path:
        case "/repos/example/calculator":
            return json_response(repository_payload())
        case "/repos/example/calculator/pulls/43":
            return json_response(pull_request_payload())
        case "/repos/example/calculator/pulls/43/files":
            return json_response(files_payload())
        case "/repos/example/calculator/pulls/43/commits":
            return json_response(commits_payload())
    return json_response({"message": "not found"}, status_code=404)


def service_headers_without_token() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-benchmark-platform",
    }


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload)


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


def issue_payload() -> dict[str, object]:
    return {
        "number": 42,
        "title": "Division by zero is unclear",
        "body": "This should be fixed by https://github.com/example/calculator/pull/43.",
        "state": "closed",
        "html_url": "https://github.com/example/calculator/issues/42",
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


def timeline_cross_reference_payload() -> dict[str, object]:
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "number": 43,
                "pull_request": {
                    "url": "https://api.github.test/repos/example/calculator/pulls/43"
                },
                "repository": {"full_name": "example/calculator"},
            }
        },
    }


def pull_request_payload() -> dict[str, object]:
    return {
        "number": 43,
        "title": "Handle division by zero",
        "body": "Fixes #42",
        "state": "closed",
        "html_url": "https://github.com/example/calculator/pull/43",
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
