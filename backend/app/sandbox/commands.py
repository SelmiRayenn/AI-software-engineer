from __future__ import annotations

import hashlib
import io
import tarfile
import time
import uuid
from pathlib import Path
from typing import Self

from docker.errors import DockerException

from app.core.config import settings
from app.sandbox.network import SandboxNetworkPolicyError, resolve_network_policy
from app.sandbox.runner import DockerSandboxRunner, DockerUnavailableError
from app.schemas.sandbox import SandboxCommandResult, SandboxNetworkDecision


class DockerCommandSession:
    """Run-scoped container filesystem, with no host mounts or inherited host environment."""

    def __init__(self, *, requested_mode: str | None = None) -> None:
        self.requested_mode = requested_mode
        self.runner = DockerSandboxRunner()
        self.client = None
        self.container = None
        self.mode: str | None = None
        self.image = settings.sandbox_image
        self.images: list[str] = []
        self.workspaces: dict[Path, tuple[str, dict[str, str]]] = {}
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        failed = False
        try:
            if self.container is not None:
                self.container.remove(force=True)
                self.container = None
        except DockerException:
            failed = True
        finally:
            if self.client is not None:
                for image_id in reversed(self.images):
                    try:
                        self.client.images.remove(image_id, noprune=True)
                    except DockerException:
                        failed = True
                self.images.clear()
                self.client.close()
                self.client = None
        if failed:
            raise DockerException("Docker container or temporary image cleanup failed.")

    def execute(
        self, *, workspace_path: Path, command: str, phase: str, timeout_seconds: int
    ) -> SandboxCommandResult:
        started = time.monotonic()
        policy = None
        try:
            policy = resolve_network_policy(phase, requested_mode=self.requested_mode)
            if self.closed:
                raise RuntimeError("Sandbox command session is closed; create a new session.")
            self._ensure_container(policy)
            workdir = self._synchronize(workspace_path)
            result = self.runner._run_container_command(
                container=self.container,
                command=command,
                phase=phase,
                timeout_seconds=self.runner._command_timeout(timeout_seconds),
                workdir=workdir,
            )
            result.network_policy = policy
            if result.timed_out:
                result.exit_code = 124
                # A timed-out container is killed, never reused for another command.
                self.closed = True
            return result
        except (DockerException, OSError, RuntimeError, ValueError) as exc:
            # Never reuse a partially configured container after an isolation/setup error.
            self.closed = True
            code = (
                "network_mode_unsupported"
                if isinstance(exc, SandboxNetworkPolicyError)
                else "docker_unavailable"
                if isinstance(exc, DockerUnavailableError)
                else "sandbox_execution_error"
            )
            message = (
                str(exc)
                if isinstance(exc, (SandboxNetworkPolicyError, DockerUnavailableError))
                else "Docker command execution failed; inspect the sandbox configuration."
            )
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=False,
                exit_code=1,
                stderr=message,
                duration_seconds=round(time.monotonic() - started, 3),
                network_policy=policy,
                error_code=code,
            )

    def _ensure_container(self, policy: SandboxNetworkDecision) -> None:
        mode = policy.effective_network_mode
        if self.client is None:
            self.client = self.runner._docker_client()
            if self.client.info().get("OSType") != "linux":
                raise SandboxNetworkPolicyError(
                    "Sandbox network isolation requires Linux containers; this Docker "
                    "engine does not support the requested execution policy."
                )
            self.runner._ensure_image(self.client, self.image)
        if self.container is not None and self.mode != mode:
            # Stop setup processes before changing policy. Only filesystem state is carried
            # into a new container, so setup network sockets cannot survive into test phases.
            self.container.stop(timeout=1)
            snapshot = self.container.commit()
            self.images.append(snapshot.id)
            self.image = snapshot.id
            self.container.remove(force=True)
            self.container = None
        if self.container is None:
            try:
                self.container = self.client.containers.create(
                    image=self.image,
                    command=["sh", "-lc", "while true; do sleep 3600; done"],
                    detach=True,
                    privileged=False,
                    network_mode=mode,
                    mem_limit=settings.sandbox_memory_limit,
                    nano_cpus=int(settings.sandbox_cpu_limit * 1_000_000_000),
                    pids_limit=settings.sandbox_pids_limit,
                    security_opt=["no-new-privileges:true"],
                    cap_drop=["ALL"],
                    labels={"agent-benchmark.sandbox": "true"},
                )
                self.container.start()
            except DockerException as exc:
                raise SandboxNetworkPolicyError(
                    f"Docker could not create a sandbox with network mode '{mode}'; "
                    "verify Linux container and network support. No fallback was used."
                ) from exc
            self.mode = mode

    def _synchronize(self, workspace_path: Path) -> str:
        if workspace_path.is_symlink() or workspace_path.is_junction():
            raise ValueError("Sandbox synchronization does not allow workspace links.")
        root = workspace_path.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace must be a directory.")
        destination, previous = self.workspaces.get(
            root, (f"/workspace/repo-{uuid.uuid4().hex}", {})
        )
        current: dict[str, str] = {}
        files: dict[str, Path] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Sandbox synchronization does not allow workspace links.")
            if path.is_file():
                if not path.resolve().is_relative_to(root):
                    raise ValueError("Sandbox file is outside the workspace.")
                name = path.relative_to(root).as_posix()
                with path.open("rb") as source:
                    current[name] = hashlib.file_digest(source, "sha256").hexdigest()
                files[name] = path
        removed = sorted(set(previous) - set(current))
        if removed:
            result = self.container.exec_run(
                ["rm", "-rf", "--", *[f"{destination}/{name}" for name in removed]]
            )
            if result.exit_code != 0:
                raise RuntimeError("Sandbox synchronization failed.")
        # Only host changes are overlaid, preserving dependency installs/setup outputs in Docker.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            directory = tarfile.TarInfo(destination.lstrip("/"))
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            for name, digest in current.items():
                if previous.get(name) != digest:
                    archive.add(
                        files[name], arcname=f"{destination.lstrip('/')}/{name}", recursive=False
                    )
        if not self.container.put_archive("/", stream.getvalue()):
            raise RuntimeError("Sandbox workspace upload failed.")
        self.workspaces[root] = destination, current
        return destination
