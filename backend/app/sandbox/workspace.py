from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings

WORKSPACE_METADATA_FILENAME = ".sandbox-workspace.json"


class SandboxWorkspaceError(RuntimeError):
    pass


class SandboxWorkspaceSafetyError(SandboxWorkspaceError):
    pass


@dataclass(frozen=True)
class SandboxWorkspaceMetadata:
    workspace_id: str
    workspace_root: Path
    workspace_path: Path
    repo_path: Path
    retain: bool
    created_at: datetime

    @property
    def metadata_path(self) -> Path:
        return self.workspace_path / WORKSPACE_METADATA_FILENAME

    def to_json(self) -> dict[str, str | bool]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_root": str(self.workspace_root),
            "workspace_path": str(self.workspace_path),
            "repo_path": str(self.repo_path),
            "retain": self.retain,
            "created_at": self.created_at.isoformat(),
        }


class SandboxWorkspaceManager:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        retain_workspaces: bool | None = None,
    ) -> None:
        self._workspace_root = _resolve_workspace_root(workspace_root)
        self._retain_workspaces = (
            settings.sandbox_retain_workspaces
            if retain_workspaces is None
            else retain_workspaces
        )

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def retain_workspaces(self) -> bool:
        return self._retain_workspaces

    def create_workspace(self, *, prefix: str = "sandbox") -> SandboxWorkspaceMetadata:
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            workspace_id = uuid.uuid4().hex
            workspace_path = (self._workspace_root / f"{prefix}-{workspace_id}").resolve(
                strict=False
            )
            self._ensure_under_root(workspace_path)
            try:
                workspace_path.mkdir(mode=0o700)
            except FileExistsError:
                continue

            metadata = SandboxWorkspaceMetadata(
                workspace_id=workspace_id,
                workspace_root=self._workspace_root,
                workspace_path=workspace_path,
                repo_path=workspace_path / "repo",
                retain=self._retain_workspaces,
                created_at=datetime.now(UTC),
            )
            self.write_metadata(metadata)
            return metadata

        raise SandboxWorkspaceError("Unable to create a unique sandbox workspace.")

    def write_metadata(self, metadata: SandboxWorkspaceMetadata) -> None:
        self._ensure_under_root(metadata.workspace_path)
        metadata.metadata_path.write_text(
            json.dumps(metadata.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def cleanup_workspace(self, metadata: SandboxWorkspaceMetadata) -> bool:
        if metadata.retain:
            return True
        self.cleanup_path(metadata.workspace_path)
        return False

    def cleanup_path(self, path: str | Path) -> None:
        target = Path(path).resolve(strict=False)
        self._ensure_under_root(target)
        if target == self._workspace_root:
            raise SandboxWorkspaceSafetyError("Refusing to delete the sandbox workspace root.")
        if not target.exists():
            return
        if not target.is_dir():
            raise SandboxWorkspaceSafetyError("Sandbox cleanup target must be a directory.")
        shutil.rmtree(target)

    def _ensure_under_root(self, path: Path) -> None:
        if not path.is_relative_to(self._workspace_root):
            raise SandboxWorkspaceSafetyError(
                f"Sandbox path {path} is outside workspace root {self._workspace_root}."
            )


def _resolve_workspace_root(workspace_root: str | Path | None = None) -> Path:
    configured_root = workspace_root if workspace_root is not None else settings.sandbox_workspace_root
    if configured_root:
        root = Path(configured_root).expanduser()
    else:
        root = Path(tempfile.gettempdir()) / "agent-benchmark-sandbox-workspaces"
    return root.resolve(strict=False)
