from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from docker.errors import DockerException

from app.agents.orchestrator import GitSandboxWorkspacePreparer
from app.sandbox.runner import DockerSandboxRunner
from app.sandbox.workspace import SandboxWorkspaceManager, SandboxWorkspaceSafetyError
from app.schemas.sandbox import SandboxCommandResult, SandboxRunRequest


def test_unique_workspace_id_creation(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)

    first = manager.create_workspace()
    second = manager.create_workspace()

    assert first.workspace_id != second.workspace_id
    assert first.workspace_path != second.workspace_path
    assert first.metadata_path.exists()
    metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
    assert metadata["workspace_id"] == first.workspace_id


def test_workspace_path_remains_under_workspace_root(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)

    workspace = manager.create_workspace(prefix="agent-run")

    assert workspace.workspace_path.is_relative_to(tmp_path.resolve())
    assert workspace.repo_path.is_relative_to(tmp_path.resolve())


def test_cleanup_removes_workspace_when_retention_disabled(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)
    workspace = manager.create_workspace()
    (workspace.workspace_path / "file.txt").write_text("temporary", encoding="utf-8")

    retained = manager.cleanup_workspace(workspace)

    assert retained is False
    assert not workspace.workspace_path.exists()


def test_cleanup_keeps_workspace_when_retention_enabled(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)
    workspace = manager.create_workspace()
    (workspace.workspace_path / "file.txt").write_text("temporary", encoding="utf-8")

    retained = manager.cleanup_workspace(workspace)

    assert retained is True
    assert workspace.workspace_path.exists()


def test_unsafe_cleanup_path_is_rejected(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path / "root", retain_workspaces=False)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(SandboxWorkspaceSafetyError):
        manager.cleanup_path(outside)

    assert outside.exists()


def test_workspace_root_cleanup_is_rejected(tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)

    with pytest.raises(SandboxWorkspaceSafetyError):
        manager.cleanup_path(tmp_path)

    assert tmp_path.exists()


def test_agent_workspace_preparer_cleans_up_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)
    preparer = GitSandboxWorkspacePreparer(workspace_manager=manager)
    monkeypatch.setattr("app.agents.orchestrator._run_workspace_command", fake_git_command)

    with preparer.prepare(
        repository_url="https://github.com/example/project",
        base_commit="1111111",
        command_timeout_seconds=10,
    ) as workspace:
        workspace_root = workspace.workspace_path
        assert workspace_root is not None
        assert workspace.path.exists()
        assert workspace.path.is_relative_to(tmp_path.resolve())

    assert workspace_root is not None
    assert not workspace_root.exists()


def test_agent_workspace_preparer_respects_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)
    preparer = GitSandboxWorkspacePreparer(workspace_manager=manager)
    monkeypatch.setattr("app.agents.orchestrator._run_workspace_command", fake_git_command)

    with preparer.prepare(
        repository_url="https://github.com/example/project",
        base_commit="1111111",
        command_timeout_seconds=10,
    ) as workspace:
        workspace_root = workspace.workspace_path
        assert workspace_root is not None

    assert workspace_root is not None
    assert workspace_root.exists()


def test_docker_unavailable_error_is_clear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)
    runner = DockerSandboxRunner(workspace_manager=manager)
    monkeypatch.setattr(runner, "_run_host_command", successful_host_command)
    monkeypatch.setattr("app.sandbox.runner.docker.from_env", raise_docker_unavailable)

    response = runner.run(
        SandboxRunRequest(
            repository_url="https://github.com/example/project",
            base_commit="1111111",
            setup_commands=[],
            test_commands=["pytest"],
        )
    )

    assert response.status == "sandbox_error"
    assert response.error_code == "docker_unavailable"
    assert "Docker is unavailable" in (response.error or "")
    assert response.workspace_retained is False
    assert not Path(response.workspace_path).exists()


