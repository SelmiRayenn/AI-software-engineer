from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.core.config import settings
from app.github.test_detection import classify_pull_request_file, is_likely_test_file
from app.schemas.github import (
    GitHubBenchmarkTaskHint,
    GitHubHiddenTestCandidate,
    GitHubIssueCommentPreview,
    GitHubIssuePreview,
    GitHubIssuePreviewResponse,
    GitHubPullRequestCommitPreview,
    GitHubPullRequestFilePreview,
    GitHubPullRequestPreview,
    GitHubPullRequestPreviewResponse,
    GitHubRepositoryPreview,
    GitHubTrustedPullRequestPreviewResponse,
    GitHubUserPreview,
)

GITHUB_API_BASE_URL = "https://api.github.com"
LINKED_PR_LIMIT = 10
MAX_HIDDEN_TEST_CONTENT_BYTES = 250_000


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class GitHubClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_json: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_json = response_json


def parse_github_repo_url(repository_url: str) -> GitHubRepoRef:
    value = repository_url.strip()
    if not value:
        raise ValueError("GitHub repository URL must not be empty")

    ssh_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", value)
    if ssh_match:
        return GitHubRepoRef(owner=ssh_match.group("owner"), name=ssh_match.group("repo"))

    if value.startswith("github.com/"):
        value = f"https://{value}"

    parsed = urlparse(value)
    if parsed.netloc.lower() != "github.com":
        raise ValueError("Only github.com repository URLs are supported")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub repository URL must include owner and repository name")

    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo.removesuffix(".git")

    if not parts[0] or not repo:
        raise ValueError("GitHub repository URL must include owner and repository name")

    return GitHubRepoRef(owner=parts[0], name=repo)


