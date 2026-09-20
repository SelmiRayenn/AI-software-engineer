from __future__ import annotations

import io
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound

from app.core.config import settings
from app.sandbox.workspace import (
    SandboxWorkspaceError,
    SandboxWorkspaceManager,
    SandboxWorkspaceMetadata,
)
from app.schemas.sandbox import SandboxCommandResult, SandboxRunRequest, SandboxRunResponse

CONTAINER_REPO_DIR = "/workspace/repo"
DOCKER_UNAVAILABLE_MESSAGE = (
    "Docker is unavailable. Ensure Docker Desktop or the Docker daemon is running and the "
    "backend can access the Docker socket."
)


class DockerUnavailableError(RuntimeError):
    pass


class DockerSandboxRunner:
    def __init__(self, *, workspace_manager: SandboxWorkspaceManager | None = None) -> None:
        self._workspace_manager = workspace_manager or SandboxWorkspaceManager()

    def run(self, request: SandboxRunRequest) -> SandboxRunResponse:
        image = settings.sandbox_image
        command_timeout = self._command_timeout(request.command_timeout_seconds)
        network_enabled = (
            request.network_enabled
            if request.network_enabled is not None
            else settings.sandbox_network_enabled
        )

        workspace = self._workspace_manager.create_workspace(prefix="sandbox")
        response: SandboxRunResponse | None = None

        try:
            clone_result = self._run_host_command(
                args=[
                    "git",
                    "clone",
                    "--no-checkout",
                    "--",
                    request.repository_url,
                    str(workspace.repo_path),
                ],
                command="git clone --no-checkout <repository> repo",
                phase="clone",
                cwd=workspace.workspace_path,
                timeout_seconds=settings.sandbox_clone_timeout_seconds,
            )
            if not clone_result.passed:
                response = self._response(
                    status="clone_failed",
                    workspace=workspace,
                    request=request,
                    image=image,
                    network_enabled=network_enabled,
                    command_timeout=command_timeout,
                    clone_result=clone_result,
                )
                return response

            checkout_result = self._run_host_command(
                args=[
                    "git",
                    "-C",
                    str(workspace.repo_path),
                    "checkout",
                    "--detach",
                    request.base_commit,
                ],
                command=f"git checkout --detach {request.base_commit}",
                phase="checkout",
                cwd=workspace.workspace_path,
                timeout_seconds=settings.sandbox_clone_timeout_seconds,
            )
            if not checkout_result.passed:
                response = self._response(
                    status="checkout_failed",
                    workspace=workspace,
                    request=request,
                    image=image,
                    network_enabled=network_enabled,
                    command_timeout=command_timeout,
                    clone_result=clone_result,
                    checkout_result=checkout_result,
                )
                return response

            container = None
            setup_results: list[SandboxCommandResult] = []
            test_results: list[SandboxCommandResult] = []

            try:
                client = self._docker_client()
                self._ensure_image(client, image)
                container = client.containers.create(
                    image=image,
                    command=["sh", "-lc", "while true; do sleep 3600; done"],
                    name=f"agent-benchmark-sandbox-{workspace.workspace_id[:16]}",
                    detach=True,
                    privileged=False,
                    network_disabled=not network_enabled,
                    mem_limit=settings.sandbox_memory_limit,
                    nano_cpus=int(settings.sandbox_cpu_limit * 1_000_000_000),
                    pids_limit=settings.sandbox_pids_limit,
                    security_opt=["no-new-privileges:true"],
                    cap_drop=["ALL"],
                    labels={
                        "agent-benchmark.sandbox": "true",
                        "agent-benchmark.workspace_id": workspace.workspace_id,
                    },
                )
                container.start()
                container.put_archive("/", self._repo_archive(workspace.repo_path))

                for command in request.setup_commands:
                    result = self._run_container_command(
                        container=container,
                        command=command,
                        phase="setup",
                        timeout_seconds=command_timeout,
                    )
                    setup_results.append(result)
                    if not result.passed:
                        response = self._response(
                            status="setup_failed",
                            workspace=workspace,
                            request=request,
                            image=image,
                            network_enabled=network_enabled,
                            command_timeout=command_timeout,
                            clone_result=clone_result,
                            checkout_result=checkout_result,
                            setup_results=setup_results,
                        )
                        return response

                stop_tests = False
                for _ in range(request.test_repetitions):
                    for command in request.test_commands:
                        result = self._run_container_command(
                            container=container,
                            command=command,
                            phase="test",
                            timeout_seconds=command_timeout,
                        )
                        test_results.append(result)
                        if result.timed_out or (
                            request.stop_on_first_test_failure and not result.passed
                        ):
                            stop_tests = True
                            break
                    if stop_tests:
                        break

                status = (
                    "passed" if all(result.passed for result in test_results) else "tests_failed"
                )
                response = self._response(
                    status=status,
                    workspace=workspace,
                    request=request,
                    image=image,
                    network_enabled=network_enabled,
                    command_timeout=command_timeout,
                    clone_result=clone_result,
                    checkout_result=checkout_result,
                    setup_results=setup_results,
                    test_results=test_results,
                )
                return response
            except DockerUnavailableError as exc:
                response = self._response(
                    status="sandbox_error",
                    workspace=workspace,
                    request=request,
                    image=image,
                    network_enabled=network_enabled,
                    command_timeout=command_timeout,
                    clone_result=clone_result,
                    checkout_result=checkout_result,
                    setup_results=setup_results,
                    test_results=test_results,
                    error_code="docker_unavailable",
                    error=str(exc),
                )
                return response
            except (DockerException, OSError) as exc:
                response = self._response(
                    status="sandbox_error",
                    workspace=workspace,
                    request=request,
                    image=image,
                    network_enabled=network_enabled,
                    command_timeout=command_timeout,
                    clone_result=clone_result,
                    checkout_result=checkout_result,
                    setup_results=setup_results,
                    test_results=test_results,
                    error_code="sandbox_execution_error",
                    error=str(exc),
                )
                return response
            finally:
                if container is not None:
                    try:
                        container.remove(force=True)
                    except DockerException:
                        pass
        finally:
            if response is not None:
                try:
                    response.workspace_retained = self._workspace_manager.cleanup_workspace(
                        workspace
                    )
                except (OSError, SandboxWorkspaceError) as exc:
                    response.workspace_retained = True
                    response.cleanup_error = str(exc)

    def _command_timeout(self, requested_timeout: int | None) -> int:
        timeout = requested_timeout or settings.sandbox_command_timeout_seconds
        return min(timeout, settings.sandbox_max_command_timeout_seconds)

    def _docker_client(self) -> docker.DockerClient:
        try:
            client = docker.from_env()
            client.ping()
        except DockerException as exc:
            raise DockerUnavailableError(DOCKER_UNAVAILABLE_MESSAGE) from exc
        return client

    def _ensure_image(self, client: docker.DockerClient, image: str) -> None:
        try:
            client.images.get(image)
        except ImageNotFound:
            if not settings.sandbox_pull_image:
                raise
            client.images.pull(image)

    def _run_host_command(
        self,
        args: list[str],
        command: str,
        phase: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> SandboxCommandResult:
        start = time.monotonic()
        try:
            completed = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            stdout, stdout_truncated = self._limit_log(completed.stdout)
            stderr, stderr_truncated = self._limit_log(completed.stderr)
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=completed.returncode == 0,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_seconds=self._duration(start),
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = self._limit_log(self._coerce_output(exc.stdout))
            stderr, stderr_truncated = self._limit_log(self._coerce_output(exc.stderr))
            timeout_message = f"Command exceeded timeout of {timeout_seconds} seconds."
            stderr = f"{stderr}\n{timeout_message}".strip()
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=False,
                timed_out=True,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_seconds=self._duration(start),
            )
        except OSError as exc:
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=False,
                stderr=str(exc),
                duration_seconds=self._duration(start),
            )

    def _run_container_command(
        self,
        container: Any,
        command: str,
        phase: str,
        timeout_seconds: int,
    ) -> SandboxCommandResult:
        start = time.monotonic()
        result_box: dict[str, Any] = {}
        error_box: dict[str, BaseException] = {}

        def execute() -> None:
            try:
                result_box["result"] = container.exec_run(
                    ["sh", "-lc", command],
                    stdout=True,
                    stderr=True,
                    demux=True,
                    workdir=CONTAINER_REPO_DIR,
                )
            except DockerException as exc:
                error_box["error"] = exc

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        thread.join(timeout_seconds)

        if thread.is_alive():
            try:
                container.kill()
            except DockerException:
                pass
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=False,
                timed_out=True,
                stderr=f"Command exceeded timeout of {timeout_seconds} seconds.",
                duration_seconds=self._duration(start),
            )

        if "error" in error_box:
            return SandboxCommandResult(
                phase=phase,
                command=command,
                passed=False,
                stderr=str(error_box["error"]),
                duration_seconds=self._duration(start),
            )

        exec_result = result_box["result"]
        stdout_bytes, stderr_bytes = self._demux_output(exec_result.output)
        stdout, stdout_truncated = self._limit_log(stdout_bytes.decode("utf-8", errors="replace"))
        stderr, stderr_truncated = self._limit_log(stderr_bytes.decode("utf-8", errors="replace"))
        return SandboxCommandResult(
            phase=phase,
            command=command,
            passed=exec_result.exit_code == 0,
            exit_code=exec_result.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_seconds=self._duration(start),
        )

    def _repo_archive(self, repo_path: Path) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(repo_path, arcname="workspace/repo")
        stream.seek(0)
        return stream.read()

    def _demux_output(self, output: Any) -> tuple[bytes, bytes]:
        if isinstance(output, tuple):
            stdout, stderr = output
            return stdout or b"", stderr or b""
        if isinstance(output, bytes):
            return output, b""
        return b"", b""

    def _limit_log(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8", errors="replace")
        max_output_bytes = settings.sandbox_max_output_bytes
        if len(encoded) <= max_output_bytes:
            return value, False

        truncated = encoded[-max_output_bytes:].decode("utf-8", errors="replace")
        return f"[truncated to last {max_output_bytes} bytes]\n{truncated}", True

    def _coerce_output(self, output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return output

    def _duration(self, start: float) -> float:
        return round(time.monotonic() - start, 3)

    def _response(
        self,
        status: str,
        workspace: SandboxWorkspaceMetadata,
        request: SandboxRunRequest,
        image: str,
        network_enabled: bool,
        command_timeout: int,
        clone_result: SandboxCommandResult,
        checkout_result: SandboxCommandResult | None = None,
        setup_results: list[SandboxCommandResult] | None = None,
        test_results: list[SandboxCommandResult] | None = None,
        error_code: str | None = None,
        error: str | None = None,
    ) -> SandboxRunResponse:
        return SandboxRunResponse(
            status=status,
            workspace_id=workspace.workspace_id,
            workspace_path=str(workspace.workspace_path),
            workspace_root=str(workspace.workspace_root),
            workspace_retained=workspace.retain,
            repository_url=request.repository_url,
            base_commit=request.base_commit,
            image=image,
            network_enabled=network_enabled,
            command_timeout_seconds=command_timeout,
            clone_result=clone_result,
            checkout_result=checkout_result,
            setup_results=setup_results or [],
            test_results=test_results or [],
            error_code=error_code,
            error=error,
        )
