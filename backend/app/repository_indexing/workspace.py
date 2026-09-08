from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.repository_indexing.errors import (
    IndexLimitError,
    IndexSafetyError,
    IndexWorkspaceError,
    SkippedFile,
)
from app.sandbox.workspace import WORKSPACE_METADATA_FILENAME

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "dist",
        "build",
    }
)
GOLD_NAMES = frozenset({"gold", "gold_patch", "gold_patches", "gold_solution", "benchmark_gold"})


def protected_path(path: PurePosixPath) -> bool:
    for part in path.parts:
        name = part.lower()
        stem = name.lstrip(".").replace("-", "_").split(".")[0]
        if stem in GOLD_NAMES or name in {".benchmark", WORKSPACE_METADATA_FILENAME}:
            return True
        if name == ".env" or name.startswith(".env."):
            return True
    return False


def searchable_path(value: str) -> bool:
    try:
        path = _relative_path(value)
    except IndexSafetyError:
        return False
    return (
        bool(path.parts)
        and not protected_path(path)
        and not any(part.lower() in IGNORED_DIRECTORIES for part in path.parts)
    )


def _relative_path(value: str) -> PurePosixPath:
    if "\x00" in value or "\\" in value or PureWindowsPath(value).drive:
        raise IndexSafetyError("Index paths must use relative POSIX paths without traversal.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise IndexSafetyError("Index paths must be relative and cannot contain '..'.")
    return path


def _checked_path(root: Path, relative: PurePosixPath) -> Path:
    candidate = root
    for part in (".", *relative.parts):
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise IndexSafetyError("Indexing links or junctions is not allowed.")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise IndexSafetyError("Index path resolves outside the workspace.")
    return candidate


def _read_regular_file(root: Path, relative: PurePosixPath, max_bytes: int) -> bytes:
    candidate = _checked_path(root, relative)
    before = candidate.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SkippedFile("unsafe_entries")
    if before.st_size > max_bytes:
        raise SkippedFile("oversized_files")

    with ExitStack() as stack:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        # POSIX descriptor-relative opens prevent parent-link swaps between check and read.
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent_fd = os.open(root, directory_flags)
            stack.callback(os.close, parent_fd)
            for part in relative.parts[:-1]:
                parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
            fd = os.open(relative.name, flags | os.O_NOFOLLOW, dir_fd=parent_fd)
        else:
            fd = os.open(candidate, flags)
        stack.callback(os.close, fd)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise IndexSafetyError("Workspace file changed while opening it.")
        if opened.st_size > max_bytes:
            raise SkippedFile("oversized_files")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        checked = _checked_path(root, relative).stat()
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or (
            checked.st_dev,
            checked.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise IndexWorkspaceError(
                "Workspace changed during indexing; retry on a quiet checkout."
            )
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise SkippedFile("oversized_files")
    return data


class IndexWorkspace:
    def __init__(self, *, root: Path, workspace_id: str | None, workspace_path: str | None):
        self.root = root
        if not workspace_id or not workspace_path:
            raise IndexWorkspaceError("Agent run does not have a prepared sandbox workspace.")
        if not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            raise IndexSafetyError("Agent run workspace ID is not a managed sandbox ID.")
        path = Path(workspace_path)
        if not path.is_absolute() or ".." in PurePosixPath(workspace_path.replace("\\", "/")).parts:
            raise IndexSafetyError("Agent run workspace path must be absolute without traversal.")
        expected = root / f"agent-run-{workspace_id}" / "repo"
        if path != expected:
            raise IndexSafetyError(
                "Agent run workspace path does not match its managed sandbox ID."
            )
        self.relative = PurePosixPath(expected.relative_to(root).as_posix())
        try:
            self.path = _checked_path(root, self.relative)
            if not self.path.is_dir():
                raise IndexWorkspaceError("Agent run workspace must be a directory.")
            metadata_path = self.relative.parent / WORKSPACE_METADATA_FILENAME
            metadata = json.loads(_read_regular_file(root, metadata_path, 16_384))
        except (OSError, ValueError, SkippedFile) as exc:
            raise IndexWorkspaceError(
                "Managed workspace or metadata is unavailable. Retain a workspace before indexing."
            ) from exc
        expected_metadata = {
            "workspace_id": workspace_id,
            "workspace_root": str(root),
            "workspace_path": str(expected.parent),
            "repo_path": str(expected),
        }
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected_metadata.items()
        ):
            raise IndexSafetyError("Sandbox workspace metadata does not match the agent run.")

    def read_file(self, relative_path: str, max_bytes: int) -> bytes:
        relative = _relative_path(relative_path)
        if protected_path(relative) or any(
            p.lower() in IGNORED_DIRECTORIES for p in relative.parts
        ):
            raise IndexSafetyError("Indexing protected or ignored paths is not allowed.")
        return _read_regular_file(self.root, self.relative / relative, max_bytes)

    def files(self, skipped: Counter[str], *, max_entries: int) -> Iterator[str]:
        pending = [PurePosixPath(".")]
        visited = 0
        while pending:
            directory = pending.pop()
            current = _checked_path(self.root, self.relative / directory)
            entries = []
            with os.scandir(current) as iterator:
                for entry in iterator:
                    visited += 1
                    if visited > max_entries:
                        raise IndexLimitError("Repository exceeds the index entry limit.")
                    entries.append(entry)
            directories = []
            for entry in sorted(entries, key=lambda item: item.name):
                relative = directory / entry.name
                try:
                    _relative_path(relative.as_posix())
                    if protected_path(relative):
                        skipped["protected_entries"] += 1
                        continue
                    if entry.name.lower() in IGNORED_DIRECTORIES:
                        skipped["ignored_entries"] += 1
                        continue
                    candidate = _checked_path(self.root, self.relative / relative)
                    if candidate.is_dir():
                        directories.append(relative)
                    elif candidate.is_file():
                        yield relative.as_posix()
                    else:
                        skipped["unsafe_entries"] += 1
                except IndexSafetyError:
                    skipped["unsafe_entries"] += 1
            pending.extend(reversed(directories))