class GitHubService:
    def __init__(
        self,
        token: str | None = None,
        api_base_url: str = GITHUB_API_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token if token is not None else settings.github_token
        self._api_base_url = api_base_url.rstrip("/")
        self._client = client

    def preview_issue(self, repository_url: str, issue_number: int) -> GitHubIssuePreviewResponse:
        repo_ref = parse_github_repo_url(repository_url)
        repository_data = self.fetch_repository_metadata(repo_ref)
        issue_data = self.fetch_issue(repo_ref, issue_number)
        comments_data = self.fetch_issue_comments(repo_ref, issue_number)
        linked_pr_numbers = self.detect_linked_pull_requests(
            repo_ref=repo_ref,
            issue_data=issue_data,
            comments_data=comments_data,
        )
        linked_pull_requests = [
            self._build_pull_request_preview(repo_ref, pr_number)
            for pr_number in linked_pr_numbers[:LINKED_PR_LIMIT]
        ]
        issue = self._issue_preview(issue_data)
        repository = self._repository_preview(repository_data)
        first_pr = linked_pull_requests[0] if linked_pull_requests else None

        return GitHubIssuePreviewResponse(
            repository=repository,
            issue=issue,
            comments=[self._comment_preview(comment) for comment in comments_data],
            linked_pull_requests=linked_pull_requests,
            benchmark_task_hint=GitHubBenchmarkTaskHint(
                repository_url=repository.html_url,
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                base_commit=first_pr.base_sha if first_pr else None,
                fix_commit=self._fix_commit(first_pr) if first_pr else None,
                linked_pr_url=first_pr.html_url if first_pr else None,
                changed_files=[file.filename for file in first_pr.files] if first_pr else [],
                test_files=self._test_files(first_pr.files) if first_pr else [],
            ),
        )

    def preview_pull_request(
        self,
        repository_url: str,
        pull_request_number: int,
    ) -> GitHubPullRequestPreviewResponse:
        repo_ref = parse_github_repo_url(repository_url)
        repository_data = self.fetch_repository_metadata(repo_ref)
        repository = self._repository_preview(repository_data)
        pull_request = self._build_pull_request_preview(repo_ref, pull_request_number)

        return GitHubPullRequestPreviewResponse(
            repository=repository,
            pull_request=pull_request,
            benchmark_task_hint=GitHubBenchmarkTaskHint(
                repository_url=repository.html_url,
                base_commit=pull_request.base_sha,
                fix_commit=self._fix_commit(pull_request),
                linked_pr_url=pull_request.html_url,
                changed_files=[file.filename for file in pull_request.files],
                test_files=self._test_files(pull_request.files),
            ),
        )

    def preview_pull_request_trusted(
        self,
        repository_url: str,
        pull_request_number: int,
    ) -> GitHubTrustedPullRequestPreviewResponse:
        preview = self.preview_pull_request(repository_url, pull_request_number)
        repo_ref = parse_github_repo_url(repository_url)
        candidates = self.hidden_test_candidates(repo_ref, preview.pull_request)
        return GitHubTrustedPullRequestPreviewResponse(
            **preview.model_dump(),
            detected_test_files=self._test_files(preview.pull_request.files),
            hidden_test_candidates=candidates,
        )

    def fetch_repository_metadata(self, repo_ref: GitHubRepoRef) -> dict[str, Any]:
        return self._get_json(f"/repos/{repo_ref.owner}/{repo_ref.name}")

    def fetch_issue(self, repo_ref: GitHubRepoRef, issue_number: int) -> dict[str, Any]:
        return self._get_json(f"/repos/{repo_ref.owner}/{repo_ref.name}/issues/{issue_number}")

    def fetch_issue_comments(
        self, repo_ref: GitHubRepoRef, issue_number: int
    ) -> list[dict[str, Any]]:
        return self._get_paginated_json(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/issues/{issue_number}/comments"
        )

    def detect_linked_pull_requests(
        self,
        repo_ref: GitHubRepoRef,
        issue_data: dict[str, Any],
        comments_data: list[dict[str, Any]],
    ) -> list[int]:
        found: list[int] = []

        if issue_data.get("pull_request"):
            self._append_unique(found, self._number(issue_data.get("number")))

        self._append_many(found, self._find_pr_numbers_in_text(repo_ref, issue_data.get("body")))
        for comment in comments_data:
            self._append_many(found, self._find_pr_numbers_in_text(repo_ref, comment.get("body")))

        for event in self._fetch_issue_timeline(repo_ref, self._number(issue_data.get("number"))):
            self._append_many(found, self._pr_numbers_from_timeline_event(repo_ref, event))

        return found

    def fetch_pull_request_metadata(
        self,
        repo_ref: GitHubRepoRef,
        pull_request_number: int,
    ) -> dict[str, Any]:
        return self._get_json(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/pulls/{pull_request_number}"
        )

    def fetch_pull_request_files(
        self,
        repo_ref: GitHubRepoRef,
        pull_request_number: int,
    ) -> list[dict[str, Any]]:
        return self._get_paginated_json(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/pulls/{pull_request_number}/files"
        )

    def fetch_pull_request_commits(
        self,
        repo_ref: GitHubRepoRef,
        pull_request_number: int,
    ) -> list[dict[str, Any]]:
        return self._get_paginated_json(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/pulls/{pull_request_number}/commits"
        )

    def fetch_pull_request_diff(self, repo_ref: GitHubRepoRef, pull_request_number: int) -> str:
        return self._get_text(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/pulls/{pull_request_number}",
            accept="application/vnd.github.v3.diff",
        )

    def fetch_file_content(self, repo_ref: GitHubRepoRef, path: str, ref: str) -> str:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        payload = self._get_json(
            f"/repos/{repo_ref.owner}/{repo_ref.name}/contents/{encoded_path}?ref={quote(ref, safe='')}"
        )
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            raise GitHubClientError("GitHub did not return inline base64 file content")
        encoded_content = payload.get("content")
        if not isinstance(encoded_content, str):
            raise GitHubClientError("GitHub did not return file content")
        if self._number(payload.get("size")) > MAX_HIDDEN_TEST_CONTENT_BYTES:
            raise GitHubClientError(
                f"Test file exceeds the {MAX_HIDDEN_TEST_CONTENT_BYTES}-byte candidate limit"
            )
        try:
            raw = base64.b64decode("".join(encoded_content.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GitHubClientError("GitHub returned invalid base64 file content") from exc
        if len(raw) > MAX_HIDDEN_TEST_CONTENT_BYTES:
            raise GitHubClientError(
                f"Test file exceeds the {MAX_HIDDEN_TEST_CONTENT_BYTES}-byte candidate limit"
            )
        if b"\x00" in raw:
            raise GitHubClientError("Binary test files cannot become hidden evaluation candidates")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubClientError("Test file is not valid UTF-8 text") from exc

    def repository_preview_from_data(self, data: dict[str, Any]) -> GitHubRepositoryPreview:
        return self._repository_preview(data)

    def issue_preview_from_data(self, data: dict[str, Any]) -> GitHubIssuePreview:
        return self._issue_preview(data)

    def comment_preview_from_data(self, data: dict[str, Any]) -> GitHubIssueCommentPreview:
        return self._comment_preview(data)

    def pull_request_preview_from_data(
        self,
        data: dict[str, Any],
        files_data: list[dict[str, Any]],
        commits_data: list[dict[str, Any]],
    ) -> GitHubPullRequestPreview:
        return self._pull_request_preview(data, files_data, commits_data)

    def test_files_from_pull_request_files(
        self,
        files: list[GitHubPullRequestFilePreview],
    ) -> list[str]:
        return self._test_files(files)

    def hidden_test_candidates(
        self,
        repo_ref: GitHubRepoRef,
        pull_request: GitHubPullRequestPreview,
        configured_test_commands: list[str] | None = None,
    ) -> list[GitHubHiddenTestCandidate]:
        candidates: list[GitHubHiddenTestCandidate] = []
        for file in pull_request.files:
            if not is_likely_test_file(file.filename):
                continue
            commands = self._hidden_test_commands(file.filename, configured_test_commands or [])
            content: str | None = None
            reason: str | None = None
            if file.status == "removed":
                reason = "Removed test files have no content at the pull request head."
            elif not pull_request.head_sha:
                reason = "Pull request head commit is unavailable."
            else:
                try:
                    content = self.fetch_file_content(
                        repo_ref,
                        file.filename,
                        pull_request.head_sha,
                    )
                except GitHubClientError as exc:
                    reason = str(exc)
            if content is not None and not commands:
                reason = "No supported test command could be inferred for this file."
            candidates.append(
                GitHubHiddenTestCandidate(
                    path=file.filename,
                    status=file.status,
                    patch=file.patch,
                    content=content,
                    content_available=content is not None,
                    suggested_commands=commands,
                    unavailable_reason=reason,
                )
            )
        return candidates

    def fix_commit_from_pull_request(
        self,
        pull_request: GitHubPullRequestPreview | None,
    ) -> str | None:
        return self._fix_commit(pull_request)

    def _build_pull_request_preview(
        self,
        repo_ref: GitHubRepoRef,
        pull_request_number: int,
    ) -> GitHubPullRequestPreview:
        pr_data = self.fetch_pull_request_metadata(repo_ref, pull_request_number)
        files_data = self.fetch_pull_request_files(repo_ref, pull_request_number)
        commits_data = self.fetch_pull_request_commits(repo_ref, pull_request_number)
        return self._pull_request_preview(pr_data, files_data, commits_data)

    def _get_json(self, path: str) -> dict[str, Any]:
        with self._http_client() as client:
            response = client.get(path, headers=self._headers())
        return self._parse_response_object(response)

    def _get_text(self, path: str, accept: str) -> str:
        with self._http_client() as client:
            response = client.get(path, headers=self._headers(accept=accept))

        payload = self._response_payload(response)
        if response.status_code >= 400:
            raise GitHubClientError(
                self._error_message(payload),
                status_code=response.status_code,
                response_json=payload if isinstance(payload, dict) else None,
            )
        return response.text

    def _get_paginated_json(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_path: str | None = path

        with self._http_client() as client:
            while next_path:
                response = client.get(next_path, headers=self._headers())
                page = self._parse_response_list(response)
                items.extend(page)
                next_url = response.links.get("next", {}).get("url")
                next_path = self._relative_api_url(next_url)

        return items

    def _fetch_issue_timeline(
        self, repo_ref: GitHubRepoRef, issue_number: int
    ) -> list[dict[str, Any]]:
        try:
            return self._get_paginated_json(
                f"/repos/{repo_ref.owner}/{repo_ref.name}/issues/{issue_number}/timeline"
            )
        except GitHubClientError:
            return []

    def _http_client(self) -> httpx.Client:
        if self._client is not None:
            return _ReusableClientContext(self._client)

        return httpx.Client(
            base_url=self._api_base_url,
            headers=self._headers(),
            timeout=20,
            follow_redirects=True,
        )

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-benchmark-platform",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _parse_response_object(self, response: httpx.Response) -> dict[str, Any]:
        payload = self._response_payload(response)
        if response.status_code >= 400:
            raise GitHubClientError(
                self._error_message(payload),
                status_code=response.status_code,
                response_json=payload if isinstance(payload, dict) else None,
            )
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub API returned an unexpected response shape")
        return payload

    def _parse_response_list(self, response: httpx.Response) -> list[dict[str, Any]]:
        payload = self._response_payload(response)
        if response.status_code >= 400:
            raise GitHubClientError(
                self._error_message(payload),
                status_code=response.status_code,
                response_json=payload if isinstance(payload, dict) else None,
            )
        if not isinstance(payload, list):
            raise GitHubClientError("GitHub API returned an unexpected response shape")
        return [item for item in payload if isinstance(item, dict)]

    def _response_payload(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"message": response.text}

    def _error_message(self, payload: Any) -> str:
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
        return "GitHub API request failed"

    def _relative_api_url(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        if not parsed.path:
            return None
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    def _repository_preview(self, data: dict[str, Any]) -> GitHubRepositoryPreview:
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        return GitHubRepositoryPreview(
            owner=str(owner.get("login") or data.get("owner") or ""),
            name=str(data.get("name") or ""),
            full_name=str(data.get("full_name") or ""),
            url=str(data.get("clone_url") or data.get("html_url") or ""),
            html_url=str(data.get("html_url") or ""),
            default_branch=str(data.get("default_branch") or "main"),
            language=data.get("language"),
            description=data.get("description"),
            private=bool(data.get("private", False)),
        )

    def _issue_preview(self, data: dict[str, Any]) -> GitHubIssuePreview:
        return GitHubIssuePreview(
            number=self._number(data.get("number")),
            title=str(data.get("title") or ""),
            body=data.get("body"),
            state=str(data.get("state") or ""),
            html_url=str(data.get("html_url") or ""),
            user=self._user_preview(data.get("user")),
            labels=self._label_names(data.get("labels")),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            closed_at=data.get("closed_at"),
            is_pull_request=bool(data.get("pull_request")),
        )

    def _comment_preview(self, data: dict[str, Any]) -> GitHubIssueCommentPreview:
        return GitHubIssueCommentPreview(
            id=self._number(data.get("id")),
            body=data.get("body"),
            html_url=str(data.get("html_url") or ""),
            user=self._user_preview(data.get("user")),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def _pull_request_preview(
        self,
        data: dict[str, Any],
        files_data: list[dict[str, Any]],
        commits_data: list[dict[str, Any]],
    ) -> GitHubPullRequestPreview:
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        return GitHubPullRequestPreview(
            number=self._number(data.get("number")),
            title=str(data.get("title") or ""),
            body=data.get("body"),
            state=str(data.get("state") or ""),
            html_url=str(data.get("html_url") or ""),
            merged=data.get("merged"),
            merge_commit_sha=data.get("merge_commit_sha"),
            base_branch=str(base.get("ref") or ""),
            base_sha=str(base.get("sha") or ""),
            head_branch=str(head.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            user=self._user_preview(data.get("user")),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            closed_at=data.get("closed_at"),
            merged_at=data.get("merged_at"),
            files=[self._file_preview(file_data) for file_data in files_data],
            commits=[self._commit_preview(commit_data) for commit_data in commits_data],
        )

    def _file_preview(self, data: dict[str, Any]) -> GitHubPullRequestFilePreview:
        filename = str(data.get("filename") or "")
        return GitHubPullRequestFilePreview(
            filename=filename,
            status=str(data.get("status") or ""),
            file_kind=classify_pull_request_file(filename),
            previous_filename=data.get("previous_filename"),
            additions=self._number(data.get("additions")),
            deletions=self._number(data.get("deletions")),
            changes=self._number(data.get("changes")),
            patch=data.get("patch"),
        )

    def _commit_preview(self, data: dict[str, Any]) -> GitHubPullRequestCommitPreview:
        commit = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        return GitHubPullRequestCommitPreview(
            sha=str(data.get("sha") or ""),
            message=commit.get("message"),
            author_name=author.get("name"),
            author_date=author.get("date"),
            html_url=data.get("html_url"),
        )

    def _user_preview(self, data: Any) -> GitHubUserPreview | None:
        if not isinstance(data, dict):
            return None
        return GitHubUserPreview(login=data.get("login"), html_url=data.get("html_url"))

    def _label_names(self, labels: Any) -> list[str]:
        if not isinstance(labels, list):
            return []
        names: list[str] = []
        for label in labels:
            if isinstance(label, dict) and label.get("name"):
                names.append(str(label["name"]))
            elif isinstance(label, str):
                names.append(label)
        return names

    def _find_pr_numbers_in_text(self, repo_ref: GitHubRepoRef, text: str | None) -> list[int]:
        if not text:
            return []

        patterns = [
            rf"github\.com/{re.escape(repo_ref.owner)}/{re.escape(repo_ref.name)}/pull/(\d+)",
            r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
            r"\bPR\s+#(\d+)",
        ]
        numbers: list[int] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                self._append_unique(numbers, int(match.group(1)))
        return numbers

    def _pr_numbers_from_timeline_event(
        self,
        repo_ref: GitHubRepoRef,
        event: dict[str, Any],
    ) -> list[int]:
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        issue = source.get("issue") if isinstance(source.get("issue"), dict) else {}

        numbers: list[int] = []
        if issue.get("pull_request"):
            repo = issue.get("repository") if isinstance(issue.get("repository"), dict) else {}
            full_name = repo.get("full_name")
            if not full_name or full_name == repo_ref.full_name:
                self._append_unique(numbers, self._number(issue.get("number")))

        self._append_many(numbers, self._find_pr_numbers_in_text(repo_ref, event.get("body")))
        return numbers

    def _append_many(self, target: list[int], values: Iterable[int]) -> None:
        for value in values:
            self._append_unique(target, value)

    def _append_unique(self, target: list[int], value: int) -> None:
        if value > 0 and value not in target:
            target.append(value)

    def _test_files(self, files: list[GitHubPullRequestFilePreview]) -> list[str]:
        return list(dict.fromkeys(file.filename for file in files if file.file_kind == "test"))

    def _hidden_test_commands(
        self,
        path: str,
        configured_test_commands: list[str],
    ) -> list[str]:
        candidate = PurePosixPath(path)
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or candidate.is_absolute()
            or not candidate.parts
            or ".." in candidate.parts
        ):
            return []
        staged_path = f".benchmark-hidden-eval/{path}"
        suffix = candidate.suffix.lower()
        if suffix == ".py":
            return [f"python -m pytest -q {staged_path}"]
        if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            lowered = "\n".join(configured_test_commands).lower()
            if "vitest" in lowered:
                return [f"npx --no-install vitest run {staged_path}"]
            if "jest" in lowered:
                return [f"npx --no-install jest --runInBand {staged_path}"]
        if suffix == ".rb" and any(
            "rspec" in command.lower() for command in configured_test_commands
        ):
            return [f"bundle exec rspec {staged_path}"]
        return []

    def _fix_commit(self, pull_request: GitHubPullRequestPreview | None) -> str | None:
        if pull_request is None:
            return None
        return pull_request.merge_commit_sha or pull_request.head_sha

    def _number(self, value: Any) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0


class _ReusableClientContext:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None
