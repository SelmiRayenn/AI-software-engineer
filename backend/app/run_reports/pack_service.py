from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.prompts import redact_prompt_text
from app.models import BenchmarkPack, BenchmarkPackRun, BenchmarkPackRunTask
from app.schemas.agent_run import AgentRunConfig
from app.schemas.benchmark_pack_run import BenchmarkPackRunAggregates, PackTaskMetrics
from app.schemas.pack_run_report import (
    BenchmarkPackRunReport,
    PackReportFailureBreakdown,
    PackReportMetadata,
    PackReportModel,
    PackReportRecurringFailure,
    PackReportTask,
    PackRunReportMetadata,
)
from app.schemas.run_report import ReportText

MAX_PACK_DESCRIPTION_BYTES = 4_096
MAX_PACK_TASK_MESSAGE_BYTES = 2_048
MAX_RECURRING_FAILURES = 10
MAX_FAILURE_TASK_REFERENCES = 25


class BenchmarkPackRunReportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, pack_run_id: UUID) -> BenchmarkPackRunReport | None:
        pack_run = self.db.scalar(
            select(BenchmarkPackRun)
            .where(BenchmarkPackRun.id == pack_run_id)
            .options(selectinload(BenchmarkPackRun.tasks))
        )
        if pack_run is None:
            return None

        pack = self.db.get(BenchmarkPack, pack_run.benchmark_pack_id)
        tasks = [
            _task_report(task) for task in sorted(pack_run.tasks, key=lambda item: item.order_index)
        ]
        aggregates = BenchmarkPackRunAggregates.model_validate(pack_run.aggregates or {})
        return BenchmarkPackRunReport(
            generated_at=datetime.now(UTC),
            pack=PackReportMetadata(
                id=pack_run.benchmark_pack_id,
                name=_safe_text(pack_run.pack_name),
                slug=_safe_text(pack_run.pack_slug),
                description=(
                    _bounded_text(pack.description, MAX_PACK_DESCRIPTION_BYTES)
                    if pack and pack.description
                    else None
                ),
                version=_safe_text(pack_run.pack_version),
                source=_safe_text(pack.source) if pack and pack.source else None,
            ),
            pack_run=PackRunReportMetadata(
                id=pack_run.id,
                status=_safe_text(pack_run.status),
                created_at=pack_run.created_at,
                started_at=pack_run.started_at,
                completed_at=pack_run.completed_at,
                include_hidden_tests=pack_run.include_hidden_tests,
                stop_on_task_failure=pack_run.stop_on_task_failure,
            ),
            model=PackReportModel(
                provider=_safe_text(pack_run.model_provider),
                name=_safe_text(pack_run.model_name),
            ),
            run_configuration=_run_config(pack_run),
            aggregates=aggregates,
            tasks=tasks,
            failure_breakdown=_failure_breakdown(tasks),
            recurring_failures=_recurring_failures(tasks),
            known_limitations=_known_limitations(pack_run, tasks, aggregates),
        )

    def render_markdown(self, report: BenchmarkPackRunReport) -> str:
        aggregates = report.aggregates
        lines = [
            "# Benchmark Pack Run Report",
            "",
            f"Generated: {report.generated_at.isoformat()}",
            "",
            "## Pack Metadata",
            "",
            f"- Pack: {_safe_text(report.pack.name)} (`{_md_inline(report.pack.slug)}`)",
            f"- Version: `{_md_inline(report.pack.version)}`",
            f"- Pack ID: `{report.pack.id}`",
            f"- Source: {report.pack.source or 'Not recorded'}",
        ]
        if report.pack.description:
            lines.extend(["", _markdown_quote(report.pack.description.text)])
            _append_truncation_note(lines, report.pack.description, "Pack description")

        lines.extend(
            [
                "",
                "## Pack Run Metadata",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| Pack run ID | `{report.pack_run.id}` |",
                f"| Status | {_md_cell(report.pack_run.status)} |",
                f"| Started | {report.pack_run.started_at.isoformat()} |",
                f"| Completed | {_optional_datetime(report.pack_run.completed_at)} |",
                (
                    "| Hidden evaluation enabled | "
                    f"{_yes_no(report.pack_run.include_hidden_tests)} |"
                ),
                (f"| Stop on task failure | {_yes_no(report.pack_run.stop_on_task_failure)} |"),
                "",
                "## Model and Run Configuration",
                "",
                f"- Provider: `{_md_inline(report.model.provider)}`",
                f"- Model: `{_md_inline(report.model.name)}`",
                "",
                *_fenced(
                    json.dumps(report.run_configuration.model_dump(mode="json"), indent=2), "json"
                ),
                "",
                "## Aggregate Summary",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Total tasks | {aggregates.total_tasks} |",
                f"| Completed tasks | {aggregates.completed_tasks} |",
                f"| Failed tasks | {aggregates.failed_tasks} |",
                f"| Skipped tasks | {aggregates.skipped_tasks} |",
                f"| Issue resolved rate | {_percent(aggregates.issue_resolved_rate)} |",
                f"| Visible test pass rate | {_percent(aggregates.visible_test_pass_rate)} |",
                f"| Hidden test pass rate | {_percent(aggregates.hidden_test_pass_rate)} |",
                (
                    "| Average localization score | "
                    f"{aggregates.average_file_localization_score:.4f} |"
                ),
                (
                    "| Average issue-specific score | "
                    f"{aggregates.average_issue_specific_score:.4f} |"
                ),
                f"| Total tokens | {aggregates.total_tokens} |",
                f"| Total estimated cost | ${aggregates.total_cost:.8f} |",
                f"| Total execution time | {aggregates.total_execution_time:.4f}s |",
                f"| Average execution time | {aggregates.average_execution_time:.4f}s |",
                "",
                "## Per-Task Results",
                "",
            ]
        )
        if not report.tasks:
            lines.append("No task results were recorded for this pack run.")
        else:
            lines.extend(
                [
                    (
                        "| # | Task | Repository | Status | Resolved | Visible | Hidden | "
                        "Localization | Issue score | Tokens | Cost | Time | Failure |"
                    ),
                    (
                        "| ---: | --- | --- | --- | --- | --- | --- | ---: | ---: | "
                        "---: | ---: | ---: | --- |"
                    ),
                ]
            )
            lines.extend(_task_table_row(task) for task in report.tasks)

        lines.extend(["", "## Failure Category Breakdown", ""])
        if report.failure_breakdown:
            lines.extend(["| Category | Count |", "| --- | ---: |"])
            lines.extend(
                f"| `{_md_inline(item.category)}` | {item.count} |"
                for item in report.failure_breakdown
            )
        else:
            lines.append("No task failures were recorded.")

        lines.extend(["", "## Top Recurring Failed Tasks / Errors", ""])
        if report.recurring_failures:
            lines.extend(["| Category | Count | Summary | Tasks |", "| --- | ---: | --- | --- |"])
            for failure in report.recurring_failures:
                task_ids = ", ".join(f"`{task_id}`" for task_id in failure.benchmark_task_ids)
                lines.append(
                    f"| `{_md_inline(failure.category)}` | {failure.count} | "
                    f"{_md_cell(failure.summary.text)} | {task_ids} |"
                )
                _append_truncation_note(lines, failure.summary, "Failure message")
        else:
            lines.append("No recurring failures were recorded.")

        failed_tasks = [task for task in report.tasks if task.failure_summary]
        if failed_tasks:
            lines.extend(["", "### Failed Task Details", ""])
            for task in failed_tasks:
                lines.extend(
                    [
                        f"#### {_task_label(task)}",
                        "",
                        f"- Category: `{task.failure_category or 'unknown'}`",
                        _markdown_quote(task.failure_summary.text),
                    ]
                )
                _append_truncation_note(lines, task.failure_summary, "Failure message")

        lines.extend(["", "## Known Limitations", ""])
        lines.extend(f"- {_safe_text(item)}" for item in report.known_limitations)
        return "\n".join(lines).rstrip() + "\n"


