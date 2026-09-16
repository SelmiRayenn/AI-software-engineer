from __future__ import annotations

import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.repository_indexing.workspace import protected_path
from app.sandbox.workspace import SandboxWorkspaceManager

HIDDEN_EVAL_DIRECTORY = ".benchmark-hidden-eval"


def validate_payload_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or PureWindowsPath(value).drive
        or any(part.rstrip(". ") != part for part in path.parts)
    ):
        raise ValueError("Hidden file paths must be relative POSIX paths without traversal.")
    return path.as_posix()


def _copy_regular_file(source: str, destination: str) -> str:
    path = Path(source)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Hidden evaluation snapshot requires regular repository files.")
    return shutil.copy2(source, destination)


def _excluded_entries(directory: str, names: list[str]) -> list[str]:
    excluded = []
    for name in names:
        path = Path(directory) / name
        if name in {
            ".git",
            "__pycache__",
            ".pytest_cache",
            HIDDEN_EVAL_DIRECTORY,
        } or protected_path(PurePosixPath(name)):
            excluded.append(name)
        elif path.is_symlink() or path.is_junction():
            raise ValueError("Hidden evaluation snapshot cannot contain links or junctions.")
    return excluded


@contextmanager
def hidden_workspace(source: Path, files: dict[str, str]) -> Iterator[Path]:
    """Keep evaluation payloads out of every agent workspace, including retained ones."""
    manager = SandboxWorkspaceManager(retain_workspaces=False)
    if manager.workspace_root.is_relative_to(source):
        raise ValueError("Evaluation workspace root must be outside the agent repository.")
    metadata = manager.create_workspace(prefix="hidden-eval")
    try:
        shutil.copytree(
            source,
            metadata.repo_path,
            copy_function=_copy_regular_file,
            ignore=_excluded_entries,
        )
        staging = metadata.repo_path / HIDDEN_EVAL_DIRECTORY
        staging.mkdir(mode=0o700)
        for name, content in files.items():
            destination = staging / validate_payload_path(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Reject aliases and prefix collisions, even for records created directly in Python.
            with destination.open("x", encoding="utf-8") as target:
                target.write(content)
        yield metadata.repo_path
    finally:
        if metadata.workspace_path.is_symlink() or metadata.workspace_path.is_junction():
            raise ValueError("Evaluation workspace was replaced; refusing unsafe cleanup.")
        manager.cleanup_workspace(metadata)
