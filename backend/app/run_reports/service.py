from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import redact_prompt_text
from app.core.test_phases import TEST_PHASE_HIDDEN_EVAL
from app.evaluation.service import model_usage_totals
from app.models import AgentEvent, AgentRun, TestResult
from app.run_traces import AgentRunTraceService
from app.schemas.agent_run import AgentRunConfig
from app.schemas.run_report import (
    AgentRunReport,
    ReportBenchmarkTask,
    ReportEvaluationMetrics,
    ReportFailure,
    ReportFinalPatch,
    ReportHiddenEvaluation,
    ReportHumanReview,
    ReportIssueSummary,
    ReportModel,
    ReportPatchQuality,
    ReportRepository,
    ReportRunMetadata,
    ReportTestCommand,
    ReportTestPhase,
    ReportText,
    ReportTraceEvent,
    ReportTraceSummary,
    ReportUsageSummary,
)

MAX_ISSUE_BYTES = 8_192
MAX_DIFF_BYTES = 50_000
MAX_LOG_BYTES = 8_192
MAX_REVIEW_BYTES = 4_096
MAX_FAILURE_BYTES = 4_096
MAX_TRACE_EVENTS = 200
MAX_TEST_RESULTS_PER_PHASE = 100
MAX_REPORT_PATHS = 500

_INSPECTION_TOOLS = {
    "get_diff",
    "list_files",
    "read_file",
    "retrieve_relevant_files",
    "search_code",
}


class AgentRunReportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, run_id: UUID) -> AgentRunReport | None:
        run = self.db.get(AgentRun, run_id)
        if run is None:
            return None

        task = run.benchmark_task
        repository = task.repository
        trace = AgentRunTraceService(self.db).get(run.id)
        if trace is None:
            return None

        patch = run.generated_patch
        metric = run.evaluation_metric
        events = sorted(run.events, key=lambda event: (event.created_at, event.id))
        test_results = sorted(
            run.test_results,
            key=lambda result: (result.created_at, result.id),
        )
        tokens, cost = model_usage_totals(run.model_provider, events)
        if metric is not None:
            tokens = metric.tokens_used if metric.tokens_used is not None else tokens
            cost = metric.estimated_cost if metric.estimated_cost is not None else cost

        changed_files = _safe_paths(patch.changed_files if patch else [])
        return AgentRunReport(
            generated_at=datetime.now(UTC),
            run=ReportRunMetadata(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                completed_at=run.completed_at,
                repair_attempts_used=run.repair_attempts_used,
                final_patch_id=run.final_patch_id,
                final_patch_passed_tests=run.final_patch_passed_tests,
            ),
            benchmark_task=ReportBenchmarkTask(
                id=task.id,
                status=task.status,
                issue_number=task.issue_number,
                issue_title=_safe_text(task.issue_title),
                base_commit=task.base_commit,
                difficulty=task.difficulty,
                tags=[_safe_text(tag)[:100] for tag in task.tags],
            ),
            repository=ReportRepository(
                id=repository.id,
                owner=_safe_text(repository.owner),
                name=_safe_text(repository.name),
                url=_safe_text(repository.url),
                default_branch=_safe_text(repository.default_branch),
                language=repository.language,
            ),
            issue=ReportIssueSummary(
                title=_safe_text(task.issue_title),
                body=_bounded_text(task.issue_body or "", MAX_ISSUE_BYTES),
                comment_count=len(task.issue_comments or []),
            ),
            model=ReportModel(
                provider=run.model_provider,
                name=run.model_name,
            ),
            run_configuration=self._run_configuration(run.id),
            trace=_trace_summary(trace),
            files_inspected=_inspected_files(trace),
            files_modified=changed_files,
            final_patch=_final_patch(run.id, patch, changed_files),
            patch_quality=_patch_quality(patch),
            test_results=_test_phase_summaries(test_results),
            hidden_evaluation=_hidden_evaluation(test_results, metric),
            evaluation_metrics=(
                ReportEvaluationMetrics.model_validate(metric, from_attributes=True)
                if metric
                else None
            ),
            failure=(
                ReportFailure(
                    category=run.failure.category,
                    summary=_bounded_text(
                        run.failure.human_readable_summary,
                        MAX_FAILURE_BYTES,
                    ),
                    created_at=run.failure.created_at,
                )
                if run.failure
                else None
            ),
            human_review=_human_review(patch),
            usage=ReportUsageSummary(
                tokens_used=tokens,
                estimated_cost=cost,
                execution_time_seconds=(
                    metric.execution_time_seconds
                    if metric and metric.execution_time_seconds is not None
                    else _execution_time(run)
                ),
            ),
        )

    def render_markdown(self, report: AgentRunReport) -> str:
        lines = [
            "# Agent Run Evaluation Report",
            "",
            f"Generated: {report.generated_at.isoformat()}",
            "",
            "## Run Metadata",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Run ID | `{report.run.id}` |",
            f"| Status | {_md_cell(report.run.status)} |",
            f"| Started | {report.run.started_at.isoformat()} |",
            f"| Completed | {_optional_datetime(report.run.completed_at)} |",
            f"| Repair attempts | {report.run.repair_attempts_used} |",
            "",
            "## Benchmark Task",
            "",
            f"- Task ID: `{report.benchmark_task.id}`",
            f"- Status: {_safe_text(report.benchmark_task.status)}",
            f"- Issue: {_issue_label(report)}",
            f"- Base commit: `{report.benchmark_task.base_commit}`",
            f"- Difficulty: {_safe_text(report.benchmark_task.difficulty)}",
            f"- Tags: {', '.join(report.benchmark_task.tags) or 'None'}",
            "",
            "## Repository",
            "",
            f"- Repository: `{report.repository.owner}/{report.repository.name}`",
            f"- URL: {report.repository.url}",
            f"- Default branch: `{report.repository.default_branch}`",
            f"- Language: {report.repository.language or 'Unknown'}",
            "",
            "## Issue / Problem Statement",
            "",
            f"### {_safe_text(report.issue.title)}",
            "",
            _markdown_quote(report.issue.body.text or "No problem statement provided."),
        ]
        _append_truncation_note(lines, report.issue.body, "Problem statement")

        lines.extend(
            [
                "",
                "## Model and Run Configuration",
                "",
                f"- Provider: `{report.model.provider}`",
                f"- Model: `{report.model.name}`",
                "",
            ]
        )
        if report.run_configuration is None:
            lines.append("Run configuration was not recorded.")
        else:
            lines.extend(_fenced(json.dumps(report.run_configuration, indent=2), "json"))

        lines.extend(
            [
                "",
                "## Trace Summary",
                "",
                f"- Total events: {report.trace.total_events}",
                f"- Events included: {report.trace.events_included}",
                f"- Truncated: {'yes' if report.trace.truncated else 'no'}",
                "",
            ]
        )
        for event in report.trace.events:
            tool = f" (`{event.tool_name}`)" if event.tool_name else ""
            lines.append(
                f"- {event.created_at.isoformat()} [{event.severity}] "
                f"{_safe_text(event.summary)}{tool}"
            )

        lines.extend(["", "## Files Inspected / Read", ""])
        lines.extend(_markdown_path_list(report.files_inspected))
        lines.extend(["", "## Files Modified", ""])
        lines.extend(_markdown_path_list(report.files_modified))

        lines.extend(["", "## Patch Quality", ""])
        if report.patch_quality is None:
            lines.append("No patch quality report is available.")
        else:
            quality = report.patch_quality
            lines.extend(
                [
                    f"- Changed files: {quality.changed_file_count}",
                    f"- Added lines: {quality.added_lines}",
                    f"- Removed lines: {quality.removed_lines}",
                    f"- Total changed lines: {quality.total_changed_lines}",
                    f"- Whitespace only: {'yes' if quality.whitespace_only else 'no'}",
                    f"- Warnings: {'; '.join(quality.warnings) or 'None'}",
                    (
                        "- Hard-limit violations: "
                        f"{'; '.join(quality.hard_limit_violations) or 'None'}"
                    ),
                ]
            )

        lines.extend(["", "## Test Results", ""])
        for phase in report.test_results:
            lines.extend(
                [
                    f"### {phase.phase.replace('_', ' ').title()}",
                    "",
                    f"- Commands: {phase.command_count}",
                    f"- Passed: {phase.passed_count}",
                    f"- Failed: {phase.failed_count}",
                    f"- Duration: {phase.duration_seconds:.4f}s",
                ]
            )
            if not phase.details_included:
                lines.append("- Command and log details withheld for hidden evaluation.")
                continue
            for result in phase.results:
                lines.extend(
                    [
                        "",
                        f"Command: `{_md_inline(result.command)}`",
                        (
                            f"Result: {'passed' if result.passed else 'failed'} "
                            f"(exit {result.exit_code})"
                        ),
                    ]
                )
                _append_log(lines, "stdout", result.stdout)
                _append_log(lines, "stderr", result.stderr)

        lines.extend(["", "## Hidden Evaluation Summary", ""])
        hidden = report.hidden_evaluation
        lines.extend(
            [
                f"- Available: {'yes' if hidden.available else 'no'}",
                f"- Passed: {_optional_bool(hidden.passed)}",
                f"- Commands run: {hidden.run_count}",
                f"- Commands failed: {hidden.failed_count}",
            ]
        )

        lines.extend(["", "## Evaluation Metrics", ""])
        if report.evaluation_metrics is None:
            lines.append("No evaluation metrics are available.")
        else:
            for key, value in report.evaluation_metrics.model_dump().items():
                lines.append(f"- {key.replace('_', ' ').title()}: {_md_cell(value)}")

        if report.failure:
            lines.extend(
                [
                    "",
                    "## Failure Classification",
                    "",
                    f"- Category: `{report.failure.category}`",
                    f"- Summary: {_safe_text(report.failure.summary.text)}",
                ]
            )
            _append_truncation_note(lines, report.failure.summary, "Failure summary")

        lines.extend(
            [
                "",
                "## Human Review",
                "",
                f"- Status: `{report.human_review.status}`",
                f"- Reviewer: {report.human_review.reviewer_name or 'Not reviewed'}",
                f"- Reviewed at: {_optional_datetime(report.human_review.reviewed_at)}",
            ]
        )
        if report.human_review.review_notes:
            lines.append(f"- Notes: {_safe_text(report.human_review.review_notes.text)}")
            _append_truncation_note(lines, report.human_review.review_notes, "Review notes")

        lines.extend(
            [
                "",
                "## Cost, Tokens, and Time",
                "",
                f"- Tokens used: {report.usage.tokens_used}",
                f"- Estimated cost: ${report.usage.estimated_cost:.8f}",
                "- Execution time: "
                + (
                    f"{report.usage.execution_time_seconds:.4f}s"
                    if report.usage.execution_time_seconds is not None
                    else "Unavailable"
                ),
                "",
                "## Final Patch",
                "",
            ]
        )
        if report.final_patch is None:
            lines.append("No generated patch is available.")
        else:
            lines.extend(
                [
                    f"- Patch ID: `{report.final_patch.id}`",
                    f"- Version: {report.final_patch.version}",
                    f"- Review status: `{report.final_patch.review_status}`",
                    f"- Full patch reference: `{report.final_patch.reference}`",
                    "",
                ]
            )
            lines.extend(_fenced(report.final_patch.diff.text, "diff"))
            _append_truncation_note(lines, report.final_patch.diff, "Patch diff")

        return "\n".join(lines).rstrip() + "\n"

    def _run_configuration(self, run_id: UUID) -> dict[str, Any] | None:
        event = self.db.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == run_id,
                AgentEvent.event_type == "agent_run_configured",
            )
            .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
            .limit(1)
        )
        if event is None:
            return None
        try:
            config = AgentRunConfig.model_validate((event.payload_json or {}).get("config"))
        except ValidationError:
            return None
        return config.model_dump(mode="json")


