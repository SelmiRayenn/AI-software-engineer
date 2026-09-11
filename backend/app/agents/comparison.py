from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.orchestrator import AgentRunOrchestrator, BenchmarkTaskNotReadyError
from app.agents.prompts import redact_prompt_text
from app.evaluation import EvaluationService
from app.failures import FailureClassificationService
from app.failures.categories import FAILURE_MODEL_PROVIDER_ERROR
from app.failures.service import classify_failure_text
from app.model_providers import ModelProviderConfigError
from app.models import AgentEvent, AgentRun, BenchmarkTask, EvaluationMetric
from app.schemas.agent_run import AgentRunStartRequest
from app.schemas.model_comparison import (
    ComparisonWinner,
    ModelComparisonAggregates,
    ModelComparisonRequest,
    ModelComparisonResponse,
    ModelComparisonRun,
)

REQUESTED_EVENT = "model_comparison_run_requested"
FINISHED_EVENT = "model_comparison_run_finished"
METRIC_FIELDS = (
    "patch_applied",
    "tests_passed",
    "file_localization_score",
    "modified_files_count",
    "unrelated_files_count",
    "tokens_used",
    "estimated_cost",
    "execution_time_seconds",
)


class ModelComparisonNotFoundError(RuntimeError):
    pass


class ModelComparisonService:
    def __init__(self, *, db: Session, orchestrator: AgentRunOrchestrator | None = None) -> None:
        self._db = db
        self._orchestrator = orchestrator or AgentRunOrchestrator(db=db)

    def compare(
        self, *, benchmark_task_id: UUID, request: ModelComparisonRequest
    ) -> ModelComparisonResponse:
        task = self._task(benchmark_task_id)
        if task.status != "ready":
            raise BenchmarkTaskNotReadyError(
                "Benchmark task must be ready before comparing models."
            )

        comparison_id = uuid4()
        created_at = datetime.now(UTC)
        requests = request.run_requests()
        run_ids: list[UUID] = []
        # Persist the entire roster before execution, including models whose configuration fails.
        for position, run_request in enumerate(requests):
            run_id = uuid4()
            run_ids.append(run_id)
            self._db.add(
                AgentRun(
                    id=run_id,
                    benchmark_task_id=benchmark_task_id,
                    model_provider=run_request.model_provider,
                    model_name=run_request.model_name,
                    status="queued",
                    started_at=created_at,
                )
            )
            self._db.flush()
            self._event(
                run_id,
                REQUESTED_EVENT,
                {
                    "comparison_id": str(comparison_id),
                    "position": position,
                    "config": run_request.model_dump(mode="json"),
                },
                created_at=created_at,
            )
        self._db.commit()

        for run_id, run_request in zip(run_ids, requests, strict=True):
            self._execute_model(benchmark_task_id, comparison_id, run_id, run_request)
        return self.get_comparison(benchmark_task_id=benchmark_task_id, comparison_id=comparison_id)

    def _execute_model(
        self,
        task_id: UUID,
        comparison_id: UUID,
        run_id: UUID,
        request: AgentRunStartRequest,
    ) -> None:
        run = self._db.get(AgentRun, run_id)
        run.started_at = datetime.now(UTC)
        self._event(run_id, "model_comparison_run_started", {"comparison_id": str(comparison_id)})
        self._db.commit()
        failure_reason = None
        try:
            result = self._orchestrator.start_run(
                benchmark_task_id=task_id, request=request, agent_run_id=run_id
            )
            failure_reason = result.error_message
        except Exception as exc:  # noqa: BLE001 - each model is an independent failure boundary
            self._db.rollback()
            failure_reason = (
                str(exc)
                if isinstance(exc, ModelProviderConfigError)
                else f"Model run failed ({type(exc).__name__})."
            )
            self._fail_run(
                run_id,
                failure_reason,
                category=(
                    FAILURE_MODEL_PROVIDER_ERROR
                    if isinstance(exc, ModelProviderConfigError)
                    else classify_failure_text(failure_reason)
                ),
            )

        try:
            evaluator = EvaluationService(db=self._db, agent_run_id=run_id)
            if evaluator.get_metrics() is None:
                evaluator.evaluate(include_failed=True)
        except Exception as exc:  # noqa: BLE001 - preserve other models when evaluation fails
            self._db.rollback()
            evaluation_error = f"Evaluation failed ({type(exc).__name__})."
            failure_reason = failure_reason or evaluation_error
            self._fail_run(
                run_id,
                failure_reason,
                category=classify_failure_text(failure_reason),
            )
            self._event(
                run_id,
                "model_comparison_evaluation_failed",
                {"comparison_id": str(comparison_id), "error_message": evaluation_error},
            )

        run = self._db.get(AgentRun, run_id)
        self._event(
            run_id,
            FINISHED_EVENT,
            {
                "comparison_id": str(comparison_id),
                "status": run.status,
                "failure_reason": _safe_reason(failure_reason),
            },
        )
        self._db.commit()

    def get_comparison(
        self, *, benchmark_task_id: UUID, comparison_id: UUID | None = None
    ) -> ModelComparisonResponse:
        self._task(benchmark_task_id)
        markers = (
            select(AgentEvent)
            .join(AgentRun, AgentRun.id == AgentEvent.agent_run_id)
            .where(
                AgentRun.benchmark_task_id == benchmark_task_id,
                AgentEvent.event_type == REQUESTED_EVENT,
            )
        )
        if comparison_id is None:
            latest = self._db.scalar(
                markers.order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc()).limit(1)
            )
            if latest is None:
                raise ModelComparisonNotFoundError("Model comparison not found for benchmark task.")
            comparison_id = UUID(latest.payload_json["comparison_id"])

        membership = list(
            self._db.scalars(
                markers.where(
                    AgentEvent.payload_json["comparison_id"].as_string() == str(comparison_id)
                )
            )
        )
        if not membership:
            raise ModelComparisonNotFoundError("Model comparison not found for benchmark task.")
        membership.sort(key=lambda event: event.payload_json["position"])
        run_ids = [event.agent_run_id for event in membership]
        records = {
            run.id: (run, metric)
            for run, metric in self._db.execute(
                select(AgentRun, EvaluationMetric)
                .outerjoin(EvaluationMetric, EvaluationMetric.agent_run_id == AgentRun.id)
                .where(AgentRun.id.in_(run_ids))
            )
        }
        failures = {
            event.agent_run_id: event.payload_json.get("failure_reason")
            for event in self._db.scalars(
                select(AgentEvent).where(
                    AgentEvent.agent_run_id.in_(run_ids), AgentEvent.event_type == FINISHED_EVENT
                )
            )
        }
        runs = []
        for run_id in run_ids:
            run, metric = records[run_id]
            values = {field: getattr(metric, field) for field in METRIC_FIELDS} if metric else {}
            runs.append(
                ModelComparisonRun(
                    run_id=run.id,
                    provider=run.model_provider,
                    model=run.model_name,
                    status=run.status,
                    metric_id=metric.id if metric else None,
                    failure_reason=_safe_reason(failures.get(run.id)),
                    repair_attempts_used=run.repair_attempts_used,
                    final_patch_id=run.final_patch_id,
                    final_patch_passed_tests=run.final_patch_passed_tests,
                    failure_summary=run.failure_summary,
                    failure_category=run.failure.category if run.failure else None,
                    **values,
                )
            )
        return ModelComparisonResponse(
            comparison_id=comparison_id,
            benchmark_task_id=benchmark_task_id,
            created_at=membership[0].created_at,
            status=_comparison_status(runs),
            runs=runs,
            aggregates=calculate_aggregates(runs),
        )

    def _task(self, task_id: UUID) -> BenchmarkTask:
        task = self._db.get(BenchmarkTask, task_id)
        if task is None:
            raise ModelComparisonNotFoundError("Benchmark task not found.")
        return task

    def _fail_run(self, run_id: UUID, reason: str, *, category: str) -> None:
        run = self._db.get(AgentRun, run_id)
        run.status = "failed"
        run.completed_at = run.completed_at or datetime.now(UTC)
        event = self._event(
            run_id,
            "agent_run_failed",
            {"error_message": _safe_reason(reason), "failure_category": category},
        )
        self._db.flush()
        FailureClassificationService(self._db).classify(
            agent_run_id=run_id,
            category=category,
            summary=reason,
            source_event_id=event.id,
        )

    def _event(
        self, run_id: UUID, event_type: str, payload: dict, *, created_at: datetime | None = None
    ) -> AgentEvent:
        event = AgentEvent(
            agent_run_id=run_id,
            event_type=event_type,
            payload_json=payload,
            created_at=created_at or datetime.now(UTC),
        )
        self._db.add(event)
        return event


