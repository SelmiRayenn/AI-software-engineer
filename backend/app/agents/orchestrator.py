from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from sqlalchemy.orm import Session

from app import crud
from app.agents.loop import AgentLoop, registered_tool_names
from app.agents.prompts import redact_prompt_text, render_agent_prompts
from app.agents.repairs import AgentRepairService
from app.agents.tools import AgentWorkspaceTools, ToolError
from app.core.config import settings
from app.core.run_statuses import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
)
from app.core.task_statuses import TASK_STATUS_READY
from app.evaluation import EvaluationError, EvaluationService
from app.failures import FailureClassificationService
from app.failures.categories import (
    FAILURE_DOCKER_UNAVAILABLE,
    FAILURE_MODEL_PROVIDER_ERROR,
    FAILURE_PATCH_APPLY_FAILED,
    FAILURE_PATCH_QUALITY_BLOCKED,
    FAILURE_REPOSITORY_CHECKOUT_FAILED,
    FAILURE_SETUP_FAILED,
    FAILURE_TIMEOUT,
    FAILURE_UNKNOWN,
)
from app.model_providers import ModelProvider, ModelProviderFactory
from app.models import AgentEvent, AgentRun, BenchmarkTask
from app.patches import PatchApplyError, PatchSafetyError, PatchService
from app.repository_indexing import RepositoryIndexService
from app.repository_indexing.semantic import RepositoryEmbeddingsService
from app.sandbox import (
    DockerUnavailableError,
    SandboxWorkspaceManager,
    SandboxWorkspaceSafetyError,
)
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
        agent_run_id: uuid.UUID | None = None,
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
        if agent_run_id is None:
            run = self._create_agent_run(task=task, provider=provider)
        else:
            run = self._db.get(AgentRun, agent_run_id)
            if (
                run is None
                or run.benchmark_task_id != task.id
                or run.status != RUN_STATUS_QUEUED
                or run.model_provider != provider.provider_name
                or run.model_name != provider.model_name
            ):
                raise AgentRunStartError(
                    "Prepared run must be queued and match the task and model."
                )
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
            max_repair_attempts=run_config.max_repair_attempts,
            run_tests_after_patch=run_config.run_tests_after_patch,
            stop_on_first_passing_patch=run_config.stop_on_first_passing_patch,
            include_test_failure_feedback=run_config.include_test_failure_feedback,
            require_plan_before_edit=run_config.require_plan_before_edit,
            max_plan_revisions=run_config.max_plan_revisions,
            plan_min_evidence_files=run_config.plan_min_evidence_files,
            require_hypothesis_before_patch=run_config.require_hypothesis_before_patch,
            require_candidate_files_before_edit=run_config.require_candidate_files_before_edit,
            max_candidate_files=run_config.max_candidate_files,
        )
        prompt_preview = prompts.redacted_preview()
        steps: list[AgentRunTraceStep] = []
        generated_patch_id: uuid.UUID | None = None
        changed_files: list[str] = []
        error_message: str | None = None
        failure_category: str | None = None

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
                    failure_category = FAILURE_SETUP_FAILED
                    raise AgentRunStartError("Setup phase failed.")
                test_executor.run_baseline_tests(run_setup=False)
                self._create_repository_index(run, workspace)
                repairs = AgentRepairService(
                    db=self._db,
                    run=run,
                    config=run_config,
                    tests=test_executor,
                    patches=PatchService(
                        db=self._db, agent_run_id=run.id, workspace_path=workspace.path
                    ),
                )
                loop = AgentLoop(
                    db=self._db,
                    agent_run=run,
                    benchmark_task=task,
                    repository=repository,
                    provider=provider,
                    tools=tools,
                    config=run_config,
                    prompts=prompts,
                    repairs=repairs,
                )
                try:
                    loop_result = loop.run()
                except Exception as exc:
                    repairs.finish(
                        stop_reason="unrecoverable_error",
                        error_message=redact_prompt_text(str(exc)),
                        failure_category=_failure_category_for_exception(exc),
                    )
                    raise AgentRunStartError(redact_prompt_text(str(exc))) from exc
                steps = loop_result.steps
                outcome = repairs.finish(
                    stop_reason=loop_result.stop_reason,
                    error_message=loop_result.error_message,
                    failure_category=loop_result.failure_category,
                )
                failure_category = outcome.failure_category
                if outcome.patch:
                    generated_patch_id = outcome.patch.id
                    changed_files = outcome.patch.changed_files
                if run_config.run_hidden_tests and outcome.patch and outcome.valid:
                    test_executor.run_hidden_evaluation(
                        generated_patch_id=outcome.patch.id, run_hidden_tests=True
                    )
                if not outcome.accepted:
                    raise AgentRunStartError(
                        outcome.failure_summary or "Repair attempts exhausted."
                    )

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
            error_message = redact_prompt_text(str(exc))[:2000]
            failure_category = failure_category or _failure_category_for_exception(exc)
            run.failure_summary = error_message
            self._mark_run_status(run, RUN_STATUS_FAILED)
            failure_event = self._log_event(
                run,
                "agent_run_failed",
                {
                    "step_count": len(steps),
                    "error_message": error_message,
                    "failure_category": failure_category,
                },
            )
            FailureClassificationService(self._db).classify(
                agent_run_id=run.id,
                category=failure_category,
                summary=error_message,
                source_event_id=failure_event.id,
            )
            if run.final_patch_id:
                generated_patch_id = run.final_patch_id
                changed_files = run.generated_patch.changed_files
                try:
                    EvaluationService(db=self._db, agent_run_id=run.id).evaluate(
                        include_failed=True
                    )
                except EvaluationError as evaluation_error:
                    self._log_event(
                        run,
                        "evaluation_failed",
                        {
                            "error_message": redact_prompt_text(str(evaluation_error))[:2000],
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
            repair_attempts_used=run.repair_attempts_used,
            final_patch_id=run.final_patch_id,
            final_patch_passed_tests=run.final_patch_passed_tests,
            failure_summary=run.failure_summary,
            failure_category=run.failure.category if run.failure else None,
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
        if status == RUN_STATUS_RUNNING:
            run.started_at = datetime.now(UTC)
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

    def _log_event(self, run: AgentRun, event_type: str, payload: dict[str, object]) -> AgentEvent:
        event = AgentEvent(
            agent_run_id=run.id,
            event_type=event_type,
            payload_json=payload,
        )
        self._db.add(event)
        self._db.commit()
        self._db.refresh(event)
        return event


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
    run = db.get(AgentRun, agent_run_id)
    patch = run.generated_patch if run else None
    return patch.id if patch is not None else None


def _failure_category_for_exception(exc: Exception) -> str:
    message = str(exc).lower()
    cause = exc.__cause__
    if isinstance(exc, DockerUnavailableError) or isinstance(cause, DockerUnavailableError):
        return FAILURE_DOCKER_UNAVAILABLE
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or "timeout" in message:
        return FAILURE_TIMEOUT
    if isinstance(exc, WorkspacePreparationError):
        return FAILURE_REPOSITORY_CHECKOUT_FAILED
    if isinstance(exc, PatchApplyError):
        return FAILURE_PATCH_APPLY_FAILED
    if isinstance(exc, PatchSafetyError) and "quality guardrail" in message:
        return FAILURE_PATCH_QUALITY_BLOCKED
    if "model provider" in message or "provider call failed" in message:
        return FAILURE_MODEL_PROVIDER_ERROR
    return FAILURE_UNKNOWN
