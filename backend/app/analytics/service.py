from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.analytics.file_localization import build_file_localization_analytics
from app.models.agent_event import AgentEvent
from app.models.agent_run import AgentRun
from app.models.benchmark_pack import BenchmarkPack
from app.models.benchmark_pack_run import BenchmarkPackRun, BenchmarkPackRunTask
from app.models.benchmark_task import BenchmarkTask
from app.models.evaluation_metric import EvaluationMetric
from app.models.generated_patch import GeneratedPatch
from app.models.repository import Repository
from app.schemas.analytics import (
    AnalyticsSummary,
    FileLocalizationAnalytics,
    ModelLeaderboardRow,
    PackAnalytics,
    RepositoryAnalytics,
    ToolErrorsByModel,
    ToolFailureCount,
    ToolUsageAnalytics,
    ToolUsageCount,
)


@dataclass(frozen=True)
class AnalyticsFilters:
    benchmark_pack_id: UUID | None = None
    repository_id: UUID | None = None
    model_provider: str | None = None
    model_name: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


@dataclass(frozen=True)
class _ToolCallRecord:
    run_id: UUID
    tool_name: str
    success: bool
    error_type: str | None = None


class AnalyticsService:
    """Aggregate persisted run outcomes without exposing benchmark gold data."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def summary(self, filters: AnalyticsFilters) -> AnalyticsSummary:
        return self._aggregate(self._load_runs(filters))

    def by_repository(self, filters: AnalyticsFilters) -> list[RepositoryAnalytics]:
        grouped: dict[UUID, list[AgentRun]] = defaultdict(list)
        repositories: dict[UUID, Repository] = {}

        for run in self._load_runs(filters):
            repository = run.benchmark_task.repository
            grouped[repository.id].append(run)
            repositories[repository.id] = repository

        results = [
            RepositoryAnalytics(
                repository_id=repository_id,
                repository_owner=repositories[repository_id].owner,
                repository_name=repositories[repository_id].name,
                repository_url=repositories[repository_id].url,
                **self._aggregate(runs).model_dump(),
            )
            for repository_id, runs in grouped.items()
        ]
        return sorted(
            results,
            key=lambda item: (
                item.repository_owner.lower(),
                item.repository_name.lower(),
                str(item.repository_id),
            ),
        )

    def by_pack(self, filters: AnalyticsFilters) -> list[PackAnalytics]:
        runs = self._load_runs(filters)
        if not runs:
            return []

        run_by_id = {run.id: run for run in runs}
        statement = (
            select(BenchmarkPackRunTask, BenchmarkPackRun, BenchmarkPack)
            .join(
                BenchmarkPackRun,
                BenchmarkPackRun.id == BenchmarkPackRunTask.benchmark_pack_run_id,
            )
            .join(BenchmarkPack, BenchmarkPack.id == BenchmarkPackRun.benchmark_pack_id)
            .where(BenchmarkPackRunTask.agent_run_id.in_(run_by_id))
        )
        if filters.benchmark_pack_id is not None:
            statement = statement.where(
                BenchmarkPackRun.benchmark_pack_id == filters.benchmark_pack_id
            )

        grouped: dict[UUID, list[AgentRun]] = defaultdict(list)
        packs: dict[UUID, BenchmarkPack] = {}
        for run_task, pack_run, pack in self.db.execute(statement).all():
            grouped[pack_run.benchmark_pack_id].append(run_by_id[run_task.agent_run_id])
            packs[pack.id] = pack

        results = [
            PackAnalytics(
                benchmark_pack_id=pack_id,
                pack_name=packs[pack_id].name,
                pack_slug=packs[pack_id].slug,
                pack_version=packs[pack_id].version,
                **self._aggregate(pack_runs).model_dump(),
            )
            for pack_id, pack_runs in grouped.items()
        ]
        return sorted(results, key=lambda item: (item.pack_slug, item.pack_version))

    def model_leaderboard(
        self, filters: AnalyticsFilters, *, min_runs: int = 1
    ) -> list[ModelLeaderboardRow]:
        grouped: dict[tuple[str, str], list[AgentRun]] = defaultdict(list)
        for run in self._load_runs(filters):
            grouped[(run.model_provider, run.model_name)].append(run)

        rows = [
            self._leaderboard_row(provider, model, runs)
            for (provider, model), runs in grouped.items()
            if len(runs) >= min_runs
        ]
        self._assign_competition_ranks(
            rows,
            value_field="issue_resolved_rate",
            rank_field="rank_by_issue_resolved",
            descending=True,
        )
        self._assign_competition_ranks(
            rows,
            value_field="average_cost_per_run",
            rank_field="rank_by_cost",
            descending=False,
        )
        self._assign_competition_ranks(
            rows,
            value_field="average_execution_time_seconds",
            rank_field="rank_by_speed",
            descending=False,
        )
        self._assign_competition_ranks(
            rows,
            value_field="average_file_localization_score",
            rank_field="rank_by_localization",
            descending=True,
        )
        return sorted(
            rows,
            key=lambda row: (
                -row.composite_score,
                -row.issue_resolved_rate,
                row.average_cost_per_run,
                row.model_provider.lower(),
                row.model_name.lower(),
            ),
        )

    def tool_usage(self, filters: AnalyticsFilters) -> ToolUsageAnalytics:
        runs = self._load_runs(filters)
        if not runs:
            return ToolUsageAnalytics()

        records = self._load_tool_call_records(runs)
        used_counts = Counter(record.tool_name for record in records)
        failed_records = [record for record in records if not record.success]
        failed_counts = Counter(record.tool_name for record in failed_records)
        error_type_counts = Counter(
            record.error_type or "tool_error" for record in failed_records
        )

        failed_run_ids = {record.run_id for record in failed_records}
        total_calls = len(records)

        run_by_id = {run.id: run for run in runs}
        model_records: dict[tuple[str, str], list[_ToolCallRecord]] = defaultdict(list)
        for record in records:
            run = run_by_id[record.run_id]
            model_records[(run.model_provider, run.model_name)].append(record)

        return ToolUsageAnalytics(
            total_tool_calls=total_calls,
            successful_tool_calls=sum(record.success for record in records),
            failed_tool_calls=len(failed_records),
            unknown_tool_calls=error_type_counts["unknown_tool"],
            malformed_tool_calls=error_type_counts["malformed_tool_call"],
            tool_error_rate=len(failed_records) / total_calls if total_calls else 0.0,
            average_tool_calls_per_run=total_calls / len(runs),
            most_used_tools=[
                ToolUsageCount(tool_name=tool_name, call_count=count)
                for tool_name, count in _sorted_counts(used_counts)
            ],
            most_failed_tools=[
                ToolFailureCount(tool_name=tool_name, failed_count=count)
                for tool_name, count in _sorted_counts(failed_counts)
            ],
            tool_error_counts_by_type=dict(sorted(error_type_counts.items())),
            runs_with_tool_errors=len(failed_run_ids),
            tool_errors_by_model=[
                self._tool_model_row(provider, model, model_calls)
                for (provider, model), model_calls in sorted(model_records.items())
            ],
        )

    def file_localization(self, filters: AnalyticsFilters) -> FileLocalizationAnalytics:
        return build_file_localization_analytics(self.db, self._load_runs(filters))

    def _load_tool_call_records(self, runs: list[AgentRun]) -> list[_ToolCallRecord]:
        run_ids = [run.id for run in runs]
        events = list(
            self.db.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.agent_run_id.in_(run_ids),
                    AgentEvent.event_type.in_(
                        [
                            "tool_call_requested",
                            "tool_call_completed",
                            "tool_call_failed",
                            "agent_tool_call",
                        ]
                    ),
                )
                .order_by(AgentEvent.agent_run_id, AgentEvent.created_at, AgentEvent.id)
            ).all()
        )
        events_by_run: dict[UUID, list[AgentEvent]] = defaultdict(list)
        for event in events:
            events_by_run[event.agent_run_id].append(event)

        records: list[_ToolCallRecord] = []
        for run in runs:
            run_events = events_by_run[run.id]
            requests = [event for event in run_events if event.event_type == "tool_call_requested"]
            if requests:
                records.extend(self._modern_tool_records(run.id, requests, run_events))
                continue
            records.extend(self._legacy_tool_records(run.id, run_events))
        return records

    @staticmethod
    def _modern_tool_records(
        run_id: UUID,
        requests: list[AgentEvent],
        events: list[AgentEvent],
    ) -> list[_ToolCallRecord]:
        terminal_by_id: dict[str, AgentEvent] = {}
        terminal_without_id: list[AgentEvent] = []
        for event in events:
            if event.event_type not in {"tool_call_completed", "tool_call_failed"}:
                continue
            call_id = event.payload_json.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                current = terminal_by_id.get(call_id)
                if current is None or event.event_type == "tool_call_failed":
                    terminal_by_id[call_id] = event
            else:
                terminal_without_id.append(event)

        unmatched = list(terminal_without_id)
        records: list[_ToolCallRecord] = []
        for request in requests:
            raw_call = request.payload_json.get("tool_call")
            call = raw_call if isinstance(raw_call, dict) else {}
            call_id = call.get("id") if isinstance(call.get("id"), str) else None
            raw_name = call.get("name")
            tool_name = raw_name if isinstance(raw_name, str) and raw_name else "malformed_tool_call"
            outcome = terminal_by_id.get(call_id) if call_id else None
            if outcome is None:
                outcome = _take_matching_terminal(unmatched, tool_name)

            if outcome is None:
                records.append(
                    _ToolCallRecord(run_id, tool_name, False, "incomplete_tool_call")
                )
            elif outcome.event_type == "tool_call_completed":
                records.append(_ToolCallRecord(run_id, tool_name, True))
            else:
                records.append(
                    _ToolCallRecord(
                        run_id,
                        tool_name,
                        False,
                        _tool_error_type(outcome.payload_json, tool_name),
                    )
                )
        return records

    @staticmethod
    def _legacy_tool_records(
        run_id: UUID, events: list[AgentEvent]
    ) -> list[_ToolCallRecord]:
        records: list[_ToolCallRecord] = []
        for event in events:
            if event.event_type != "agent_tool_call":
                continue
            payload = event.payload_json
            raw_name = payload.get("tool_name")
            tool_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown_tool"
            success = payload.get("success") is True
            records.append(
                _ToolCallRecord(
                    run_id,
                    tool_name,
                    success,
                    None if success else _tool_error_type(payload, tool_name),
                )
            )
        return records

    @staticmethod
    def _tool_model_row(
        provider: str, model: str, records: list[_ToolCallRecord]
    ) -> ToolErrorsByModel:
        failed = [record for record in records if not record.success]
        return ToolErrorsByModel(
            model_provider=provider,
            model_name=model,
            total_tool_calls=len(records),
            failed_tool_calls=len(failed),
            unknown_tool_calls=sum(record.error_type == "unknown_tool" for record in failed),
            malformed_tool_calls=sum(
                record.error_type == "malformed_tool_call" for record in failed
            ),
            tool_error_rate=len(failed) / len(records) if records else 0.0,
            runs_with_tool_errors=len({record.run_id for record in failed}),
        )

    def _load_runs(self, filters: AnalyticsFilters) -> list[AgentRun]:
        statement = select(AgentRun).options(
            joinedload(AgentRun.benchmark_task).joinedload(BenchmarkTask.repository),
            joinedload(AgentRun.benchmark_task).joinedload(BenchmarkTask.gold_patch),
            joinedload(AgentRun.evaluation_metric),
            selectinload(AgentRun.generated_patches).selectinload(GeneratedPatch.human_review),
        )

        if filters.repository_id is not None:
            statement = statement.where(
                AgentRun.benchmark_task.has(BenchmarkTask.repository_id == filters.repository_id)
            )
        if filters.model_provider is not None:
            statement = statement.where(AgentRun.model_provider == filters.model_provider)
        if filters.model_name is not None:
            statement = statement.where(AgentRun.model_name == filters.model_name)
        if filters.date_from is not None:
            statement = statement.where(AgentRun.started_at >= filters.date_from)
        if filters.date_to is not None:
            statement = statement.where(AgentRun.started_at <= filters.date_to)
        if filters.benchmark_pack_id is not None:
            pack_run_exists = (
                select(BenchmarkPackRunTask.id)
                .join(
                    BenchmarkPackRun,
                    BenchmarkPackRun.id == BenchmarkPackRunTask.benchmark_pack_run_id,
                )
                .where(
                    BenchmarkPackRunTask.agent_run_id == AgentRun.id,
                    BenchmarkPackRun.benchmark_pack_id == filters.benchmark_pack_id,
                )
                .exists()
            )
            statement = statement.where(pack_run_exists)

        return list(self.db.scalars(statement.order_by(AgentRun.started_at, AgentRun.id)).all())

    @staticmethod
    def _aggregate(runs: list[AgentRun]) -> AnalyticsSummary:
        metrics = [run.evaluation_metric for run in runs if run.evaluation_metric is not None]
        hidden_metrics = [metric for metric in metrics if metric.hidden_tests_run_count > 0]
        reviews = [
            patch.human_review
            for run in runs
            for patch in run.generated_patches
            if patch.human_review is not None
        ]

        total_cost = sum(metric.estimated_cost or 0.0 for metric in metrics)
        total_runs = len(runs)
        return AnalyticsSummary(
            total_runs=total_runs,
            completed_runs=sum(run.status == "completed" for run in runs),
            failed_runs=sum(run.status == "failed" for run in runs),
            approved_patches=sum(review.decision == "approved" for review in reviews),
            rejected_patches=sum(review.decision == "rejected" for review in reviews),
            patch_apply_rate=AnalyticsService._rate(metrics, "patch_applied"),
            visible_test_pass_rate=AnalyticsService._rate(metrics, "post_patch_tests_passed"),
            hidden_test_pass_rate=(
                AnalyticsService._rate(hidden_metrics, "hidden_tests_passed")
                if hidden_metrics
                else None
            ),
            issue_resolved_rate=AnalyticsService._rate(metrics, "issue_resolved"),
            regression_rate=AnalyticsService._rate(metrics, "regression_detected"),
            average_file_localization_score=AnalyticsService._average(
                metric.file_localization_score for metric in metrics
            ),
            average_issue_specific_score=AnalyticsService._average(
                metric.issue_specific_score for metric in metrics
            ),
            average_modified_files_count=AnalyticsService._average(
                metric.modified_files_count for metric in metrics
            ),
            average_unrelated_files_count=AnalyticsService._average(
                metric.unrelated_files_count for metric in metrics
            ),
            total_tokens=sum(metric.tokens_used or 0 for metric in metrics),
            total_cost=total_cost,
            average_cost_per_run=total_cost / total_runs if total_runs else 0.0,
            average_execution_time_seconds=AnalyticsService._average(
                metric.execution_time_seconds for metric in metrics
            ),
        )

    @staticmethod
    def _rate(metrics: list[EvaluationMetric], attribute: str) -> float:
        if not metrics:
            return 0.0
        return sum(bool(getattr(metric, attribute)) for metric in metrics) / len(metrics)

    @staticmethod
    def _average(values: Iterable[float | int | None]) -> float:
        present_values = [float(value) for value in values if value is not None]
        return fmean(present_values) if present_values else 0.0

    @staticmethod
    def _leaderboard_row(provider: str, model: str, runs: list[AgentRun]) -> ModelLeaderboardRow:
        summary = AnalyticsService._aggregate(runs)
        average_tokens = summary.total_tokens / summary.total_runs if summary.total_runs else 0.0
        composite_score = AnalyticsService._composite_score(summary)
        return ModelLeaderboardRow(
            model_provider=provider,
            model_name=model,
            total_runs=summary.total_runs,
            completed_runs=summary.completed_runs,
            failed_runs=summary.failed_runs,
            issue_resolved_rate=summary.issue_resolved_rate,
            visible_test_pass_rate=summary.visible_test_pass_rate,
            hidden_test_pass_rate=summary.hidden_test_pass_rate,
            average_file_localization_score=summary.average_file_localization_score,
            average_issue_specific_score=summary.average_issue_specific_score,
            average_cost_per_run=summary.average_cost_per_run,
            average_tokens_per_run=average_tokens,
            average_execution_time_seconds=summary.average_execution_time_seconds,
            average_modified_files_count=summary.average_modified_files_count,
            average_unrelated_files_count=summary.average_unrelated_files_count,
            composite_score=composite_score,
        )

    @staticmethod
    def _composite_score(summary: AnalyticsSummary) -> float:
        hidden_rate = summary.hidden_test_pass_rate or 0.0
        cost_penalty = 0.04 * min(max(summary.average_cost_per_run, 0.0) / 1.0, 1.0)
        speed_penalty = 0.03 * min(max(summary.average_execution_time_seconds, 0.0) / 600.0, 1.0)
        unrelated_penalty = 0.03 * min(max(summary.average_unrelated_files_count, 0.0) / 5.0, 1.0)
        performance = (
            0.55 * summary.issue_resolved_rate
            + 0.15 * summary.visible_test_pass_rate
            + 0.10 * hidden_rate
            + 0.20 * summary.average_file_localization_score
        )
        return round(
            max(0.0, min(1.0, performance - cost_penalty - speed_penalty - unrelated_penalty)),
            6,
        )

    @staticmethod
    def _assign_competition_ranks(
        rows: list[ModelLeaderboardRow],
        *,
        value_field: str,
        rank_field: str,
        descending: bool,
    ) -> None:
        ordered = sorted(
            rows,
            key=lambda row: getattr(row, value_field),
            reverse=descending,
        )
        previous_value: float | None = None
        previous_rank = 0
        for position, row in enumerate(ordered, start=1):
            value = float(getattr(row, value_field))
            if previous_value is None or value != previous_value:
                previous_rank = position
                previous_value = value
            setattr(row, rank_field, previous_rank)


def _take_matching_terminal(events: list[AgentEvent], tool_name: str) -> AgentEvent | None:
    for index, event in enumerate(events):
        if (
            event.payload_json.get("tool_name") == tool_name
            and event.event_type == "tool_call_failed"
        ):
            return events.pop(index)
    for index, event in enumerate(events):
        if event.payload_json.get("tool_name") == tool_name:
            return events.pop(index)
    return events.pop(0) if events else None


def _tool_error_type(payload: dict, tool_name: str) -> str:
    category = payload.get("failure_category")
    if isinstance(category, str) and category:
        return category
    if tool_name == "malformed_tool_call":
        return "malformed_tool_call"

    message = str(payload.get("error_message") or "").lower()
    if "unknown tool" in message or "not registered" in message:
        return "unknown_tool"
    if "malformed" in message or "invalid tool call" in message:
        return "malformed_tool_call"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    return "tool_error"


def _sorted_counts(counts: Counter[str]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