def _run_config(pack_run: BenchmarkPackRun) -> AgentRunConfig:
    try:
        return AgentRunConfig.model_validate(pack_run.run_config)
    except ValidationError:
        return AgentRunConfig(
            model_provider=pack_run.model_provider,
            model_name=pack_run.model_name,
        )


def _task_report(task: BenchmarkPackRunTask) -> PackReportTask:
    snapshot = task.task_snapshot if isinstance(task.task_snapshot, dict) else {}
    repository = snapshot.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    raw_tags = snapshot.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else []
    return PackReportTask(
        id=task.id,
        order_index=task.order_index,
        benchmark_task_id=task.benchmark_task_id,
        agent_run_id=task.agent_run_id,
        repository_owner=_safe_text(str(repository.get("owner") or "unknown")),
        repository_name=_safe_text(str(repository.get("name") or "unknown")),
        repository_url=_safe_text(str(repository.get("url") or "")),
        issue_number=_optional_positive_int(snapshot.get("issue_number")),
        issue_title=_safe_text(str(snapshot.get("issue_title") or "Untitled task")),
        difficulty=_safe_text(str(snapshot.get("difficulty") or "unknown")),
        tags=[_safe_text(str(tag))[:100] for tag in tags[:25]],
        status=_safe_text(task.status),
        metrics=(
            PackTaskMetrics.model_validate(task.metric_summary) if task.metric_summary else None
        ),
        failure_category=(_safe_text(task.failure_category) if task.failure_category else None),
        failure_summary=(
            _bounded_text(task.failure_summary, MAX_PACK_TASK_MESSAGE_BYTES)
            if task.failure_summary
            else None
        ),
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


def _failure_breakdown(tasks: list[PackReportTask]) -> list[PackReportFailureBreakdown]:
    counts = Counter(
        task.failure_category or "unknown"
        for task in tasks
        if task.failure_category or task.status in {"failed", "skipped", "cancelled"}
    )
    return [
        PackReportFailureBreakdown(category=category, count=count)
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _recurring_failures(tasks: list[PackReportTask]) -> list[PackReportRecurringFailure]:
    grouped: dict[tuple[str, str], list[PackReportTask]] = defaultdict(list)
    for task in tasks:
        if task.failure_summary is None:
            continue
        category = task.failure_category or "unknown"
        summary_key = " ".join(task.failure_summary.text.split()).lower()[:500]
        grouped[(category, summary_key)].append(task)

    ordered = sorted(
        grouped.items(),
        key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
    )[:MAX_RECURRING_FAILURES]
    return [
        PackReportRecurringFailure(
            category=category,
            summary=matches[0].failure_summary,
            count=len(matches),
            benchmark_task_ids=[
                task.benchmark_task_id for task in matches[:MAX_FAILURE_TASK_REFERENCES]
            ],
            issue_titles=[task.issue_title for task in matches[:MAX_FAILURE_TASK_REFERENCES]],
        )
        for (category, _), matches in ordered
    ]


def _known_limitations(
    pack_run: BenchmarkPackRun,
    tasks: list[PackReportTask],
    aggregates: BenchmarkPackRunAggregates,
) -> list[str]:
    limitations = [
        "Pack tasks execute sequentially, so total duration is not a parallel benchmark measure.",
        "Hidden evaluation content is omitted; only aggregate status and counts are exported.",
        "Costs are stored provider estimates and may be zero when pricing is unavailable.",
    ]
    if aggregates.hidden_test_pass_rate is None:
        limitations.append(
            "No hidden evaluation results were recorded; issue resolution relies on visible tests."
        )
    if not tasks:
        limitations.append("This pack run contains no task results.")
    if any(task.metrics is None for task in tasks):
        limitations.append("Some tasks have no metric snapshot and are absent from metric detail.")
    if pack_run.status != "completed":
        limitations.append(
            f"The pack run ended with status '{_safe_text(pack_run.status)}'; results may be partial."
        )
    return limitations


def _task_table_row(task: PackReportTask) -> str:
    metrics = task.metrics
    issue = _task_label(task)
    repository = f"{task.repository_owner}/{task.repository_name}"
    return (
        f"| {task.order_index} | {_md_cell(issue)} | {_md_cell(repository)} | "
        f"{_md_cell(task.status)} | {_metric_bool(metrics, 'issue_resolved')} | "
        f"{_metric_bool(metrics, 'post_patch_tests_passed')} | {_hidden_status(metrics)} | "
        f"{_metric_float(metrics, 'file_localization_score')} | "
        f"{_metric_float(metrics, 'issue_specific_score')} | "
        f"{_metric_number(metrics, 'tokens_used', integer=True)} | "
        f"${_metric_number(metrics, 'estimated_cost', precision=8)} | "
        f"{_metric_number(metrics, 'execution_time_seconds', precision=4)}s | "
        f"{_md_cell(task.failure_category or '')} |"
    )


def _metric_bool(metrics: PackTaskMetrics | None, field: str) -> str:
    if metrics is None:
        return "N/A"
    return _yes_no(bool(getattr(metrics, field)))


def _metric_float(metrics: PackTaskMetrics | None, field: str) -> str:
    if metrics is None:
        return "N/A"
    value = getattr(metrics, field)
    return "N/A" if value is None else f"{value:.4f}"


def _metric_number(
    metrics: PackTaskMetrics | None,
    field: str,
    *,
    integer: bool = False,
    precision: int = 4,
) -> str:
    if metrics is None:
        return "N/A"
    value = getattr(metrics, field)
    if value is None:
        return "0" if integer else f"{0.0:.{precision}f}"
    return str(int(value)) if integer else f"{float(value):.{precision}f}"


def _hidden_status(metrics: PackTaskMetrics | None) -> str:
    if metrics is None or metrics.hidden_tests_run_count == 0:
        return "Not run"
    return _yes_no(metrics.hidden_tests_passed is True)


def _task_label(task: PackReportTask) -> str:
    prefix = f"#{task.issue_number} " if task.issue_number else ""
    return f"{prefix}{task.issue_title}"


def _bounded_text(value: str, limit_bytes: int) -> ReportText:
    redacted = _safe_text(value)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return ReportText(text=redacted, truncated=False, original_size_bytes=len(encoded))
    return ReportText(
        text=encoded[:limit_bytes].decode("utf-8", errors="ignore"),
        truncated=True,
        original_size_bytes=len(encoded),
    )


def _safe_text(value: str) -> str:
    return redact_prompt_text(value)


def _optional_positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _percent(value: float | None) -> str:
    return "Not available" if value is None else f"{value * 100:.2f}%"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _optional_datetime(value: datetime | None) -> str:
    return value.isoformat() if value else "Unavailable"


def _md_inline(value: str) -> str:
    return _safe_text(value).replace("`", "'").replace("\n", " ")


def _md_cell(value: Any) -> str:
    return _safe_text(str(value)).replace("|", "\\|").replace("\n", " ")


def _markdown_quote(value: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def _fenced(value: str, language: str = "") -> list[str]:
    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", value)),
        default=0,
    )
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", value, fence]


def _append_truncation_note(lines: list[str], value: ReportText, label: str) -> None:
    if value.truncated:
        lines.append(f"_{label} truncated from {value.original_size_bytes} bytes for safe export._")
