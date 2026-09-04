from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud
from app.agents.tools import AgentWorkspaceTools, ToolError
from app.core.run_statuses import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
)
from app.core.task_statuses import TASK_STATUS_READY
from app.evaluation import EvaluationService
from app.model_providers import (
    ModelMessage,
    ModelProvider,
    ModelProviderFactory,
    ModelProviderResponse,
    ToolDefinition,
)
from app.models import AgentEvent, AgentRun, BenchmarkTask, GeneratedPatch, Repository
from app.sandbox import SandboxWorkspaceManager, SandboxWorkspaceSafetyError
from app.sandbox.workspace import SandboxWorkspaceMetadata
from app.schemas.agent_run import AgentRunStartRequest, AgentRunStartResponse, AgentRunTraceStep
from app.test_execution import TestExecutionError, TestExecutionService


class AgentRunStartError(RuntimeError):
    pass


class BenchmarkTaskNotReadyError(AgentRunStartError):
    pass


class WorkspacePreparationError(AgentRunStartError):
    pass


class MaxStepsExceededError(AgentRunStartError):
    pass


@dataclass(frozen=True)
class PreparedWorkspace:
    workspace_id: str
    path: Path
    workspace_path: Path | None = None
    metadata: SandboxWorkspaceMetadata | None = None
    cleanup: Callable[[], None] | None = None

    def close(self) -> None:
        if self.cleanup is not None:
            self.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class GitSandboxWorkspacePreparer:
    def __init__(self, *, workspace_manager: SandboxWorkspaceManager | None = None) -> None:
        self._workspace_manager = workspace_manager or SandboxWorkspaceManager()

    def prepare(
        self,
        *,
        repository_url: str,
        base_commit: str,
        command_timeout_seconds: int,
    ) -> PreparedWorkspace:
        workspace = self._workspace_manager.create_workspace(prefix="agent-run")

        try:
            _run_workspace_command(
                ["git", "clone", "--no-checkout", "--", repository_url, str(workspace.repo_path)],
                cwd=workspace.workspace_path,
                timeout_seconds=command_timeout_seconds,
            )
            _run_workspace_command(
                ["git", "-C", str(workspace.repo_path), "checkout", "--detach", base_commit],
                cwd=workspace.workspace_path,
                timeout_seconds=command_timeout_seconds,
            )
        except (OSError, RuntimeError):
            try:
                self._workspace_manager.cleanup_workspace(workspace)
            except (OSError, SandboxWorkspaceSafetyError):
                pass
            raise

        return PreparedWorkspace(
            workspace_id=workspace.workspace_id,
            path=workspace.repo_path,
            workspace_path=workspace.workspace_path,
            metadata=workspace,
            cleanup=lambda: self._workspace_manager.cleanup_workspace(workspace),
        )


