from __future__ import annotations

import difflib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.run_statuses import RUN_STATUS_QUEUED, RUN_STATUS_RUNNING
from app.models import AgentEvent, AgentRun, GeneratedPatch

PATCH_APPLY_ALLOWED_STATUSES = {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}
_GIT_COMMAND_TIMEOUT_SECONDS = 30


class PatchError(RuntimeError):
    pass


class PatchWorkspaceError(PatchError):
    pass


class PatchSafetyError(PatchError):
    pass


class PatchApplyError(PatchError):
    pass


@dataclass(frozen=True)
class PatchSizeStats:
    size_bytes: int
    changed_files_count: int
    additions: int
    deletions: int


@dataclass(frozen=True)
class WorkspaceDiffResult:
    patch_text: str
    changed_files: list[str]
    stats: PatchSizeStats


@dataclass(frozen=True)
class PatchValidationResult:
    valid: bool
    changed_files: list[str]
    stats: PatchSizeStats
    errors: list[str]


@dataclass(frozen=True)
class PatchApplyResult:
    generated_patch_id: UUID
    patch_text: str
    changed_files: list[str]
    stats: PatchSizeStats


@dataclass(frozen=True)
class PatchEnsureAppliedResult:
    applied: bool
    already_applied: bool
    changed_files: list[str]
    stats: PatchSizeStats


