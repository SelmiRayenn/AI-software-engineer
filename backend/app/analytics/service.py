from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.agent_run import AgentRun
from app.models.benchmark_pack import BenchmarkPack
from app.models.benchmark_pack_run import BenchmarkPackRun, BenchmarkPackRunTask
from app.models.benchmark_task import BenchmarkTask
from app.models.evaluation_metric import EvaluationMetric
from app.models.generated_patch import GeneratedPatch
from app.models.repository import Repository
from app.schemas.analytics import (
    AnalyticsSummary,
    ModelLeaderboardRow,
    PackAnalytics,
    RepositoryAnalytics,
)


@dataclass(frozen=True)
class AnalyticsFilters:
    benchmark_pack_id: UUID | None = None
    repository_id: UUID | None = None
    model_provider: str | None = None
    model_name: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


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

    def _load_runs(self, filters: AnalyticsFilters) -> list[AgentRun]:
        statement = select(AgentRun).options(
            joinedload(AgentRun.benchmark_task).joinedload(BenchmarkTask.repository),
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