class AgentRunOrchestrator:
    def __init__(
        self,
        *,
        db: Session,
        workspace_preparer: GitSandboxWorkspacePreparer | None = None,
        provider_factory: ModelProviderFactory | None = None,
    ) -> None:
        self._db = db
        self._workspace_preparer = workspace_preparer or GitSandboxWorkspacePreparer()
        self._provider_factory = provider_factory or ModelProviderFactory()

    def start_run(
        self,
        *,
        benchmark_task_id: uuid.UUID,
        request: AgentRunStartRequest,
    ) -> AgentRunStartResponse:
        task = crud.get_benchmark_task(self._db, benchmark_task_id)
        if task is None:
            raise AgentRunStartError("Benchmark task not found.")
        if task.status != TASK_STATUS_READY:
            raise BenchmarkTaskNotReadyError("Benchmark task must be ready before starting a run.")

        repository = task.repository
        if repository is None:
            raise AgentRunStartError("Benchmark task repository not found.")

        provider = self._provider_factory.create(
            request.model_provider,
            model_name=request.model_name,
        )
        run = self._create_agent_run(task=task, provider=provider)
        steps: list[AgentRunTraceStep] = []
        generated_patch_id: uuid.UUID | None = None
        changed_files: list[str] = []
        error_message: str | None = None

        try:
            self._mark_run_status(run, RUN_STATUS_RUNNING)
            self._log_event(
                run,
                "agent_run_started",
                {
                    "benchmark_task_id": str(task.id),
                    "model_provider": provider.provider_name,
                    "model_name": provider.model_name,
                    "max_steps": request.max_steps,
                },
            )
            provider_response = provider.generate_response(
                self._agent_visible_messages(task, repository),
                tools=_controlled_tool_definitions(),
            )
            self._log_model_response(run, provider, provider_response)

            with self._workspace_preparer.prepare(
                repository_url=repository.url,
                base_commit=task.base_commit,
                command_timeout_seconds=request.command_timeout_seconds,
            ) as workspace:
                self._set_run_workspace(run, workspace)
                self._log_event(
                    run,
                    "workspace_prepared",
                    {
                        "workspace_id": workspace.workspace_id,
                        "workspace_path": str(workspace.path),
                    },
                )
                tools = AgentWorkspaceTools(
                    db=self._db,
                    agent_run_id=run.id,
                    workspace_path=workspace.path,
                    allowed_test_commands=task.test_commands,
                    command_timeout_seconds=request.command_timeout_seconds,
                )
                test_executor = TestExecutionService(
                    db=self._db,
                    agent_run_id=run.id,
                    workspace_path=workspace.path,
                    command_timeout_seconds=request.command_timeout_seconds,
                )
                setup_result = test_executor.run_setup_commands()
                if not setup_result.passed:
                    raise AgentRunStartError("Setup phase failed.")
                test_executor.run_baseline_tests(run_setup=False)
                listed_files = self._run_step(
                    steps,
                    request.max_steps,
                    "list_files",
                    lambda: tools.list_files(),
                )
                read_target = _select_read_target(listed_files.files)
                self._run_step(
                    steps,
                    request.max_steps,
                    "read_file",
                    lambda: tools.read_file(read_target),
                )
                self._run_step(
                    steps,
                    request.max_steps,
                    "get_diff",
                    lambda: tools.get_diff(),
                )
                submitted_patch = self._run_step(
                    steps,
                    request.max_steps,
                    "submit_patch",
                    lambda: tools.submit_patch(),
                )
                generated_patch_id = submitted_patch.generated_patch_id
                changed_files = submitted_patch.changed_files
                post_patch_result = test_executor.run_post_patch_tests()
                if not post_patch_result.passed:
                    raise AgentRunStartError("Post-patch test phase failed.")

            self._mark_run_status(run, RUN_STATUS_COMPLETED)
            metric = EvaluationService(db=self._db, agent_run_id=run.id).evaluate()
            self._log_event(
                run,
                "agent_run_completed",
                {
                    "step_count": len(steps),
                    "generated_patch_id": str(generated_patch_id) if generated_patch_id else None,
                    "evaluation_metric_id": str(metric.id),
                    "changed_files": changed_files,
                },
            )
        except (AgentRunStartError, OSError, RuntimeError, ToolError, TestExecutionError, ValueError) as exc:
            error_message = str(exc)
            self._mark_run_status(run, RUN_STATUS_FAILED)
            self._log_event(
                run,
                "agent_run_failed",
                {
                    "step_count": len(steps),
                    "error_message": error_message,
                },
            )

        self._db.refresh(run)
        return AgentRunStartResponse(
            id=run.id,
            benchmark_task_id=run.benchmark_task_id,
            model_provider=run.model_provider,
            model_name=run.model_name,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            steps=steps,
            generated_patch_id=generated_patch_id,
            changed_files=changed_files,
            patch_review_status=run.patch_review_status,
            error_message=error_message,
        )

    def _create_agent_run(self, *, task: BenchmarkTask, provider: ModelProvider) -> AgentRun:
        run = AgentRun(
            benchmark_task_id=task.id,
            model_provider=provider.provider_name,
            model_name=provider.model_name,
            status=RUN_STATUS_QUEUED,
        )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)
        return run

    def _mark_run_status(self, run: AgentRun, status: str) -> None:
        run.status = status
        if status in {RUN_STATUS_COMPLETED, RUN_STATUS_FAILED}:
            run.completed_at = datetime.now(UTC)
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)

    def _set_run_workspace(self, run: AgentRun, workspace: PreparedWorkspace) -> None:
        run.workspace_id = workspace.workspace_id
        run.workspace_path = str(workspace.path)
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)

    def _run_step(
        self,
        steps: list[AgentRunTraceStep],
        max_steps: int,
        step_name: str,
        operation: Callable[[], Any],
    ) -> Any:
        if len(steps) >= max_steps:
            raise MaxStepsExceededError(f"Maximum step limit of {max_steps} reached.")

        started = time.perf_counter()
        try:
            result = operation()
        except Exception as exc:
            steps.append(
                AgentRunTraceStep(
                    step_name=step_name,
                    success=False,
                    duration_seconds=time.perf_counter() - started,
                    error_message=str(exc),
                )
            )
            raise

        steps.append(
            AgentRunTraceStep(
                step_name=step_name,
                success=True,
                duration_seconds=time.perf_counter() - started,
                summary=_step_summary(result),
                files_read=_step_files(result),
                files_modified=_step_files(result, modified=True),
            )
        )
        return result

    def _log_event(self, run: AgentRun, event_type: str, payload: dict[str, Any]) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type=event_type,
                payload_json=payload,
            )
        )
        self._db.commit()

    def _log_model_response(
        self,
        run: AgentRun,
        provider: ModelProvider,
        response: ModelProviderResponse,
    ) -> None:
        self._log_event(
            run,
            "model_response",
            {
                "provider_name": provider.provider_name,
                "model_name": provider.model_name,
                "content_preview": response.content[:500],
                "tool_calls": [
                    {"id": tool_call.id, "name": tool_call.name}
                    for tool_call in response.tool_calls
                ],
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "estimated_cost": response.estimated_cost,
                "latency_seconds": response.latency_seconds,
            },
        )

    def _agent_visible_messages(
        self,
        task: BenchmarkTask,
        repository: Repository,
    ) -> list[ModelMessage]:
        return [
            ModelMessage(
                role="system",
                content=(
                    "You are an AI software engineering agent. Use only the controlled "
                    "workspace tools provided by the platform."
                ),
            ),
            ModelMessage(
                role="user",
                content=(
                    f"Repository: {repository.owner}/{repository.name}\n"
                    f"Repository URL: {repository.url}\n"
                    f"Base commit: {task.base_commit}\n"
                    f"Issue #{task.issue_number}: {task.issue_title}\n\n"
                    f"{task.issue_body or ''}"
                ),
            ),
        ]


