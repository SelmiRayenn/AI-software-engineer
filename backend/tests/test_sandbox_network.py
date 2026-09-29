from __future__ import annotations

import io
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import DockerException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.tools import AgentWorkspaceTools
from app.core.config import settings
from app.db.base import Base
from app.main import create_app
from app.models import AgentEvent, AgentRun, BenchmarkTask, Repository
from app.run_reports import AgentRunReportService
from app.run_traces import AgentRunTraceService
from app.sandbox.commands import DockerCommandSession
from app.sandbox.network import SandboxNetworkPolicyError, resolve_network_policy
from app.sandbox.runner import DockerSandboxRunner
from app.sandbox.workspace import SandboxWorkspaceManager
from app.schemas.sandbox import SandboxCommandResult
from app.test_execution import TestExecutionService as ExecutionService


@pytest.fixture(autouse=True)
def safe_settings(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_mode", "none")
    monkeypatch.setattr(settings, "sandbox_allow_network_during_setup", False)
    monkeypatch.setattr(settings, "sandbox_allow_network_during_tests", False)
    monkeypatch.setattr(settings, "sandbox_allowed_hosts", "")


@pytest.fixture
def docker_client(monkeypatch):
    client = Mock()
    client.info.return_value = {"OSType": "linux"}
    client.created = []
    client.options = []

    def create(**kwargs):
        container = Mock()
        container.exec_run.return_value = SimpleNamespace(exit_code=0, output=(b"ok\n", b""))
        container.commit.return_value = SimpleNamespace(id=f"snapshot-{len(client.created)}")
        container.put_archive.return_value = True
        client.created.append(container)
        client.options.append(kwargs)
        return container

    client.containers.create.side_effect = create
    monkeypatch.setattr("app.sandbox.runner.docker.from_env", lambda: client)
    return client


@pytest.mark.parametrize(
    "phase",
    [
        "setup",
        "test",
        "baseline",
        "post_patch",
        "hidden_eval",
        "lint",
        "format_check",
        "flakiness",
        "agent_test",
    ],
)
def test_every_phase_uses_no_network_by_default(phase, docker_client, tmp_path):
    with DockerCommandSession() as session:
        result = session.execute(
            workspace_path=tmp_path,
            command="python --version",
            phase=phase,
            timeout_seconds=10,
        )
    assert result.passed
    assert result.network_policy.effective_network_mode == "none"
    assert result.network_policy.network_exception is False
    options = docker_client.options[0]
    assert options["network_mode"] == "none"
    assert options["privileged"] is False
    assert options["cap_drop"] == ["ALL"]
    assert options["mem_limit"] == settings.sandbox_memory_limit
    assert options["nano_cpus"] > 0
    assert "volumes" not in options
    docker_client.created[0].remove.assert_called_once_with(force=True)


def test_setup_exception_does_not_leak_into_tests(monkeypatch, docker_client, tmp_path):
    monkeypatch.setattr(settings, "sandbox_allow_network_during_setup", True)
    with DockerCommandSession() as session:
        setup = session.execute(
            workspace_path=tmp_path, command="pip install pytest", phase="setup", timeout_seconds=10
        )
        baseline = session.execute(
            workspace_path=tmp_path, command="pytest", phase="baseline", timeout_seconds=10
        )
    assert setup.network_policy.effective_network_mode == "bridge"
    assert setup.network_policy.network_exception is True
    assert baseline.network_policy.effective_network_mode == "none"
    assert [item["network_mode"] for item in docker_client.options] == ["bridge", "none"]
    setup_container = docker_client.created[0]
    setup_container.stop.assert_called_once_with(timeout=1)
    setup_container.commit.assert_called_once()
    assert docker_client.options[1]["image"] == setup_container.commit.return_value.id
    docker_client.images.remove.assert_called_once_with(
        setup_container.commit.return_value.id, noprune=True
    )


@pytest.mark.parametrize("phase", ["setup", "post_patch", "agent_test", "hidden_eval"])
def test_request_and_legacy_global_flag_cannot_grant_network(monkeypatch, phase):
    monkeypatch.setattr(settings, "sandbox_network_enabled", True)
    assert resolve_network_policy(phase, requested_mode="bridge").effective_network_mode == "none"


def test_test_network_exception_requires_both_settings(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_allow_network_during_tests", True)
    assert resolve_network_policy("post_patch").effective_network_mode == "none"
    monkeypatch.setattr(settings, "sandbox_network_mode", "bridge")
    assert resolve_network_policy("post_patch").effective_network_mode == "bridge"
    assert (
        resolve_network_policy("post_patch", requested_mode="none").effective_network_mode == "none"
    )
    assert resolve_network_policy("unexpected_phase").effective_network_mode == "none"


@pytest.mark.parametrize("mode", ["host", "container:other", "default", "custom-network"])
def test_unsupported_modes_fail_closed(mode, docker_client, tmp_path):
    with DockerCommandSession(requested_mode=mode) as session:
        result = session.execute(
            workspace_path=tmp_path, command="pytest", phase="test", timeout_seconds=10
        )
    assert not result.passed
    assert result.error_code == "network_mode_unsupported"
    assert "Only 'none' and 'bridge'" in result.stderr
    docker_client.containers.create.assert_not_called()


def test_allowlist_placeholder_cannot_promise_unimplemented_filtering(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_allow_network_during_setup", True)
    monkeypatch.setattr(settings, "sandbox_allowed_hosts", "pypi.org")
    with pytest.raises(SandboxNetworkPolicyError, match="hostname filtering is not implemented"):
        resolve_network_policy("setup")
    assert resolve_network_policy("baseline").effective_network_mode == "none"


def test_unsupported_docker_platform_and_engine_network_error(docker_client, tmp_path):
    docker_client.info.return_value = {"OSType": "windows"}
    with DockerCommandSession() as session:
        result = session.execute(
            workspace_path=tmp_path, command="pytest", phase="baseline", timeout_seconds=10
        )
        retry = session.execute(
            workspace_path=tmp_path, command="pytest", phase="baseline", timeout_seconds=10
        )
        assert not retry.passed
        docker_client.containers.create.assert_not_called()
    assert result.error_code == "network_mode_unsupported"
    assert "Linux containers" in result.stderr
    docker_client.info.return_value = {"OSType": "linux"}
    docker_client.containers.create.side_effect = DockerException("network unsupported")
    with DockerCommandSession() as session:
        result = session.execute(
            workspace_path=tmp_path, command="pytest", phase="baseline", timeout_seconds=10
        )
    assert result.error_code == "network_mode_unsupported"
    assert "No fallback" in result.stderr


def test_sync_preserves_setup_outputs_and_overlays_agent_edits(docker_client, tmp_path):
    source = tmp_path / "source.py"
    source.write_text("before", encoding="utf-8")
    with DockerCommandSession() as session:
        for phase in ("setup", "baseline"):
            session.execute(
                workspace_path=tmp_path, command="true", phase=phase, timeout_seconds=10
            )
        source.write_text("after", encoding="utf-8")
        session.execute(
            workspace_path=tmp_path, command="true", phase="post_patch", timeout_seconds=10
        )
    archives = docker_client.created[0].put_archive.call_args_list
    with tarfile.open(fileobj=io.BytesIO(archives[1].args[1])) as archive:
        assert all(item.isdir() for item in archive.getmembers())
    with tarfile.open(fileobj=io.BytesIO(archives[2].args[1])) as archive:
        changed = [item for item in archive.getmembers() if item.isfile()]
        assert len(changed) == 1
        assert archive.extractfile(changed[0]).read() == b"after"


def test_phase_and_agent_tools_audit_exceptions_in_traces_and_reports(
    monkeypatch, docker_client, tmp_path
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "sandbox_allow_network_during_setup", True)
    try:
        with Session(engine) as db, DockerCommandSession() as commands:
            task = BenchmarkTask(
                repository=Repository(
                    owner="example", name="policy", url="https://github.com/example/policy"
                ),
                issue_title="Network isolation",
                base_commit="a" * 40,
                status="ready",
                setup_commands=["pip install pytest"],
                test_commands=["pytest"],
            )
            run = AgentRun(
                benchmark_task=task,
                model_provider="mock",
                model_name="mock",
                status="running",
                workspace_path=str(tmp_path),
            )
            db.add(run)
            db.commit()
            tests = ExecutionService(db=db, agent_run_id=run.id, command_session=commands)
            assert tests.run_setup_commands().passed
            assert tests.run_baseline_tests(run_setup=False).passed
            tools = AgentWorkspaceTools(
                db=db, agent_run_id=run.id, workspace_path=tmp_path, command_session=commands
            )
            result = tools.run_tests("pytest")
            assert result.network_policy["effective_network_mode"] == "none"
            events = list(
                db.scalars(
                    select(AgentEvent).where(AgentEvent.event_type == "sandbox_network_policy")
                )
            )
            assert len(events) == 3
            trace = AgentRunTraceService(db).get(run.id)
            assert any(
                "explicit exception: setup (bridge)" in event.summary for event in trace.events
            )
            report_service = AgentRunReportService(db)
            report = report_service.build(run.id)
            assert report.network_policy.network_exception_count == 1
            assert report.network_policy.exception_phases == ["setup"]
            assert "Network exceptions: 1" in report_service.render_markdown(report)
            db.add(
                AgentEvent(
                    agent_run_id=run.id,
                    event_type="sandbox_network_policy",
                    payload_json={
                        "phase": "hidden_eval",
                        "effective_network_mode": "none",
                        "network_exception": False,
                        "command": "private-test-command",
                        "reason": "private-test-payload",
                        "error_code": "private-test-error",
                    },
                )
            )
            db.commit()
            trace = AgentRunTraceService(db).get(run.id)
            assert "private-test" not in trace.model_dump_json()
            assert "private-test" not in report_service.build(run.id).model_dump_json()
            hidden_policy = next(
                event
                for event in trace.events
                if event.event_type == "sandbox_network_policy"
                and event.sanitized_payload.get("phase") == "hidden_eval"
            )
            assert hidden_policy.sanitized_payload["effective_network_mode"] == "none"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "network_request, expected",
    [
        ({"network_enabled": True}, "passed"),
        ({"network_mode": "host"}, "sandbox_error"),
        ({"network_mode": "container:other"}, "sandbox_error"),
    ],
)
def test_api_cannot_bypass_network_policy(
    monkeypatch, docker_client, tmp_path, network_request, expected
):
    def clone_stub(**kwargs):
        if kwargs["phase"] == "clone":
            Path(kwargs["args"][-1]).mkdir()
        return SandboxCommandResult(
            phase=kwargs["phase"],
            command=kwargs["command"],
            passed=True,
            exit_code=0,
            duration_seconds=0,
        )

    runner = DockerSandboxRunner(
        workspace_manager=SandboxWorkspaceManager(workspace_root=tmp_path, retain_workspaces=False)
    )
    monkeypatch.setattr(runner, "_run_host_command", clone_stub)
    monkeypatch.setattr("app.api.routes.sandbox.runner", runner)
    with TestClient(create_app()) as client:
        response = client.post(
            "/sandbox/run",
            json={
                "repository_url": "https://github.com/example/project",
                "base_commit": "a" * 40,
                "setup_commands": ["pip install pytest"],
                "test_commands": ["pytest"],
                **network_request,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected
    assert body["network_enabled"] is False
    if expected == "passed":
        for item in body["setup_results"] + body["test_results"]:
            assert item["network_policy"]["effective_network_mode"] == "none"
    else:
        assert body["error_code"] == "network_mode_unsupported"
        docker_client.containers.create.assert_not_called()


def test_timeout_kills_container_and_records_policy(docker_client, tmp_path):
    unblock = threading.Event()
    with DockerCommandSession() as session:
        session.execute(workspace_path=tmp_path, command="true", phase="setup", timeout_seconds=1)
        container = docker_client.created[0]

        def wait_command(*args, **kwargs):
            unblock.wait(3)
            return SimpleNamespace(exit_code=0, output=(b"", b""))

        container.exec_run.side_effect = wait_command
        container.kill.side_effect = unblock.set
        result = session.execute(
            workspace_path=tmp_path, command="sleep 100", phase="test", timeout_seconds=1
        )
        assert result.timed_out and result.exit_code == 124
        assert result.network_policy.effective_network_mode == "none"
        assert session.closed
        container.kill.assert_called_once()


def test_cleanup_attempts_images_and_client_even_when_container_removal_fails(
    monkeypatch, docker_client, tmp_path
):
    monkeypatch.setattr(settings, "sandbox_allow_network_during_setup", True)
    session = DockerCommandSession()
    session.execute(workspace_path=tmp_path, command="true", phase="setup", timeout_seconds=1)
    session.execute(workspace_path=tmp_path, command="true", phase="test", timeout_seconds=1)
    docker_client.created[-1].remove.side_effect = DockerException("cleanup unavailable")
    with pytest.raises(DockerException, match="cleanup failed"):
        session.close()
    docker_client.images.remove.assert_called_once()
    docker_client.close.assert_called_once()


def test_workspace_links_rejected_before_command_execution(monkeypatch, docker_client, tmp_path):
    link = tmp_path / "link"
    link.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(Path, "is_junction", lambda self: self == link)
    with DockerCommandSession() as session:
        result = session.execute(
            workspace_path=tmp_path, command="pytest", phase="test", timeout_seconds=1
        )
    assert result.error_code == "sandbox_execution_error"
    docker_client.created[0].exec_run.assert_not_called()
