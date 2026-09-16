from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.github import GitHubService, classify_pull_request_file, is_likely_test_file
from app.main import create_app


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_api.py",
        "src/test_parser.py",
        "parser_test.py",
        "conftest.py",
        "web/button.test.ts",
        "web/button.spec.tsx",
        "web/__tests__/button.js",
        "src/unit_tests/parser.py",
        "src/parser_spec.rb",
        "src/ParserTest.java",
    ],
)
def test_detects_python_javascript_and_framework_test_files(path: str) -> None:
    assert is_likely_test_file(path)
    assert classify_pull_request_file(path) == "test"


@pytest.mark.parametrize(
    "path",
    ["src/latest.py", "src/contest.py", "docs/testing-guide.md", "package.json"],
)
def test_non_test_files_are_not_detected(path: str) -> None:
    assert not is_likely_test_file(path)


def test_trusted_preview_fetches_hidden_candidates_without_changing_public_preview() -> None:
    service = github_service()

    public = service.preview_pull_request("https://github.com/example/project", 7)
    trusted = service.preview_pull_request_trusted("https://github.com/example/project", 7)

    assert public.pull_request.files[0].file_kind == "source"
    assert public.pull_request.files[1].file_kind == "test"
    assert "hidden_test_candidates" not in public.model_dump()
    assert trusted.detected_test_files == ["tests/test_feature.py", "ui/button.test.ts"]
    assert trusted.hidden_test_candidates[0].content == "def test_feature():\n    assert True\n"
    assert trusted.hidden_test_candidates[0].suggested_commands == [
        "python -m pytest -q .benchmark-hidden-eval/tests/test_feature.py"
    ]
    assert trusted.hidden_test_candidates[1].content_available is True
    assert trusted.hidden_test_candidates[1].suggested_commands == []


def test_trusted_preview_endpoint_requires_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "trusted_operator_token", "operator-token")
    monkeypatch.setattr("app.api.routes.github.GitHubService", github_service)
    client = TestClient(create_app())

    denied = client.post("/github/preview-pr/trusted", json=preview_request())
    allowed = client.post(
        "/github/preview-pr/trusted",
        headers={"X-Operator-Token": "operator-token"},
        json=preview_request(),
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["detected_test_files"] == [
        "tests/test_feature.py",
        "ui/button.test.ts",
    ]
    assert "test_feature" in allowed.json()["hidden_test_candidates"][0]["content"]


def github_service() -> GitHubService:
    return GitHubService(
        token=None,
        api_base_url="https://api.github.test",
        client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ),
    )


def preview_request() -> dict[str, object]:
    return {
        "repository_url": "https://github.com/example/project",
        "pull_request_number": 7,
    }


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/example/project":
        return httpx.Response(
            200,
            json={
                "name": "project",
                "full_name": "example/project",
                "owner": {"login": "example"},
                "clone_url": "https://github.com/example/project.git",
                "html_url": "https://github.com/example/project",
                "default_branch": "main",
                "language": "Python",
            },
        )
    if path == "/repos/example/project/pulls/7":
        return httpx.Response(
            200,
            json={
                "number": 7,
                "title": "Fix feature",
                "state": "closed",
                "html_url": "https://github.com/example/project/pull/7",
                "merged": True,
                "merge_commit_sha": "merge-sha",
                "base": {"ref": "main", "sha": "base-sha"},
                "head": {"ref": "fix", "sha": "head-sha"},
            },
        )
    if path == "/repos/example/project/pulls/7/files":
        return httpx.Response(
            200,
            json=[
                {"filename": "src/feature.py", "status": "modified", "changes": 1},
                {
                    "filename": "tests/test_feature.py",
                    "status": "added",
                    "changes": 2,
                    "patch": "@@ -0,0 +1,2 @@",
                },
                {"filename": "ui/button.test.ts", "status": "modified", "changes": 2},
            ],
        )
    if path == "/repos/example/project/pulls/7/commits":
        return httpx.Response(200, json=[])
    if path == "/repos/example/project/contents/tests/test_feature.py":
        return content_response("def test_feature():\n    assert True\n")
    if path == "/repos/example/project/contents/ui/button.test.ts":
        return content_response("test('feature', () => expect(true).toBe(true));\n")
    return httpx.Response(404, json={"message": "not found"})


def content_response(content: str) -> httpx.Response:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": encoded})
