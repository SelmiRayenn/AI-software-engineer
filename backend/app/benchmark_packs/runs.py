from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.orchestrator import AgentRunOrchestrator, BenchmarkTaskNotReadyError
from app.benchmark_packs.service import BenchmarkPackConflict, BenchmarkPackNotFound
from app.benchmark_tasks.read_models import to_agent_visible_task
from app.evaluation import EvaluationService
from app.evaluation.service import model_usage_totals
from app.failures import FailureClassificationService
from app.model_providers import ModelProviderConfigError, ModelProviderFactory
from app.models import (
    AgentEvent,
    AgentRun,
    BenchmarkPack,
    BenchmarkPackRun,
    BenchmarkPackRunTask,
    BenchmarkPackTask,
    BenchmarkTask,
)
from app.schemas.agent_run import AgentRunStartRequest
from app.schemas.benchmark_pack_run import (
    BenchmarkPackRunAggregates,
    BenchmarkPackRunRead,
    BenchmarkPackRunRequest,
    PackTaskMetrics,
)


class BenchmarkPackRunNotFound(RuntimeError):
    pass


class BenchmarkPackRunService:
    def __init__(
        self,
        db: Session,
        *,
        orchestrator: AgentRunOrchestrator | None = None,
        provider_factory: ModelProviderFactory | None = None,
    ) -> None:
        self._db = db
        self._factory = provider_factory or ModelProviderFactory()
        self._orchestrator = orchestrator or AgentRunOrchestrator(
            db=db, provider_factory=self._factory
        )

    def start(
        self,
        pack_id: UUID,
        request: BenchmarkPackRunRequest,
        *,
        trusted_operator: bool = False,
    ) -> BenchmarkPackRunRead:
        if request.include_hidden_tests and not trusted_operator:
            raise PermissionError("Hidden evaluation requires trusted operator access.")
        pack = self._db.get(BenchmarkPack, pack_id)
        if pack is None:
            raise BenchmarkPackNotFound("Benchmark pack not found.")
        members = list(
            self._db.scalars(
                select(BenchmarkPackTask)
                .join(BenchmarkPackTask.benchmark_task)
                .where(
                    BenchmarkPackTask.benchmark_pack_id == pack_id,
                    BenchmarkTask.status == "ready",
                )
                .options(selectinload(BenchmarkPackTask.benchmark_task))
                .order_by(BenchmarkPackTask.order_index)
            )
        )
        if not members:
            raise BenchmarkPackConflict("Benchmark pack has no ready tasks.")
        run_request = request.agent_request(
            self._factory.resolve_model_name(request.model_provider, request.model_name)
        )
        pack_run = BenchmarkPackRun(
            id=uuid4(),
            benchmark_pack_id=pack_id,
            pack_name=pack.name,
            pack_slug=pack.slug,
            pack_version=pack.version,
            model_provider=run_request.model_provider,
            model_name=run_request.model_name,
            run_config=run_request.model_dump(mode="json"),
            include_hidden_tests=request.include_hidden_tests,
            stop_on_task_failure=request.stop_on_task_failure,
            status="running",
            started_at=datetime.now(UTC),
        )
        self._db.add(pack_run)
        # Save the entire roster before executing anything, including tasks later skipped.
        for member in members:
            task = member.benchmark_task
            run = AgentRun(
                id=uuid4(),
                benchmark_task_id=task.id,
                model_provider=run_request.model_provider,
                model_name=run_request.model_name,
                status="queued",
                started_at=pack_run.started_at,
            )
            self._db.add(run)
            self._db.flush()
            entry = BenchmarkPackRunTask(
                pack_run=pack_run,
                benchmark_task_id=task.id,
                agent_run_id=run.id,
                order_index=member.order_index,
                task_snapshot=to_agent_visible_task(task).model_dump(mode="json"),
                definition_hash=_definition_hash(task),
                status="queued",
            )
            self._db.add(entry)
            self._event(entry, "requested", {"config": pack_run.run_config})
        self._db.flush()
        self._update_aggregates(pack_run)
        self._db.commit()

        stop = False
        for entry in pack_run.tasks:
            if stop:
                self._skip(entry)
            else:
                self._execute(entry, run_request)
                stop = request.stop_on_task_failure and entry.status == "failed"
            self._update_aggregates(pack_run)
            self._db.commit()
        statuses = {entry.status for entry in pack_run.tasks}
        pack_run.status = (
            "completed"
            if statuses == {"completed"}
            else "partial_failure"
            if "completed" in statuses
            else "failed"
        )
        pack_run.completed_at = datetime.now(UTC)
        self._db.commit()
        return self.get(pack_run.id)

    def get(self, pack_run_id: UUID) -> BenchmarkPackRunRead:
        pack_run = self._db.scalar(
            select(BenchmarkPackRun)
            .where(BenchmarkPackRun.id == pack_run_id)
            .options(selectinload(BenchmarkPackRun.tasks))
        )
        if pack_run is None:
            raise BenchmarkPackRunNotFound("Benchmark pack run not found.")
        return BenchmarkPackRunRead.model_validate(pack_run)

    def _execute(self, entry: BenchmarkPackRunTask, request: AgentRunStartRequest) -> None:
        run = self._db.get(AgentRun, entry.agent_run_id)
        run.started_at = datetime.now(UTC)
        entry.started_at = run.started_at
        entry.status = "running"
        self._event(entry, "started")
        self._db.commit()
        try:
            self._db.expire_all()
            task = self._db.get(BenchmarkTask, entry.benchmark_task_id)
            if task.status != "ready" or _definition_hash(task) != entry.definition_hash:
                raise BenchmarkTaskNotReadyError(
                    "Task is no longer ready or its definition changed after pack selection."
                )
            self._orchestrator.start_run(
                benchmark_task_id=task.id, request=request, agent_run_id=run.id
            )
            if run.status not in {"completed", "failed", "cancelled"}:
                raise RuntimeError("Task runner returned without a terminal state.")
        except Exception as exc:  # noqa: BLE001 - each task is an independent failure boundary
            self._db.rollback()
            if isinstance(exc, ModelProviderConfigError):
                category, summary = "model_provider_error", str(exc)
            elif isinstance(exc, BenchmarkTaskNotReadyError):
                category, summary = "task_not_ready", str(exc)
            else:
                category, summary = "unknown", f"Pack task execution failed ({type(exc).__name__})."
            self._fail(entry, category, summary)

        run = self._db.get(AgentRun, entry.agent_run_id)
        metric = None
        if run.status != "cancelled":
            try:
                evaluator = EvaluationService(db=self._db, agent_run_id=run.id)
                metric = evaluator.get_metrics() or evaluator.evaluate(include_failed=True)
            except Exception as exc:  # noqa: BLE001 - preserve the roster on evaluation failure
                self._db.rollback()
                self._fail(entry, "unknown", f"Evaluation failed ({type(exc).__name__}).")
                self._event(entry, "evaluation_failed")

        entry.status = "completed" if run.status == "completed" else "failed"
        entry.started_at = run.started_at
        entry.completed_at = run.completed_at
        entry.metric_summary = (
            PackTaskMetrics.model_validate(metric) if metric else self._usage_fallback(run)
        ).model_dump(mode="json")
        if entry.status == "failed":
            failure = FailureClassificationService(self._db).get_or_classify(run.id)
            entry.failure_category = failure.category
            entry.failure_summary = failure.human_readable_summary
        self._event(entry, "finished", {"status": entry.status})
        self._db.commit()

    def _fail(self, entry: BenchmarkPackRunTask, category: str, summary: str) -> None:
        run = self._db.get(AgentRun, entry.agent_run_id)
        run.status = "failed"
        run.completed_at = run.completed_at or datetime.now(UTC)
        if run.failure is None:
            event = self._event(entry, "failed", {"failure_category": category})
            self._db.flush()
            FailureClassificationService(self._db).classify(
                agent_run_id=run.id,
                category=category,
                summary=summary,
                source_event_id=event.id,
            )
        self._db.commit()

    def _skip(self, entry: BenchmarkPackRunTask) -> None:
        run = self._db.get(AgentRun, entry.agent_run_id)
        run.status = "cancelled"
        run.started_at = run.completed_at = datetime.now(UTC)
        entry.status = "skipped"
        entry.completed_at = run.completed_at
        event = self._event(entry, "skipped")
        self._db.flush()
        failure = FailureClassificationService(self._db).classify(
            agent_run_id=run.id,
            category="cancelled",
            summary="Not started because stop_on_task_failure was enabled and an earlier task failed.",
            source_event_id=event.id,
        )
        entry.failure_category = failure.category
        entry.failure_summary = failure.human_readable_summary

    def _usage_fallback(self, run: AgentRun) -> PackTaskMetrics:
        events = list(self._db.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run.id)))
        tokens, cost = model_usage_totals(run.model_provider, events)
        elapsed = 0.0
        if run.started_at and run.completed_at:
            elapsed = max(0.0, (run.completed_at - run.started_at).total_seconds())
        return PackTaskMetrics(
            tokens_used=tokens, estimated_cost=cost, execution_time_seconds=round(elapsed, 4)
        )

    def _event(
        self, entry: BenchmarkPackRunTask, name: str, payload: dict | None = None
    ) -> AgentEvent:
        event = AgentEvent(
            agent_run_id=entry.agent_run_id,
            event_type=f"benchmark_pack_task_{name}",
            payload_json={
                "pack_run_id": str(entry.pack_run.id),
                "order_index": entry.order_index,
                **(payload or {}),
            },
        )
        self._db.add(event)
        return event

    def _update_aggregates(self, pack_run: BenchmarkPackRun) -> None:
        pack_run.aggregates = calculate_pack_aggregates(pack_run.tasks).model_dump(mode="json")