class PatchService:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        workspace_path: str | Path | None = None,
        max_patch_bytes: int | None = None,
        max_changed_files: int | None = None,
    ) -> None:
        self._db = db
        self._agent_run_id = agent_run_id
        self._agent_run = db.get(AgentRun, agent_run_id)
        if self._agent_run is None:
            raise PatchError("Agent run not found.")

        raw_workspace_path = workspace_path or self._agent_run.workspace_path
        if raw_workspace_path is None:
            raise PatchWorkspaceError("Agent run does not have an active workspace path.")

        try:
            self._workspace_path = Path(raw_workspace_path).resolve(strict=True)
        except OSError as exc:
            raise PatchWorkspaceError("Agent run workspace path does not exist.") from exc

        if not self._workspace_path.is_dir():
            raise PatchWorkspaceError("Agent run workspace path must be a directory.")

        self._max_patch_bytes = max_patch_bytes or settings.patch_max_bytes
        self._max_changed_files = max_changed_files or settings.patch_max_changed_files
        self._ensure_git_workspace()

    def get_current_workspace_diff(self) -> WorkspaceDiffResult:
        patch_text = self.create_unified_diff_from_workspace_changes()
        changed_files = self._current_changed_files()
        stats = self.calculate_patch_size_statistics(patch_text, changed_files)
        self._validate_patch_limits(patch_text, changed_files)
        return WorkspaceDiffResult(
            patch_text=patch_text,
            changed_files=changed_files,
            stats=stats,
        )

    def create_unified_diff_from_workspace_changes(self) -> str:
        tracked_diff = self._run_git(["diff", "--no-ext-diff", "--"]).stdout
        if tracked_diff.strip():
            self._reject_binary_patch(tracked_diff)

        patch_parts = [tracked_diff.rstrip("\n")] if tracked_diff.strip() else []
        for relative_path in self._untracked_files():
            patch_parts.append(self._new_file_patch(relative_path))

        patch_text = "\n".join(part for part in patch_parts if part)
        if patch_text:
            patch_text = f"{patch_text}\n"
            changed_files = self.list_changed_files_from_patch(patch_text)
            self._validate_patch_limits(patch_text, changed_files)
        return patch_text

    def validate_patch_applies_cleanly(self, patch_text: str) -> PatchValidationResult:
        try:
            if not patch_text.strip():
                raise PatchSafetyError("Patch text must not be empty.")
            changed_files = self.list_changed_files_from_patch(patch_text)
            if not changed_files:
                raise PatchSafetyError("Patch must include at least one changed file.")
            stats = self.calculate_patch_size_statistics(patch_text, changed_files)
            self._validate_patch_limits(patch_text, changed_files)
        except PatchError as exc:
            return PatchValidationResult(
                valid=False,
                changed_files=[],
                stats=self.calculate_patch_size_statistics(patch_text, []),
                errors=[str(exc)],
            )

        try:
            completed = self._run_git_with_input(
                ["apply", "--check", "--whitespace=nowarn"],
                patch_text,
            )
        except PatchError as exc:
            return PatchValidationResult(
                valid=False,
                changed_files=changed_files,
                stats=stats,
                errors=[str(exc)],
            )
        if completed.returncode != 0:
            return PatchValidationResult(
                valid=False,
                changed_files=changed_files,
                stats=stats,
                errors=[completed.stderr.strip() or "Patch does not apply cleanly."],
            )

        return PatchValidationResult(
            valid=True,
            changed_files=changed_files,
            stats=stats,
            errors=[],
        )

    def apply_unified_diff(self, patch_text: str) -> PatchApplyResult:
        self._ensure_run_allows_patch_application()
        applied = self.ensure_patch_applied(patch_text)
        generated_patch = self.store_generated_patch(
            patch_text=patch_text,
            changed_files=applied.changed_files,
        )
        self._log_event(
            "patch_applied",
            {
                "generated_patch_id": str(generated_patch.id),
                "changed_files": applied.changed_files,
                "stats": _stats_payload(applied.stats),
                "already_applied": applied.already_applied,
            },
        )
        return PatchApplyResult(
            generated_patch_id=generated_patch.id,
            patch_text=patch_text,
            changed_files=applied.changed_files,
            stats=applied.stats,
        )

    def ensure_patch_applied(self, patch_text: str) -> PatchEnsureAppliedResult:
        self._ensure_run_allows_patch_application()
        if not patch_text.strip():
            return PatchEnsureAppliedResult(
                applied=False,
                already_applied=False,
                changed_files=[],
                stats=self.calculate_patch_size_statistics("", []),
            )

        changed_files = self.list_changed_files_from_patch(patch_text)
        if not changed_files:
            raise PatchSafetyError("Patch must include at least one changed file.")
        stats = self.calculate_patch_size_statistics(patch_text, changed_files)
        self._validate_patch_limits(patch_text, changed_files)

        clean_check = self._run_git_with_input(
            ["apply", "--check", "--whitespace=nowarn"],
            patch_text,
        )
        if clean_check.returncode == 0:
            completed = self._run_git_with_input(["apply", "--whitespace=nowarn"], patch_text)
            if completed.returncode != 0:
                raise PatchApplyError(completed.stderr.strip() or "Patch application failed.")
            return PatchEnsureAppliedResult(
                applied=True,
                already_applied=False,
                changed_files=changed_files,
                stats=stats,
            )

        reverse_check = self._run_git_with_input(
            ["apply", "--reverse", "--check", "--whitespace=nowarn"],
            patch_text,
        )
        if reverse_check.returncode == 0:
            return PatchEnsureAppliedResult(
                applied=False,
                already_applied=True,
                changed_files=changed_files,
                stats=stats,
            )

        raise PatchApplyError(clean_check.stderr.strip() or "Patch does not apply cleanly.")

    def store_generated_patch(self, *, patch_text: str, changed_files: list[str]) -> GeneratedPatch:
        changed_files = self._sanitize_changed_files(changed_files)
        stats = self.calculate_patch_size_statistics(patch_text, changed_files)
        self._validate_patch_limits(patch_text, changed_files)

        generated_patch = self._db.scalar(
            select(GeneratedPatch).where(GeneratedPatch.agent_run_id == self._agent_run_id)
        )
        if generated_patch is None:
            generated_patch = GeneratedPatch(
                agent_run_id=self._agent_run_id,
                patch_text=patch_text,
                changed_files=changed_files,
            )
            self._db.add(generated_patch)
        else:
            generated_patch.patch_text = patch_text
            generated_patch.changed_files = changed_files

        self._db.commit()
        self._db.refresh(generated_patch)
        self._log_event(
            "generated_patch_stored",
            {
                "generated_patch_id": str(generated_patch.id),
                "changed_files": changed_files,
                "stats": _stats_payload(stats),
            },
        )
        return generated_patch

    def list_changed_files_from_patch(self, patch_text: str) -> list[str]:
        self._validate_patch_size(patch_text)
        self._reject_binary_patch(patch_text)

        changed_files: set[str] = set()
        in_hunk = False
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                in_hunk = False
                for raw_path in _diff_git_paths(line):
                    changed_files.add(self._normalize_patch_path(raw_path))
                continue
            if line.startswith("@@ "):
                in_hunk = True
                continue
            if not in_hunk and line.startswith(("--- ", "+++ ")):
                raw_path = _first_patch_path_token(line[4:])
                if raw_path != "/dev/null":
                    changed_files.add(self._normalize_patch_path(raw_path))
                continue
            for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
                if not in_hunk and line.startswith(prefix):
                    changed_files.add(self._normalize_patch_path(line[len(prefix) :]))
                    break

        changed_files_list = sorted(changed_files)
        self._validate_changed_file_count(changed_files_list)
        return changed_files_list

    def calculate_patch_size_statistics(
        self,
        patch_text: str,
        changed_files: list[str] | None = None,
    ) -> PatchSizeStats:
        return calculate_patch_size_statistics(patch_text, changed_files)

    def _current_changed_files(self) -> list[str]:
        tracked = _split_git_lines(self._run_git(["diff", "--name-only", "--"]).stdout)
        untracked = self._untracked_files()
        return self._sanitize_changed_files([*tracked, *untracked])

    def _untracked_files(self) -> list[str]:
        return _split_git_lines(
            self._run_git(["ls-files", "--others", "--exclude-standard"]).stdout
        )

    def _new_file_patch(self, relative_path: str) -> str:
        path = self._resolve_workspace_file(relative_path, must_exist=True)
        if not path.is_file():
            raise PatchSafetyError("Only file patches are supported.")
        if path.stat().st_size > self._max_patch_bytes:
            raise PatchSafetyError(f"Patch exceeds maximum size of {self._max_patch_bytes} bytes.")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PatchSafetyError("Binary patches are not supported.") from exc

        diff_lines = list(
            difflib.unified_diff(
                [],
                content.splitlines(),
                fromfile="/dev/null",
                tofile=f"b/{relative_path}",
                lineterm="",
            )
        )
        return "\n".join(
            [
                f"diff --git a/{relative_path} b/{relative_path}",
                "new file mode 100644",
                "index 0000000..0000000",
                *diff_lines,
            ]
        )

    def _resolve_workspace_file(self, relative_path: str, *, must_exist: bool) -> Path:
        normalized_path = self._normalize_patch_path(relative_path)
        candidate = (self._workspace_path / Path(*PurePosixPath(normalized_path).parts)).resolve(
            strict=must_exist
        )
        if not candidate.is_relative_to(self._workspace_path):
            raise PatchSafetyError("Patch path resolves outside the workspace.")
        return candidate

    def _sanitize_changed_files(self, changed_files: list[str]) -> list[str]:
        normalized = sorted({self._normalize_patch_path(relative_path) for relative_path in changed_files})
        self._validate_changed_file_count(normalized)
        return normalized

    def _normalize_patch_path(self, raw_path: str) -> str:
        if not isinstance(raw_path, str):
            raise PatchSafetyError("Patch path must be a string.")
        if "\x00" in raw_path:
            raise PatchSafetyError("Patch path must not contain null bytes.")

        value = raw_path.strip()
        if value in {"", "/dev/null"}:
            raise PatchSafetyError("Patch path is missing.")

        windows_path = PureWindowsPath(value)
        if windows_path.is_absolute() or windows_path.drive:
            raise PatchSafetyError("Patch paths must be relative to the workspace.")

        value = value.replace("\\", "/")
        if value.startswith(("a/", "b/")):
            value = value[2:]

        relative_path = PurePosixPath(value)
        if relative_path.is_absolute():
            raise PatchSafetyError("Patch paths must be relative to the workspace.")
        if any(part == ".." for part in relative_path.parts):
            raise PatchSafetyError("Patch path traversal is not allowed.")

        parts = [part for part in relative_path.parts if part not in {"", "."}]
        if not parts:
            raise PatchSafetyError("Patch path is missing.")

        normalized = PurePosixPath(*parts)
        self._reject_protected_gold_path(normalized)
        candidate = (self._workspace_path / Path(*normalized.parts)).resolve(strict=False)
        if not candidate.is_relative_to(self._workspace_path):
            raise PatchSafetyError("Patch path resolves outside the workspace.")
        return normalized.as_posix()

    def _reject_protected_gold_path(self, relative_path: PurePosixPath) -> None:
        parts = [part.lower() for part in relative_path.parts if part not in {"", "."}]
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
            raise PatchSafetyError("Patches cannot touch hidden gold solution files.")
        if len(parts) >= 2 and parts[0] == ".benchmark" and parts[1] == "gold":
            raise PatchSafetyError("Patches cannot touch hidden gold solution files.")

    def _validate_patch_limits(self, patch_text: str, changed_files: list[str]) -> None:
        self._validate_patch_size(patch_text)
        self._validate_changed_file_count(changed_files)

    def _validate_patch_size(self, patch_text: str) -> None:
        if len(patch_text.encode("utf-8")) > self._max_patch_bytes:
            raise PatchSafetyError(f"Patch exceeds maximum size of {self._max_patch_bytes} bytes.")

    def _validate_changed_file_count(self, changed_files: list[str]) -> None:
        if len(changed_files) > self._max_changed_files:
            raise PatchSafetyError(
                f"Patch changes more than {self._max_changed_files} files."
            )

    def _reject_binary_patch(self, patch_text: str) -> None:
        for line in patch_text.splitlines():
            if (
                line == "GIT binary patch"
                or line.startswith(("Binary files ", "literal ", "delta "))
            ):
                raise PatchSafetyError("Binary patches are not supported.")

    def _ensure_run_allows_patch_application(self) -> None:
        if self._agent_run.status not in PATCH_APPLY_ALLOWED_STATUSES:
            allowed = ", ".join(sorted(PATCH_APPLY_ALLOWED_STATUSES))
            raise PatchSafetyError(
                f"Patch application is only allowed for runs in these states: {allowed}."
            )

    def _ensure_git_workspace(self) -> None:
        completed = self._run_git(["rev-parse", "--is-inside-work-tree"])
        if completed.stdout.strip() != "true":
            raise PatchWorkspaceError("Agent run workspace is not a Git work tree.")

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self._workspace_path,
                text=True,
                capture_output=True,
                timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PatchWorkspaceError("Git command timed out.") from exc
        if completed.returncode != 0:
            raise PatchWorkspaceError(completed.stderr.strip() or "Git command failed.")
        return completed

    def _run_git_with_input(
        self,
        args: list[str],
        patch_text: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self._workspace_path,
                input=patch_text,
                text=True,
                capture_output=True,
                timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PatchWorkspaceError("Git patch command timed out.") from exc

    def _log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=self._agent_run_id,
                event_type=event_type,
                payload_json=payload,
            )
        )
        self._db.commit()


def _diff_git_paths(line: str) -> list[str]:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise PatchSafetyError("Invalid diff header.") from exc
    if len(parts) < 4:
        raise PatchSafetyError("Invalid diff header.")
    return [parts[2], parts[3]]


def _first_patch_path_token(value: str) -> str:
    value = value.strip()
    if "\t" in value:
        value = value.split("\t", 1)[0].strip()
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise PatchSafetyError("Invalid patch path.") from exc
    if not parts:
        raise PatchSafetyError("Patch path is missing.")
    return parts[0]


def _split_git_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def calculate_patch_size_statistics(
    patch_text: str,
    changed_files: list[str] | None = None,
) -> PatchSizeStats:
    additions = 0
    deletions = 0
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1

    return PatchSizeStats(
        size_bytes=len(patch_text.encode("utf-8")),
        changed_files_count=len(changed_files or []),
        additions=additions,
        deletions=deletions,
    )


def _stats_payload(stats: PatchSizeStats) -> dict[str, int]:
    return {
        "size_bytes": stats.size_bytes,
        "changed_files_count": stats.changed_files_count,
        "additions": stats.additions,
        "deletions": stats.deletions,
    }