def _run_workspace_command(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> None:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspacePreparationError(
            f"Workspace command exceeded timeout of {timeout_seconds} seconds."
        ) from exc

    if completed.returncode != 0:
        raise WorkspacePreparationError(
            completed.stderr.strip() or "Workspace preparation command failed."
        )


def _controlled_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(name="list_files", description="List files in the prepared workspace."),
        ToolDefinition(name="search_code", description="Search code inside the prepared workspace."),
        ToolDefinition(name="read_file", description="Read a text file from the workspace."),
        ToolDefinition(name="write_file", description="Write a text file inside the workspace."),
        ToolDefinition(name="run_tests", description="Run an explicitly allowed test command."),
        ToolDefinition(name="get_diff", description="Inspect the current workspace diff."),
        ToolDefinition(name="submit_patch", description="Submit the current workspace diff."),
    ]


def _select_read_target(files: list[str]) -> str:
    readmes = [
        file_path
        for file_path in files
        if Path(file_path).name.lower() in {"readme", "readme.md", "readme.rst", "readme.txt"}
    ]
    if readmes:
        return min(readmes)

    source_extensions = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".php",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
    source_files = [file_path for file_path in files if Path(file_path).suffix in source_extensions]
    if source_files:
        return min(source_files)

    if files:
        return min(files)
    raise AgentRunStartError("Workspace does not contain a readable file.")


def _step_summary(result: Any) -> dict[str, Any]:
    if hasattr(result, "files"):
        return {
            "file_count": len(result.files),
            "truncated": getattr(result, "truncated", False),
        }
    if hasattr(result, "file_path") and hasattr(result, "size_bytes"):
        return {"file_path": result.file_path, "size_bytes": result.size_bytes}
    if hasattr(result, "changed_files"):
        return {"changed_files": result.changed_files}
    return {}


def _step_files(result: Any, *, modified: bool = False) -> list[str]:
    if hasattr(result, "file_path"):
        return [result.file_path]
    if modified and hasattr(result, "changed_files"):
        return list(result.changed_files)
    return []


def latest_generated_patch_id(db: Session, agent_run_id: uuid.UUID) -> uuid.UUID | None:
    patch = db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == agent_run_id))
    return patch.id if patch is not None else None
