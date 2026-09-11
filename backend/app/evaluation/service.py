from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.run_statuses import RUN_STATUS_COMPLETED, RUN_STATUS_FAILED
from app.core.test_phases import TEST_PHASE_POST_PATCH
from app.models import AgentEvent, AgentRun, EvaluationMetric, GeneratedPatch, GoldPatch, TestResult


class EvaluationError(RuntimeError):
    pass


class EvaluationRunNotCompleteError(EvaluationError):
    pass


class EvaluationService:
    def __init__(self, *, db: Session, agent_run_id: UUID) -> None:
        self._db = db
        self._agent_run_id = agent_run_id
        self._agent_run = db.get(AgentRun, agent_run_id)
        if self._agent_run is None:
            raise EvaluationError("Agent run not found.")
        if self._agent_run.benchmark_task is None:
            raise EvaluationError("Agent run benchmark task not found.")

    def get_metrics(self) -> EvaluationMetric | None:
        return self._db.scalar(
            select(EvaluationMetric).where(EvaluationMetric.agent_run_id == self._agent_run_id)
        )

    def evaluate(self, *, include_failed: bool = False) -> EvaluationMetric:
        allowed_statuses = {RUN_STATUS_COMPLETED}
        if include_failed:
            allowed_statuses.add(RUN_STATUS_FAILED)
        if self._agent_run.status not in allowed_statuses:
            raise EvaluationRunNotCompleteError("Agent run must be completed before evaluation.")

        gold_patch = self._gold_patch()
        generated_patch = self._generated_patch()
        events = self._events()
        post_patch_results = self._post_patch_results(generated_patch)
        tokens_used, estimated_cost = self._tokens_and_cost(events)

        values = {
            "agent_run_id": self._agent_run_id,
            "file_localization_score": self._file_localization_score(events, gold_patch),
            "patch_applied": self._patch_applied(generated_patch, events),
            "tests_passed": bool(post_patch_results)
            and all(result.passed for result in post_patch_results)
            and (
                not self._agent_run.final_patch_id
                or self._agent_run.final_patch_passed_tests is not False
            ),
            "modified_files_count": len(_normalized_files(generated_patch.changed_files))
            if generated_patch is not None
            else 0,
            "unrelated_files_count": self._unrelated_files_count(generated_patch, gold_patch),
            "tokens_used": tokens_used,
            "estimated_cost": estimated_cost,
            "execution_time_seconds": self._execution_time_seconds(),
        }

        metric = self.get_metrics()
        if metric is None:
            metric = EvaluationMetric(**values)
            self._db.add(metric)
        else:
            for key, value in values.items():
                setattr(metric, key, value)

        self._db.commit()
        self._db.refresh(metric)
        self._log_evaluation(metric)
        return metric

    def _gold_patch(self) -> GoldPatch:
        gold_patch = self._db.scalar(
            select(GoldPatch).where(
                GoldPatch.benchmark_task_id == self._agent_run.benchmark_task_id
            )
        )
        if gold_patch is None:
            raise EvaluationError("Gold patch not found for benchmark task.")
        return gold_patch

    def _generated_patch(self) -> GeneratedPatch | None:
        return self._agent_run.generated_patch

    def _events(self) -> list[AgentEvent]:
        return list(
            self._db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == self._agent_run_id)
                .order_by(AgentEvent.created_at.asc())
            ).all()
        )

    def _post_patch_results(self, patch: GeneratedPatch | None) -> list[TestResult]:
        results = list(
            self._db.scalars(
                select(TestResult)
                .where(
                    TestResult.agent_run_id == self._agent_run_id,
                    TestResult.phase == TEST_PHASE_POST_PATCH,
                )
                .order_by(TestResult.created_at.asc())
            ).all()
        )
        if patch and patch.is_selected:
            results = [result for result in results if result.generated_patch_id == patch.id]
            attempts = [
                result.attempt_number for result in results if result.attempt_number is not None
            ]
            if attempts:
                results = [result for result in results if result.attempt_number == max(attempts)]
        return results

    def _file_localization_score(self, events: list[AgentEvent], gold_patch: GoldPatch) -> float:
        gold_files = _normalized_files(gold_patch.changed_files)
        if not gold_files:
            return 0.0

        inspected_files: set[str] = set()
        for event in events:
            payload = event.payload_json or {}
            for file_path in payload.get("files_read", []) or []:
                inspected_files.add(_normalize_file_path(file_path))

        matched_files = inspected_files & gold_files
        return round(len(matched_files) / len(gold_files), 4)

    def _patch_applied(
        self,
        generated_patch: GeneratedPatch | None,
        events: list[AgentEvent],
    ) -> bool:
        if generated_patch is None or not generated_patch.patch_text.strip():
            return False

        generated_patch_id = str(generated_patch.id)
        attempts = [
            event.payload_json
            for event in events
            if event.event_type == "repair_attempt_completed"
            and event.payload_json.get("generated_patch_id") == generated_patch_id
        ]
        if attempts and attempts[-1].get("outcome") in {"invalid_patch", "error"}:
            return False
        legacy = not attempts and generated_patch.version == 1
        for event in events:
            payload = event.payload_json or {}
            if event.event_type == "patch_applied":
                event_patch_id = payload.get("generated_patch_id")
                if (
                    event_patch_id == generated_patch_id or (event_patch_id is None and legacy)
                ) and payload.get("applied") is not False:
                    return True
            if (
                event.event_type == "test_phase_completed"
                and payload.get("phase") == TEST_PHASE_POST_PATCH
                and payload.get("patch_status") in {"applied", "already_applied"}
                and (
                    payload.get("generated_patch_id") == generated_patch_id
                    or (payload.get("generated_patch_id") is None and legacy)
                )
            ):
                return True
        return False

    def _unrelated_files_count(
        self,
        generated_patch: GeneratedPatch | None,
        gold_patch: GoldPatch,
    ) -> int:
        if generated_patch is None:
            return 0
        generated_files = _normalized_files(generated_patch.changed_files)
        gold_files = _normalized_files(gold_patch.changed_files)
        return len(generated_files - gold_files)

    def _tokens_and_cost(self, events: list[AgentEvent]) -> tuple[int, float]:
        if self._agent_run.model_provider == "mock":
            return 0, 0.0

        tokens_used = 0
        estimated_cost = 0.0
        for event in events:
            if event.event_type != "model_response":
                continue
            payload = event.payload_json or {}
            tokens_used += _int_value(payload.get("input_tokens"))
            tokens_used += _int_value(payload.get("output_tokens"))
            estimated_cost += _float_value(payload.get("estimated_cost"))
        return tokens_used, round(estimated_cost, 8)

    def _execution_time_seconds(self) -> float | None:
        if self._agent_run.started_at is None or self._agent_run.completed_at is None:
            return None
        return max(
            0.0,
            round(
                (self._agent_run.completed_at - self._agent_run.started_at).total_seconds(),
                4,
            ),
        )

    def _log_evaluation(self, metric: EvaluationMetric) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=self._agent_run_id,
                event_type="evaluation_metric_calculated",
                payload_json={
                    "evaluation_metric_id": str(metric.id),
                    "file_localization_score": metric.file_localization_score,
                    "patch_applied": metric.patch_applied,
                    "tests_passed": metric.tests_passed,
                    "modified_files_count": metric.modified_files_count,
                    "unrelated_files_count": metric.unrelated_files_count,
                    "tokens_used": metric.tokens_used,
                    "estimated_cost": metric.estimated_cost,
                    "execution_time_seconds": metric.execution_time_seconds,
                },
            )
        )
        self._db.commit()


def _normalized_files(files: list[str] | None) -> set[str]:
    return {_normalize_file_path(file_path) for file_path in files or [] if file_path}


def _normalize_file_path(file_path: str) -> str:
    return file_path.replace("\\", "/").removeprefix("./").removeprefix("a/").removeprefix("b/")


def _int_value(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_value(value: object) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