def test_container_is_created_with_resource_and_privilege_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_client = FakeDockerClient()
    manager = SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)
    runner = DockerSandboxRunner(workspace_manager=manager)
    monkeypatch.setattr(runner, "_run_host_command", successful_host_command)
    monkeypatch.setattr(runner, "_repo_archive", lambda repo_path: b"archive")
    monkeypatch.setattr("app.sandbox.runner.docker.from_env", lambda: fake_client)
    monkeypatch.setattr("app.sandbox.runner.settings.sandbox_memory_limit", "512m")
    monkeypatch.setattr("app.sandbox.runner.settings.sandbox_cpu_limit", 0.5)

    response = runner.run(
        SandboxRunRequest(
            repository_url="https://github.com/example/project",
            base_commit="1111111",
            setup_commands=[],
            test_commands=["pytest"],
        )
    )

    create_kwargs = fake_client.containers.create_kwargs
    assert response.status == "passed"
    assert create_kwargs["privileged"] is False
    assert create_kwargs["mem_limit"] == "512m"
    assert create_kwargs["nano_cpus"] == 500_000_000
    assert create_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert create_kwargs["cap_drop"] == ["ALL"]


def test_runner_repeats_tests_and_stops_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    container = SequencedContainer([0, 1, 0])
    fake_client = FakeDockerClient(container)
    runner = DockerSandboxRunner(
        workspace_manager=SandboxWorkspaceManager(
            workspace_root=tmp_path, retain_workspaces=False
        )
    )
    monkeypatch.setattr(runner, "_run_host_command", successful_host_command)
    monkeypatch.setattr(runner, "_repo_archive", lambda repo_path: b"archive")
    monkeypatch.setattr("app.sandbox.runner.docker.from_env", lambda: fake_client)

    response = runner.run(
        SandboxRunRequest(
            repository_url="https://github.com/example/project",
            base_commit="1111111",
            setup_commands=[],
            test_commands=["pytest"],
            test_repetitions=3,
            stop_on_first_test_failure=True,
        )
    )

    assert response.status == "tests_failed"
    assert [result.passed for result in response.test_results] == [True, False]
    assert container.exec_count == 2


def test_host_command_timeout_is_enforced(tmp_path: Path) -> None:
    runner = DockerSandboxRunner(
        workspace_manager=SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)
    )

    result = runner._run_host_command(
        args=[sys.executable, "-c", "import time; time.sleep(2)"],
        command="sleep",
        phase="clone",
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.passed is False
    assert result.timed_out is True
    assert "exceeded timeout" in result.stderr


def test_output_limit_is_enforced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = DockerSandboxRunner(
        workspace_manager=SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=True)
    )
    monkeypatch.setattr("app.sandbox.runner.settings.sandbox_max_output_bytes", 20)

    output, truncated = runner._limit_log("x" * 120)

    assert truncated is True
    assert output.startswith("[truncated to last 20 bytes]")
    assert output.endswith("x" * 20)


def successful_host_command(
    *,
    args: list[str],
    command: str,
    phase: str,
    cwd: Path,
    timeout_seconds: int,
) -> SandboxCommandResult:
    del args, cwd, timeout_seconds
    return SandboxCommandResult(
        phase=phase,
        command=command,
        passed=True,
        exit_code=0,
        duration_seconds=0.01,
    )


def raise_docker_unavailable():
    raise DockerException("Cannot connect to Docker daemon")


def fake_git_command(args: list[str], *, cwd: Path, timeout_seconds: int) -> None:
    del cwd, timeout_seconds
    if "clone" in args:
        Path(args[-1]).mkdir(parents=True)


class FakeDockerClient:
    def __init__(self, container=None) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers(container)

    def ping(self) -> bool:
        return True


class FakeImages:
    def get(self, image: str) -> str:
        return image


class FakeContainers:
    def __init__(self, container=None) -> None:
        self.create_kwargs: dict[str, object] = {}
        self.container = container or FakeContainer()

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self.container


class FakeExecResult:
    exit_code = 0
    output = (b"ok\n", b"")


class FakeContainer:
    def start(self) -> None:
        pass

    def put_archive(self, path: str, data: bytes) -> None:
        del path, data

    def exec_run(self, *args, **kwargs) -> FakeExecResult:
        del args, kwargs
        return FakeExecResult()

    def remove(self, *, force: bool) -> None:
        del force


class SequencedContainer(FakeContainer):
    def __init__(self, exit_codes: list[int]) -> None:
        self.exit_codes = exit_codes
        self.exec_count = 0

    def exec_run(self, *args, **kwargs):
        del args, kwargs
        exit_code = self.exit_codes[self.exec_count]
        self.exec_count += 1
        return type(
            "SequencedExecResult",
            (),
            {"exit_code": exit_code, "output": (b"ok\n", b"" if exit_code == 0 else b"failed")},
        )()