def _trace_summary(trace: Any) -> ReportTraceSummary:
    events = trace.events[:MAX_TRACE_EVENTS]
    return ReportTraceSummary(
        total_events=len(trace.events),
        events_included=len(events),
        truncated=len(trace.events) > len(events),
        event_counts=dict(sorted(Counter(event.event_type for event in trace.events).items())),
        events=[
            ReportTraceEvent(
                created_at=event.created_at,
                event_type=event.event_type,
                summary=_safe_text(event.summary),
                tool_name=event.tool_name,
                file_paths=_safe_paths(event.file_paths),
                severity=event.severity,
            )
            for event in events
        ],
    )


def _inspected_files(trace: Any) -> list[str]:
    paths: list[str] = []
    for event in trace.events:
        if event.event_type != "agent_tool_call" or event.tool_name not in _INSPECTION_TOOLS:
            continue
        paths.extend(event.file_paths)
    return _safe_paths(paths)


def _final_patch(run_id: UUID, patch: Any, changed_files: list[str]) -> ReportFinalPatch | None:
    if patch is None:
        return None
    return ReportFinalPatch(
        id=patch.id,
        version=patch.version,
        is_selected=patch.is_selected,
        changed_files=changed_files,
        review_status=patch.review_status,
        diff=_bounded_text(patch.patch_text, MAX_DIFF_BYTES),
        reference=f"/agent-runs/{run_id}/patch",
    )


