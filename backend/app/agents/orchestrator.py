from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud
from app.agents.loop import AgentLoop, registered_tool_names
from app.agents.prompts import render_agent_prompts
from app.agents.tools import AgentWorkspaceTools, ToolError
from app.core.config import settings
from app.core.run_statuses import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
)
from app.core.task_statuses import TASK_STATUS_READY
from app.evaluation import EvaluationService
from app.model_providers import ModelProvider, ModelProviderFactory
from app.models import AgentEvent, AgentRun, BenchmarkTask, GeneratedPatch
from app.repository_indexing import RepositoryIndexService
from app.repository_indexing.semantic import RepositoryEmbeddingsService
from app.sandbox import SandboxWorkspaceManager, SandboxWorkspaceSafetyError
from app.sandbox.workspace import SandboxWorkspaceMetadata
from app.schemas.agent_run import (
    AgentRunConfig,
    AgentRunStartRequest,
    AgentRunStartResponse,
    AgentRunTraceStep,
)
from app.test_execution import TestExecutionError, TestExecutionService


class AgentRunStartError(RuntimeError):
    pass


class BenchmarkTaskNotReadyError(AgentRunStartError):
    pass


class WorkspacePreparationError(AgentRunStartError):
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
        run_config = AgentRunConfig.model_validate(
            {
                **request.model_dump(),
                "model_provider": provider.provider_name,
                "model_name": provider.model_name,
            }
        )
        prompts = render_agent_prompts(
            task=task,
            repository=repository,
            allowed_tools=registered_tool_names(enable_test_tool=run_config.enable_test_tool),
            configured_test_commands=list(task.test_commands or []),
            max_steps=run_config.max_steps,
            max_tool_errors=run_config.max_tool_errors,
            command_timeout_seconds=run_config.command_timeout_seconds,
            include_issue_comments=run_config.include_issue_comments,
            enable_test_tool=run_config.enable_test_tool,
            run_mode=run_config.run_mode,
        )
        prompt_preview = prompts.redacted_preview()
        steps: list[AgentRunTraceStep] = []
        generated_patch_id: uuid.UUID | None = None
        changed_files: list[str] = []
        error_message: str | None = None

        try:
            self._mark_run_status(run, RUN_STATUS_RUNNING)
            self._log_event(
                run,
                "agent_run_configured",
                {
                    "config": run_config.model_dump(mode="json"),
                    "prompt_preview": prompt_preview,
                },
            )
            self._log_event(
                run,
                "agent_run_started",
                {
                    "benchmark_task_id": str(task.id),
                    "model_provider": provider.provider_name,
                    "model_name": provider.model_name,
                    "max_steps": request.max_steps,
                    "max_tool_errors": request.max_tool_errors,
                },
            )

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
                self._create_repository_index(run, workspace)
                loop_result = AgentLoop(
                    db=self._db,
                    agent_run=run,
                    benchmark_task=task,
                    repository=repository,
                    provider=provider,
                    tools=tools,
                    config=run_config,
                    prompts=prompts,
                ).run()
                steps = loop_result.steps
                submitted_patch = loop_result.submitted_patch
                if submitted_patch is None:
                    raise AgentRunStartError(
                        loop_result.error_message
                        or f"Agent loop stopped without a patch: {loop_result.stop_reason}."
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
        except (
            AgentRunStartError,
            OSError,
            RuntimeError,
            ToolError,
            TestExecutionError,
            ValueError,
        ) as exc:
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
            run_config=run_config,
            prompt_preview=prompt_preview,
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

    def _create_repository_index(
        self,
        run: AgentRun,
        workspace: PreparedWorkspace,
    ) -> None:
        # Test and legacy preparers may not represent a managed sandbox workspace.
        if workspace.metadata is None:
            return
        manager = SandboxWorkspaceManager(
            workspace_root=workspace.metadata.workspace_root,
            retain_workspaces=workspace.metadata.retain,
        )
        index = RepositoryIndexService(
            db=self._db,
            agent_run_id=run.id,
            workspace_manager=manager,
        ).create_index()
        self._log_event(
            run,
            "repository_index_created",
            {
                "repository_index_id": str(index.id),
                "file_count": index.file_count,
                "total_size_bytes": index.total_size_bytes,
                "checksum": index.checksum,
            },
        )
        if settings.embedding_auto_build:
            RepositoryEmbeddingsService(db=self._db, agent_run_id=run.id).build()

    def _log_event(self, run: AgentRun, event_type: str, payload: dict[str, object]) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=run.id,
                event_type=event_type,
                payload_json=payload,
            )
        )
        self._db.commit()


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


def latest_generated_patch_id(db: Session, agent_run_id: uuid.UUID) -> uuid.UUID | None:
    patch = db.scalar(select(GeneratedPatch).where(GeneratedPatch.agent_run_id == agent_run_id))
    return patch.id if patch is not None else None
