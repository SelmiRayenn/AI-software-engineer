from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.test_phases import TEST_PHASE_POST_PATCH
from app.models import AgentEvent, AgentRun, TestResult
from app.patches import PatchService


class ToolError(RuntimeError):
    pass


class ToolSafetyError(ToolError):
    pass


@dataclass(frozen=True)
class FileListingResult:
    files: list[str]
    truncated: bool = False


@dataclass(frozen=True)
class CodeSearchMatch:
    file_path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class CodeSearchResult:
    query: str
    matches: list[CodeSearchMatch]
    truncated: bool = False


@dataclass(frozen=True)
class FileReadResult:
    file_path: str
    content: str
    size_bytes: int


@dataclass(frozen=True)
class FileWriteResult:
    file_path: str
    bytes_written: int


@dataclass(frozen=True)
class TestCommandResult:
    command: str
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class DiffResult:
    patch_text: str
    changed_files: list[str]


@dataclass(frozen=True)
class SubmittedPatchResult:
    generated_patch_id: UUID
    patch_text: str
    changed_files: list[str]


@dataclass
class _ToolCallMetadata:
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class AgentWorkspaceTools:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        workspace_path: str | Path,
        allowed_test_commands: Sequence[str] | None = None,
        max_file_read_bytes: int = 200_000,
        max_search_results: int = 100,
        max_list_files: int = 2_000,
        command_timeout_seconds: int = 120,
        max_log_bytes: int = 200_000,
    ) -> None:
        self._db = db
        self._agent_run_id = agent_run_id
        self._workspace_path = Path(workspace_path).resolve(strict=True)
        if not self._workspace_path.is_dir():
            raise ToolSafetyError("Workspace path must be an existing directory.")

        agent_run = db.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise ToolError("Agent run does not exist.")

        task_commands = agent_run.benchmark_task.test_commands if agent_run.benchmark_task else []
        self._allowed_test_commands = set(allowed_test_commands or task_commands or [])
        self._max_file_read_bytes = max_file_read_bytes
        self._max_search_results = max_search_results
        self._max_list_files = max_list_files
        self._command_timeout_seconds = command_timeout_seconds
        self._max_log_bytes = max_log_bytes

    def list_files(self, path: str = ".") -> FileListingResult:
        def operation(metadata: _ToolCallMetadata) -> FileListingResult:
            directory, _ = self._resolve_path(path, must_exist=True)
            if not directory.is_dir():
                raise ToolSafetyError("list_files path must be a directory.")

            files: list[str] = []
            truncated = False
            for file_path in self._iter_workspace_files(directory):
                files.append(self._relative_to_workspace(file_path))
                if len(files) >= self._max_list_files:
                    truncated = True
                    break

            metadata.extra["file_count"] = len(files)
            metadata.extra["truncated"] = truncated
            return FileListingResult(files=files, truncated=truncated)

        return self._execute_tool("list_files", {"path": path}, operation)

    def search_code(self, query: str, path: str = ".") -> CodeSearchResult:
        def operation(metadata: _ToolCallMetadata) -> CodeSearchResult:
            if not query:
                raise ToolSafetyError("search_code query must not be empty.")

            search_root, _ = self._resolve_path(path, must_exist=True)
            if not search_root.is_dir():
                raise ToolSafetyError("search_code path must be a directory.")

            matches: list[CodeSearchMatch] = []
            truncated = False

            for file_path in self._iter_workspace_files(search_root):
                if file_path.stat().st_size > self._max_file_read_bytes:
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue

                relative_path = self._relative_to_workspace(file_path)
                metadata.files_read.append(relative_path)
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if query.lower() not in line.lower():
                        continue
                    matches.append(
                        CodeSearchMatch(
                            file_path=relative_path,
                            line_number=line_number,
                            line=line[:500],
                        )
                    )
                    if len(matches) >= self._max_search_results:
                        truncated = True
                        metadata.extra["truncated"] = truncated
                        return CodeSearchResult(query=query, matches=matches, truncated=truncated)

            metadata.extra["truncated"] = truncated
            return CodeSearchResult(query=query, matches=matches, truncated=truncated)

        return self._execute_tool("search_code", {"query": query, "path": path}, operation)

    def read_file(self, file_path: str) -> FileReadResult:
        def operation(metadata: _ToolCallMetadata) -> FileReadResult:
            path, relative_path = self._resolve_path(file_path, must_exist=True)
            if not path.is_file():
                raise ToolSafetyError("read_file path must be a file.")

            size_bytes = path.stat().st_size
            if size_bytes > self._max_file_read_bytes:
                raise ToolSafetyError(
                    f"File exceeds read limit of {self._max_file_read_bytes} bytes."
                )

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ToolSafetyError("Only UTF-8 text files can be read.") from exc

            metadata.files_read.append(relative_path)
            return FileReadResult(
                file_path=relative_path,
                content=content,
                size_bytes=size_bytes,
            )

        return self._execute_tool("read_file", {"file_path": file_path}, operation)

    def write_file(self, file_path: str, content: str) -> FileWriteResult:
        def operation(metadata: _ToolCallMetadata) -> FileWriteResult:
            path, relative_path = self._resolve_path(file_path, must_exist=False)
            self._reject_protected_gold_path(PurePosixPath(relative_path))

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

            bytes_written = len(content.encode("utf-8"))
            metadata.files_modified.append(relative_path)
            metadata.extra["bytes_written"] = bytes_written
            return FileWriteResult(file_path=relative_path, bytes_written=bytes_written)

        return self._execute_tool(
            "write_file",
            {"file_path": file_path, "content": content},
            operation,
        )

    def run_tests(self, command: str) -> TestCommandResult:
        def operation(metadata: _ToolCallMetadata) -> TestCommandResult:
            if command not in self._allowed_test_commands:
                raise ToolSafetyError("run_tests command is not in the allowed test command list.")

            started = time.perf_counter()
            timed_out = False
            try:
                completed = subprocess.run(
                    command,
                    cwd=self._workspace_path,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=self._command_timeout_seconds,
                    check=False,
                )
                exit_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                stdout = _decode_timeout_output(exc.stdout)
                stderr = _decode_timeout_output(exc.stderr)

            duration_seconds = time.perf_counter() - started
            result = TestCommandResult(
                command=command,
                passed=exit_code == 0 and not timed_out,
                exit_code=exit_code,
                stdout=self._truncate_log(stdout),
                stderr=self._truncate_log(stderr),
                duration_seconds=duration_seconds,
                timed_out=timed_out,
            )
            metadata.extra.update(
                {
                    "passed": result.passed,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                }
            )
            self._db.add(
                TestResult(
                    agent_run_id=self._agent_run_id,
                    phase=TEST_PHASE_POST_PATCH,
                    command=command,
                    passed=result.passed,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_seconds=result.duration_seconds,
                )
            )
            return result

        return self._execute_tool("run_tests", {"command": command}, operation)

    def get_diff(self) -> DiffResult:
        def operation(metadata: _ToolCallMetadata) -> DiffResult:
            result = self._build_diff()
            metadata.files_read.extend(result.changed_files)
            metadata.extra["changed_files"] = result.changed_files
            return result

        return self._execute_tool("get_diff", {}, operation)

    def submit_patch(self) -> SubmittedPatchResult:
        def operation(metadata: _ToolCallMetadata) -> SubmittedPatchResult:
            patch_service = self._patch_service()
            diff = patch_service.get_current_workspace_diff()
            generated_patch = patch_service.store_generated_patch(
                patch_text=diff.patch_text,
                changed_files=diff.changed_files,
            )
            metadata.files_read.extend(diff.changed_files)
            metadata.extra["changed_files"] = diff.changed_files
            return SubmittedPatchResult(
                generated_patch_id=generated_patch.id,
                patch_text=diff.patch_text,
                changed_files=diff.changed_files,
            )

        return self._execute_tool("submit_patch", {}, operation)

    def _execute_tool(
        self,
        tool_name: str,
        input_args: dict[str, Any],
        operation: Callable[[_ToolCallMetadata], Any],
    ):
        metadata = _ToolCallMetadata()
        started = time.perf_counter()
        try:
            result = operation(metadata)
        except Exception as exc:
            self._log_tool_call(
                tool_name=tool_name,
                input_args=input_args,
                success=False,
                error_message=str(exc),
                duration_seconds=time.perf_counter() - started,
                metadata=metadata,
            )
            raise

        self._log_tool_call(
            tool_name=tool_name,
            input_args=input_args,
            success=True,
            error_message=None,
            duration_seconds=time.perf_counter() - started,
            metadata=metadata,
        )
        return result

    def _log_tool_call(
        self,
        *,
        tool_name: str,
        input_args: dict[str, Any],
        success: bool,
        error_message: str | None,
        duration_seconds: float,
        metadata: _ToolCallMetadata,
    ) -> None:
        payload = {
            "tool_name": tool_name,
            "input": _sanitize_for_log(input_args),
            "success": success,
            "error_message": error_message,
            "duration_seconds": duration_seconds,
            "files_read": _unique_limited(metadata.files_read),
            "files_modified": _unique_limited(metadata.files_modified),
            **metadata.extra,
        }
        self._db.add(
            AgentEvent(
                agent_run_id=self._agent_run_id,
                event_type="agent_tool_call",
                payload_json=payload,
            )
        )
        self._db.commit()

    def _resolve_path(self, raw_path: str, *, must_exist: bool) -> tuple[Path, str]:
        relative_path = self._normalize_relative_path(raw_path)
        self._reject_protected_gold_path(relative_path)

        candidate = (self._workspace_path / Path(*relative_path.parts)).resolve(
            strict=must_exist
        )
        if not candidate.is_relative_to(self._workspace_path):
            raise ToolSafetyError("Path resolves outside the workspace.")

        resolved_relative = PurePosixPath(candidate.relative_to(self._workspace_path).as_posix())
        self._reject_protected_gold_path(resolved_relative)

        relative_string = "." if str(resolved_relative) == "." else resolved_relative.as_posix()
        return candidate, relative_string

    def _normalize_relative_path(self, raw_path: str) -> PurePosixPath:
        if not isinstance(raw_path, str):
            raise ToolSafetyError("Path must be a string.")
        if "\x00" in raw_path:
            raise ToolSafetyError("Path must not contain null bytes.")

        value = raw_path.strip() or "."
        windows_path = PureWindowsPath(value)
        if windows_path.is_absolute() or windows_path.drive:
            raise ToolSafetyError("Absolute paths are not allowed.")

        normalized = value.replace("\\", "/")
        relative_path = PurePosixPath(normalized)
        if relative_path.is_absolute():
            raise ToolSafetyError("Absolute paths are not allowed.")
        if any(part == ".." for part in relative_path.parts):
            raise ToolSafetyError("Path traversal is not allowed.")

        parts = [part for part in relative_path.parts if part not in {"", "."}]
        return PurePosixPath(*parts) if parts else PurePosixPath(".")

    def _reject_protected_gold_path(self, relative_path: PurePosixPath) -> None:
        parts = [part.lower() for part in relative_path.parts if part not in {"", "."}]
        if not parts:
            return

        protected_names = {
            ".gold",
            ".gold_patch",
            ".gold_solution",
            ".benchmark_gold",
            "gold_patch.diff",
            "gold_patch.patch",
            "gold_solution.diff",
            "gold_solution.patch",
        }
        if any(part in protected_names for part in parts):
            raise ToolSafetyError("Access to hidden gold solution files is blocked.")
        if len(parts) >= 2 and parts[0] == ".benchmark" and parts[1] == "gold":
            raise ToolSafetyError("Access to hidden gold solution files is blocked.")

    def _iter_workspace_files(self, root: Path):
        skip_dirs = {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        }
        for path in sorted(root.rglob("*")):
            relative = PurePosixPath(self._relative_to_workspace(path))
            if any(part in skip_dirs for part in relative.parts):
                continue
            if self._is_protected_gold_path(relative):
                continue
            if path.is_file():
                yield path

    def _is_protected_gold_path(self, relative_path: PurePosixPath) -> bool:
        try:
            self._reject_protected_gold_path(relative_path)
        except ToolSafetyError:
            return True
        return False

    def _relative_to_workspace(self, path: Path) -> str:
        return path.resolve().relative_to(self._workspace_path).as_posix()

    def _build_diff(self) -> DiffResult:
        diff = self._patch_service().get_current_workspace_diff()
        return DiffResult(patch_text=diff.patch_text, changed_files=diff.changed_files)

    def _patch_service(self) -> PatchService:
        return PatchService(
            db=self._db,
            agent_run_id=self._agent_run_id,
            workspace_path=self._workspace_path,
        )

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self._workspace_path,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ToolError(completed.stderr.strip() or "Git command failed.")
        return completed

    def _truncate_log(self, value: str | None) -> str:
        if not value:
            return ""
        encoded = value.encode("utf-8")
        if len(encoded) <= self._max_log_bytes:
            return value
        return encoded[: self._max_log_bytes].decode("utf-8", errors="replace")


def _sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, nested_value in value.items():
            if _is_sensitive_key(str(key)):
                sanitized[key] = "[REDACTED]"
                continue
            if key in {"content", "patch_text", "stdout", "stderr"} and isinstance(
                nested_value,
                str,
            ):
                sanitized[key] = {
                    "size_bytes": len(nested_value.encode("utf-8")),
                    "preview": _redact_secret_patterns(nested_value[:200]),
                }
            else:
                sanitized[key] = _sanitize_for_log(nested_value)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return {
            "size_bytes": len(value.encode("utf-8")),
            "preview": _redact_secret_patterns(value[:200]),
        }
    if isinstance(value, str):
        return _redact_secret_patterns(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return (
        normalized in {"token", "access_token", "refresh_token", "auth_token"}
        or "api_key" in normalized
        or "apikey" in normalized
        or "password" in normalized
        or "secret" in normalized
        or "authorization" in normalized
    )


def _redact_secret_patterns(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    redacted = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b",
        "[REDACTED]",
        redacted,
    )
    return redacted


def _unique_limited(values: list[str], limit: int = 100) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