def calculate_pack_aggregates(tasks: list[BenchmarkPackRunTask]) -> BenchmarkPackRunAggregates:
    total = len(tasks)
    completed = sum(task.status == "completed" for task in tasks)
    failed = sum(task.status == "failed" for task in tasks)
    metrics = [PackTaskMetrics.model_validate(task.metric_summary or {}) for task in tasks]
    resolved = sum(metric.issue_resolved for metric in metrics)
    hidden = [metric for metric in metrics if metric.hidden_tests_run_count > 0]
    elapsed = sum(metric.execution_time_seconds or 0.0 for metric in metrics)
    return BenchmarkPackRunAggregates(
        total_tasks=total,
        completed_tasks=completed,
        failed_tasks=failed,
        skipped_tasks=sum(task.status == "skipped" for task in tasks),
        issue_resolved_count=resolved,
        issue_resolved_rate=resolved / total if total else 0.0,
        visible_test_pass_rate=sum(metric.post_patch_tests_passed for metric in metrics) / total
        if total
        else 0.0,
        hidden_test_pass_rate=sum(metric.hidden_tests_passed is True for metric in hidden)
        / len(hidden)
        if hidden
        else None,
        hidden_tested_tasks=len(hidden),
        average_file_localization_score=sum(
            metric.file_localization_score or 0.0 for metric in metrics
        )
        / total
        if total
        else 0.0,
        average_issue_specific_score=sum(metric.issue_specific_score for metric in metrics) / total
        if total
        else 0.0,
        total_tokens=sum(metric.tokens_used or 0 for metric in metrics),
        total_cost=round(sum(metric.estimated_cost or 0.0 for metric in metrics), 8),
        total_execution_time=round(elapsed, 4),
        average_execution_time=round(elapsed / (completed + failed), 4)
        if completed + failed
        else 0.0,
    )


def _definition_hash(task: BenchmarkTask) -> str:
    definition = {
        "task": to_agent_visible_task(task).model_dump(mode="json"),
        "setup_commands": task.setup_commands,
        "test_commands": task.test_commands,
        "allow_lockfile_changes": task.allow_lockfile_changes,
        "allow_dependency_file_changes": task.allow_dependency_file_changes,
    }
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode("utf-8")).hexdigest()