def calculate_aggregates(runs: list[ModelComparisonRun]) -> ModelComparisonAggregates:
    passing = [
        run for run in runs if run.status == "completed" and run.tests_passed and run.patch_applied
    ]

    def best_key(run: ModelComparisonRun) -> tuple:
        return (
            -(run.file_localization_score or 0.0),
            run.unrelated_files_count if run.unrelated_files_count is not None else float("inf"),
            run.modified_files_count if run.modified_files_count is not None else float("inf"),
            run.estimated_cost if run.estimated_cost is not None else float("inf"),
            run.execution_time_seconds if run.execution_time_seconds is not None else float("inf"),
            run.provider,
            run.model,
            str(run.run_id),
        )

    def winner(candidates: list[ModelComparisonRun], key) -> ComparisonWinner | None:
        if not candidates:
            return None
        run = min(candidates, key=key)
        return ComparisonWinner(run_id=run.run_id, provider=run.provider, model=run.model)

    localization = [
        run.file_localization_score for run in runs if run.file_localization_score is not None
    ]
    return ModelComparisonAggregates(
        best_passing_model=winner(passing, best_key),
        lowest_cost_passing_model=winner(
            [run for run in passing if run.estimated_cost is not None],
            lambda run: (run.estimated_cost, best_key(run)),
        ),
        fastest_passing_model=winner(
            [run for run in passing if run.execution_time_seconds is not None],
            lambda run: (run.execution_time_seconds, best_key(run)),
        ),
        highest_localization_score=max(localization) if localization else None,
        total_cost=round(sum(run.estimated_cost or 0.0 for run in runs), 8),
        total_execution_time=round(sum(run.execution_time_seconds or 0.0 for run in runs), 4),
    )


def _comparison_status(runs: list[ModelComparisonRun]) -> str:
    statuses = {run.status for run in runs}
    if statuses == {"queued"}:
        return "queued"
    if statuses & {"queued", "running"}:
        return "running"
    if statuses == {"completed"}:
        return "completed"
    return "partial_failure" if "completed" in statuses else "failed"


def _safe_reason(reason: str | None) -> str | None:
    return redact_prompt_text(reason)[:2000] if reason else None
