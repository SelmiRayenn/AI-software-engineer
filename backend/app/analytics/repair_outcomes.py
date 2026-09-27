from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import fmean
from uuid import UUID

from app.models import AgentEvent, AgentRun, Repository
from app.schemas.analytics import (
    RepairFailureCount,
    RepairOutcomeAnalytics,
    RepairOutcomeMetricSet,
    RepairSuccessByModel,
    RepairSuccessByRepository,
)


@dataclass(frozen=True)
class _Attempt:
    outcome: str
    failure_category: str | None

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"

    @property
    def failed(self) -> bool:
        return self.outcome in {"error", "failed_tests", "invalid_patch"}


@dataclass(frozen=True)
class _RunOutcome:
    run: AgentRun
    attempts: list[_Attempt]
    repair_attempts: int
    final_patch_passed: bool
    exhausted: bool

    @property
    def has_repairs(self) -> bool:
        return self.repair_attempts > 0

    @property
    def first_patch_failed(self) -> bool:
        return bool(self.attempts and self.attempts[0].failed)

    @property
    def repair_succeeded(self) -> bool:
        return self.has_repairs and self.first_patch_failed and self.final_patch_passed


def build_repair_outcome_analytics(runs: list[AgentRun]) -> RepairOutcomeAnalytics:
    records = [_run_outcome(run) for run in runs]
    repair_records = [record for record in records if record.has_repairs]
    initial_categories = Counter(
        record.attempts[0].failure_category
        for record in records
        if record.attempts and record.attempts[0].failed and record.attempts[0].failure_category
    )
    repair_categories: Counter[str] = Counter()
    for record in repair_records:
        repair_categories.update(
            attempt.failure_category
            for attempt in record.attempts[1:]
            if attempt.failed and attempt.failure_category
        )
        if record.exhausted:
            repair_categories["max_repair_attempts_reached"] += 1

    attempted = [record for record in records if record.attempts]
    model_groups: dict[tuple[str, str], list[_RunOutcome]] = defaultdict(list)
    repository_groups: dict[UUID, list[_RunOutcome]] = defaultdict(list)
    repositories: dict[UUID, Repository] = {}
    for record in repair_records:
        model_groups[(record.run.model_provider, record.run.model_name)].append(record)
        repository = record.run.benchmark_task.repository
        repository_groups[repository.id].append(record)
        repositories[repository.id] = repository

    return RepairOutcomeAnalytics(
        **_metric_set(repair_records).model_dump(),
        first_patch_pass_rate=(
            sum(record.attempts[0].passed for record in attempted) / len(attempted)
            if attempted
            else 0.0
        ),
        most_common_initial_failure_categories=_failure_counts(initial_categories),
        most_common_repair_failure_categories=_failure_counts(repair_categories),
        repair_success_by_model=[
            RepairSuccessByModel(
                model_provider=provider,
                model_name=model,
                **_metric_set(group).model_dump(),
            )
            for (provider, model), group in sorted(model_groups.items())
        ],
        repair_success_by_repository=[
            RepairSuccessByRepository(
                repository_id=repository_id,
                repository_owner=repositories[repository_id].owner,
                repository_name=repositories[repository_id].name,
                repository_url=repositories[repository_id].url,
                **_metric_set(group).model_dump(),
            )
            for repository_id, group in sorted(
                repository_groups.items(),
                key=lambda item: (
                    repositories[item[0]].owner.lower(),
                    repositories[item[0]].name.lower(),
                    str(item[0]),
                ),
            )
        ],
    )


def _metric_set(records: list[_RunOutcome]) -> RepairOutcomeMetricSet:
    if not records:
        return RepairOutcomeMetricSet()
    initially_failed = [record for record in records if record.first_patch_failed]
    return RepairOutcomeMetricSet(
        total_runs_with_repairs=len(records),
        average_repair_attempts=fmean(record.repair_attempts for record in records),
        repair_success_rate=(
            sum(record.repair_succeeded for record in initially_failed) / len(initially_failed)
            if initially_failed
            else 0.0
        ),
        repaired_patch_pass_rate=sum(record.final_patch_passed for record in records)
        / len(records),
        attempts_exhausted_rate=sum(record.exhausted for record in records) / len(records),
        average_cost_with_repairs=fmean(
            (record.run.evaluation_metric.estimated_cost or 0.0)
            if record.run.evaluation_metric
            else 0.0
            for record in records
        ),
        average_time_with_repairs=fmean(
            (record.run.evaluation_metric.execution_time_seconds or 0.0)
            if record.run.evaluation_metric
            else 0.0
            for record in records
        ),
    )


def _run_outcome(run: AgentRun) -> _RunOutcome:
    events = sorted(run.events, key=lambda event: (event.created_at, event.id))
    completed = [event for event in events if event.event_type == "repair_attempt_completed"]
    attempts = [_event_attempt(event, run) for event in completed]
    if not attempts:
        attempts = _legacy_attempts(run)

    repair_attempts = max(
        run.repair_attempts_used,
        max(0, len(attempts) - 1),
        max(0, len(run.generated_patches) - 1),
    )
    exhausted = any(event.event_type == "repair_limit_reached" for event in events) or bool(
        run.failure and run.failure.category == "max_repair_attempts_reached"
    )
    metric = run.evaluation_metric
    final_patch_passed = bool(
        metric.post_patch_tests_passed
        if metric is not None
        else run.final_patch_passed_tests is True
    )
    return _RunOutcome(
        run=run,
        attempts=attempts,
        repair_attempts=repair_attempts,
        final_patch_passed=final_patch_passed,
        exhausted=exhausted,
    )


def _event_attempt(event: AgentEvent, run: AgentRun) -> _Attempt:
    payload = event.payload_json or {}
    outcome = str(payload.get("outcome") or "unknown")
    raw_category = payload.get("failure_category")
    category = raw_category if isinstance(raw_category, str) and raw_category else None
    if category is None:
        category = _derived_failure_category(outcome, str(payload.get("failure_summary") or ""))
    if category is None and outcome in {"error", "invalid_patch"} and run.failure:
        category = run.failure.category
    return _Attempt(outcome=outcome, failure_category=category)


def _legacy_attempts(run: AgentRun) -> list[_Attempt]:
    grouped: dict[int, list[bool]] = defaultdict(list)
    for result in run.test_results:
        if result.phase != "post_patch" or result.attempt_number is None:
            continue
        grouped[result.attempt_number].append(result.passed)
    return [
        _Attempt(
            outcome="passed" if results and all(results) else "failed_tests",
            failure_category=None if results and all(results) else "post_patch_tests_failed",
        )
        for _, results in sorted(grouped.items())
    ]


def _derived_failure_category(outcome: str, summary: str) -> str | None:
    if outcome == "failed_tests":
        return "post_patch_tests_failed"
    if outcome == "error":
        return "unknown"
    if outcome != "invalid_patch":
        return None
    lowered = summary.lower()
    if "quality" in lowered or "guardrail" in lowered:
        return "patch_quality_blocked"
    if "apply" in lowered or "does not match" in lowered:
        return "patch_apply_failed"
    return "patch_generation_failed"


def _failure_counts(counts: Counter[str]) -> list[RepairFailureCount]:
    return [
        RepairFailureCount(category=category, count=count)
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