def _patch_quality(patch: Any) -> ReportPatchQuality | None:
    if patch is None or patch.quality is None:
        return None
    quality = patch.quality
    return ReportPatchQuality(
        changed_file_count=quality.changed_file_count,
        added_lines=quality.added_lines,
        removed_lines=quality.removed_lines,
        total_changed_lines=quality.total_changed_lines,
        changed_source_files=_safe_paths(quality.changed_source_files),
        changed_test_files=_safe_paths(quality.changed_test_files),
        changed_docs_config_files=_safe_paths(quality.changed_docs_config_files),
        suspicious_generated_files=_safe_paths(quality.suspicious_generated_files),
        whitespace_only=quality.whitespace_only,
        dependency_files=_safe_paths(quality.dependency_files),
        lockfiles=_safe_paths(quality.lockfiles),
        warnings=[_safe_text(value)[:1_000] for value in quality.warnings],
        hard_limit_violations=[
            _safe_text(value)[:1_000] for value in quality.hard_limit_violations
        ],
    )


def _test_phase_summaries(results: list[TestResult]) -> list[ReportTestPhase]:
    grouped: dict[str, list[TestResult]] = defaultdict(list)
    for result in results:
        grouped[result.phase].append(result)
    preferred_order = {"setup": 0, "baseline": 1, "post_patch": 2, TEST_PHASE_HIDDEN_EVAL: 3}

    summaries = []
    for phase, phase_results in sorted(
        grouped.items(), key=lambda item: (preferred_order.get(item[0], 99), item[0])
    ):
        hidden = phase == TEST_PHASE_HIDDEN_EVAL
        included_results = phase_results[:MAX_TEST_RESULTS_PER_PHASE] if not hidden else []
        summaries.append(
            ReportTestPhase(
                phase=phase,
                command_count=len(phase_results),
                passed_count=sum(result.passed for result in phase_results),
                failed_count=sum(not result.passed for result in phase_results),
                duration_seconds=round(
                    sum(result.duration_seconds or 0.0 for result in phase_results),
                    4,
                ),
                details_included=not hidden,
                results_truncated=not hidden and len(phase_results) > len(included_results),
                results=[
                    ReportTestCommand(
                        command=_safe_text(result.command)[:2_000],
                        passed=result.passed,
                        exit_code=result.exit_code,
                        duration_seconds=result.duration_seconds,
                        stdout=_bounded_text(result.stdout or "", MAX_LOG_BYTES),
                        stderr=_bounded_text(result.stderr or "", MAX_LOG_BYTES),
                    )
                    for result in included_results
                ],
            )
        )
    return summaries


