from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.agents.prompts import redact_prompt_text
from app.analytics.service import AnalyticsFilters, AnalyticsService
from app.core.config import Settings, settings
from app.models import (
    AgentRun,
    BenchmarkPack,
    BenchmarkPackRun,
    BenchmarkPackRunTask,
    BenchmarkTask,
    GeneratedPatch,
)
from app.schemas.public_demo_report import (
    PublicDemoFilters,
    PublicDemoPack,
    PublicDemoRun,
    PublicDemoSnapshot,
    PublicDemoUsageSummary,
)

MAX_PUBLIC_LEADERBOARD_ROWS = 10


class PublicDemoSnapshotService:
    def __init__(self, db: Session, *, app_settings: Settings = settings) -> None:
        self.db = db
        self.settings = app_settings

    def build(
        self,
        *,
        benchmark_pack_id: UUID | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        limit: int = 20,
    ) -> PublicDemoSnapshot:
        filters = AnalyticsFilters(
            benchmark_pack_id=benchmark_pack_id,
            model_provider=model_provider,
            model_name=model_name,
        )
        runs = self._load_runs(filters)
        analytics = AnalyticsService(self.db)
        aggregate = analytics.summary_for_runs(runs)
        leaderboard = analytics.model_leaderboard_for_runs(runs)
        safe_leaderboard = [
            row.model_copy(
                update={
                    "model_provider": _safe_text(row.model_provider),
                    "model_name": _safe_text(row.model_name),
                }
            )
            for row in leaderboard[:MAX_PUBLIC_LEADERBOARD_ROWS]
        ]
        successful, failed = _select_examples(runs, limit)
        return PublicDemoSnapshot(
            generated_at=datetime.now(UTC),
            filters=PublicDemoFilters(
                benchmark_pack_id=benchmark_pack_id,
                model_provider=_safe_text(model_provider) if model_provider else None,
                model_name=_safe_text(model_name) if model_name else None,
                limit=limit,
            ),
            benchmark_pack=self._pack_summary(benchmark_pack_id),
            aggregate_metrics=aggregate,
            model_leaderboard=safe_leaderboard,
            usage=_usage_summary(runs, aggregate.average_cost_per_run),
            successful_runs=[self._run_summary(run) for run in successful],
            failed_runs=[self._run_summary(run) for run in failed],
            notes=_snapshot_notes(
                has_runs=bool(runs),
                redact_urls=self.settings.public_demo_redact_repository_urls,
                limit=limit,
            ),
        )

    def render_markdown(self, snapshot: PublicDemoSnapshot) -> str:
        metrics = snapshot.aggregate_metrics
        lines = [
            "# Public Benchmark Results Snapshot",
            "",
            f"Generated: {snapshot.generated_at.isoformat()}",
            "",
            "## Scope",
            "",
            f"- Benchmark pack: {_pack_label(snapshot)}",
            f"- Model provider: `{snapshot.filters.model_provider or 'all'}`",
            f"- Model name: `{snapshot.filters.model_name or 'all'}`",
            f"- Example limit: {snapshot.filters.limit}",
            "",
            "## Results Overview",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Total runs | {metrics.total_runs} |",
            f"| Completed runs | {metrics.completed_runs} |",
            f"| Failed runs | {metrics.failed_runs} |",
            f"| Patch apply rate | {_percent(metrics.patch_apply_rate)} |",
            f"| Visible test pass rate | {_percent(metrics.visible_test_pass_rate)} |",
            f"| Hidden test pass rate | {_percent(metrics.hidden_test_pass_rate)} |",
            f"| Issue resolved rate | {_percent(metrics.issue_resolved_rate)} |",
            f"| Regression rate | {_percent(metrics.regression_rate)} |",
            (f"| Average file localization | {metrics.average_file_localization_score:.4f} |"),
            f"| Average issue score | {metrics.average_issue_specific_score:.4f} |",
            "",
            "## Cost, Tokens, and Time",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Total tokens | {snapshot.usage.total_tokens} |",
            f"| Total estimated cost | ${snapshot.usage.total_estimated_cost:.8f} |",
            f"| Average cost per run | ${snapshot.usage.average_cost_per_run:.8f} |",
            (f"| Total execution time | {snapshot.usage.total_execution_time_seconds:.4f}s |"),
            (f"| Average execution time | {snapshot.usage.average_execution_time_seconds:.4f}s |"),
            "",
            "## Model Leaderboard",
            "",
        ]
        if snapshot.model_leaderboard:
            lines.extend(
                [
                    (
                        "| Model | Runs | Resolved | Visible | Hidden | Localization | "
                        "Cost/run | Time/run | Composite |"
                    ),
                    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for row in snapshot.model_leaderboard:
                lines.append(
                    f"| `{_md_cell(row.model_provider)}/{_md_cell(row.model_name)}` | "
                    f"{row.total_runs} | {_percent(row.issue_resolved_rate)} | "
                    f"{_percent(row.visible_test_pass_rate)} | "
                    f"{_percent(row.hidden_test_pass_rate)} | "
                    f"{row.average_file_localization_score:.4f} | "
                    f"${row.average_cost_per_run:.8f} | "
                    f"{row.average_execution_time_seconds:.4f}s | "
                    f"{row.composite_score:.4f} |"
                )
        else:
            lines.append("No model results matched the selected filters.")

        _append_run_table(lines, "Selected Successful Runs", snapshot.successful_runs)
        _append_run_table(lines, "Selected Failed Runs", snapshot.failed_runs)
        lines.extend(["", "## Sharing Notes", ""])
        lines.extend(f"- {_safe_text(note)}" for note in snapshot.notes)
        return "\n".join(lines).rstrip() + "\n"

    def _load_runs(self, filters: AnalyticsFilters) -> list[AgentRun]:
        statement = select(AgentRun).options(
            joinedload(AgentRun.benchmark_task).joinedload(BenchmarkTask.repository),
            joinedload(AgentRun.evaluation_metric),
            joinedload(AgentRun.failure),
            selectinload(AgentRun.generated_patches).selectinload(GeneratedPatch.human_review),
        )
        if filters.model_provider is not None:
            statement = statement.where(AgentRun.model_provider == filters.model_provider)
        if filters.model_name is not None:
            statement = statement.where(AgentRun.model_name == filters.model_name)
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
        return list(
            self.db.scalars(
                statement.order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            ).all()
        )

    def _pack_summary(self, pack_id: UUID | None) -> PublicDemoPack | None:
        if pack_id is None:
            return None
        pack = self.db.get(BenchmarkPack, pack_id)
        if pack is None:
            return None
        return PublicDemoPack(
            id=pack.id,
            name=_safe_text(pack.name),
            slug=_safe_text(pack.slug),
            version=_safe_text(pack.version),
        )

    def _run_summary(self, run: AgentRun) -> PublicDemoRun:
        task = run.benchmark_task
        repository = task.repository
        metric = run.evaluation_metric
        failure_category = run.failure.category if run.failure else None
        return PublicDemoRun(
            run_id=run.id,
            benchmark_task_id=task.id,
            repository_owner=_safe_text(repository.owner),
            repository_name=_safe_text(repository.name),
            repository_url=(
                None
                if self.settings.public_demo_redact_repository_urls
                else _public_repository_url(repository.url)
            ),
            issue_number=task.issue_number,
            issue_title=_safe_text(task.issue_title),
            model_provider=_safe_text(run.model_provider),
            model_name=_safe_text(run.model_name),
            run_status=_safe_text(run.status),
            issue_resolved=bool(metric and metric.issue_resolved),
            visible_tests_passed=bool(metric and metric.post_patch_tests_passed),
            hidden_tests_passed=metric.hidden_tests_passed if metric else None,
            hidden_tests_run_count=metric.hidden_tests_run_count if metric else 0,
            file_localization_score=metric.file_localization_score if metric else None,
            issue_specific_score=metric.issue_specific_score if metric else 0.0,
            tokens_used=(metric.tokens_used or 0) if metric else 0,
            estimated_cost=(metric.estimated_cost or 0.0) if metric else 0.0,
            execution_time_seconds=(metric.execution_time_seconds or 0.0) if metric else 0.0,
            failure_category=_safe_text(failure_category) if failure_category else None,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )


def _select_examples(runs: list[AgentRun], limit: int) -> tuple[list[AgentRun], list[AgentRun]]:
    successful = [
        run
        for run in runs
        if run.status == "completed"
        and run.evaluation_metric is not None
        and run.evaluation_metric.issue_resolved
    ]
    failed = [run for run in runs if run.status == "failed"]
    successful_selected = successful[: (limit + 1) // 2]
    failed_selected = failed[: limit // 2]

    selected_ids = {run.id for run in successful_selected + failed_selected}
    remaining_slots = limit - len(selected_ids)
    if remaining_slots:
        remaining = [run for run in successful + failed if run.id not in selected_ids]
        remaining.sort(key=lambda run: (run.started_at, run.id), reverse=True)
        for run in remaining[:remaining_slots]:
            if run.status == "failed":
                failed_selected.append(run)
            else:
                successful_selected.append(run)
    return successful_selected, failed_selected


def _usage_summary(runs: list[AgentRun], average_cost: float) -> PublicDemoUsageSummary:
    metrics = [run.evaluation_metric for run in runs if run.evaluation_metric is not None]
    total_time = sum(metric.execution_time_seconds or 0.0 for metric in metrics)
    return PublicDemoUsageSummary(
        total_tokens=sum(metric.tokens_used or 0 for metric in metrics),
        total_estimated_cost=round(sum(metric.estimated_cost or 0.0 for metric in metrics), 8),
        average_cost_per_run=average_cost,
        total_execution_time_seconds=round(total_time, 4),
        average_execution_time_seconds=(total_time / len(metrics) if metrics else 0.0),
    )


def _snapshot_notes(*, has_runs: bool, redact_urls: bool, limit: int) -> list[str]:
    notes = [
        "This public snapshot omits patches, test content, command logs, and trace payloads.",
        "Hidden evaluation is represented by aggregate pass status and counts only.",
        f"Successful and failed examples share a total selection limit of {limit} runs.",
        "Costs are stored estimates and may be zero when provider pricing is unavailable.",
    ]
    if redact_urls:
        notes.append("Repository URLs are redacted by configuration.")
    if not has_runs:
        notes.append("No benchmark runs matched the selected filters.")
    return notes


def _public_repository_url(value: str) -> str | None:
    redacted = _safe_text(value)
    try:
        parsed = urlsplit(redacted)
        if not parsed.scheme or not parsed.hostname:
            return None
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return None


def _append_run_table(lines: list[str], heading: str, runs: list[PublicDemoRun]) -> None:
    lines.extend(["", f"## {heading}", ""])
    if not runs:
        lines.append("No runs selected.")
        return
    lines.extend(
        [
            (
                "| Repository | Task | Model | Status | Resolved | Visible | Hidden | "
                "Localization | Cost | Time | Failure category |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for run in runs:
        issue = f"#{run.issue_number} {run.issue_title}" if run.issue_number else run.issue_title
        hidden = (
            "Not run"
            if run.hidden_tests_run_count == 0
            else _yes_no(run.hidden_tests_passed is True)
        )
        localization = (
            "N/A" if run.file_localization_score is None else f"{run.file_localization_score:.4f}"
        )
        lines.append(
            f"| {_md_cell(f'{run.repository_owner}/{run.repository_name}')} | "
            f"{_md_cell(issue)} | {_md_cell(f'{run.model_provider}/{run.model_name}')} | "
            f"{_md_cell(run.run_status)} | {_yes_no(run.issue_resolved)} | "
            f"{_yes_no(run.visible_tests_passed)} | {hidden} | {localization} | "
            f"${run.estimated_cost:.8f} | {run.execution_time_seconds:.4f}s | "
            f"{_md_cell(run.failure_category or '')} |"
        )


def _pack_label(snapshot: PublicDemoSnapshot) -> str:
    if snapshot.benchmark_pack is None:
        return "All packs and standalone runs"
    pack = snapshot.benchmark_pack
    return f"{_safe_text(pack.name)} (`{_md_cell(pack.slug)}`, version `{_md_cell(pack.version)}`)"


def _percent(value: float | None) -> str:
    return "Not available" if value is None else f"{value * 100:.2f}%"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _safe_text(value: str) -> str:
    return redact_prompt_text(value)


def _md_cell(value: str) -> str:
    return _safe_text(value).replace("|", "\\|").replace("\n", " ").replace("`", "'")