def _hidden_evaluation(results: list[TestResult], metric: Any) -> ReportHiddenEvaluation:
    hidden_results = [result for result in results if result.phase == TEST_PHASE_HIDDEN_EVAL]
    if metric is not None and metric.hidden_tests_run_count:
        return ReportHiddenEvaluation(
            available=True,
            passed=metric.hidden_tests_passed,
            run_count=metric.hidden_tests_run_count,
            failed_count=metric.hidden_tests_failed_count,
        )
    return ReportHiddenEvaluation(
        available=bool(hidden_results),
        passed=all(result.passed for result in hidden_results) if hidden_results else None,
        run_count=len(hidden_results),
        failed_count=sum(not result.passed for result in hidden_results),
    )


def _human_review(patch: Any) -> ReportHumanReview:
    if patch is None:
        return ReportHumanReview(status="not_available")
    review = patch.human_review
    if review is None:
        return ReportHumanReview(status="pending")
    return ReportHumanReview(
        status=review.decision,
        reviewer_name=_safe_text(review.reviewer_name)[:255] if review.reviewer_name else None,
        review_notes=(
            _bounded_text(review.review_notes, MAX_REVIEW_BYTES) if review.review_notes else None
        ),
        reviewed_at=review.reviewed_at,
    )


def _execution_time(run: AgentRun) -> float | None:
    if run.started_at is None or run.completed_at is None:
        return None
    return max(0.0, round((run.completed_at - run.started_at).total_seconds(), 4))


def _bounded_text(value: str, limit_bytes: int) -> ReportText:
    redacted = _safe_text(value)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return ReportText(
            text=redacted,
            truncated=False,
            original_size_bytes=len(encoded),
        )
    preview = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return ReportText(
        text=preview,
        truncated=True,
        original_size_bytes=len(encoded),
    )


def _safe_paths(paths: list[str]) -> list[str]:
    safe: list[str] = []
    for raw_path in paths:
        normalized = _safe_text(str(raw_path)).replace("\\", "/").strip()
        lowered = normalized.lower()
        if not normalized or any(
            marker in lowered
            for marker in (".benchmark", "gold_patch", "gold_solution", "hidden_eval")
        ):
            continue
        if normalized not in safe:
            safe.append(normalized[:1_000])
        if len(safe) >= MAX_REPORT_PATHS:
            break
    return safe


def _safe_text(value: str) -> str:
    return redact_prompt_text(value)


def _fenced(value: str, language: str = "") -> list[str]:
    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", value)),
        default=0,
    )
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", value, fence]


def _markdown_quote(value: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def _markdown_path_list(paths: list[str]) -> list[str]:
    return [f"- `{_md_inline(path)}`" for path in paths] or ["None recorded."]


def _append_log(lines: list[str], label: str, value: ReportText) -> None:
    if not value.text:
        return
    lines.extend(["", f"{label}:", *_fenced(value.text, "text")])
    _append_truncation_note(lines, value, label)


def _append_truncation_note(lines: list[str], value: ReportText, label: str) -> None:
    if value.truncated:
        lines.append(f"_{label} truncated from {value.original_size_bytes} bytes for safe export._")


def _issue_label(report: AgentRunReport) -> str:
    number = f"#{report.benchmark_task.issue_number} " if report.benchmark_task.issue_number else ""
    return _safe_text(f"{number}{report.benchmark_task.issue_title}")


def _md_inline(value: str) -> str:
    return _safe_text(value).replace("`", "'").replace("\n", " ")


def _md_cell(value: Any) -> str:
    return _safe_text(str(value)).replace("|", "\\|").replace("\n", " ")


def _optional_datetime(value: datetime | None) -> str:
    return value.isoformat() if value else "Unavailable"


def _optional_bool(value: bool | None) -> str:
    if value is None:
        return "Not run"
    return "yes" if value else "no"
