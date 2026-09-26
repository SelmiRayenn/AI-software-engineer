from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redaction import redact_common_secrets
from app.core.test_phases import TEST_PHASE_HIDDEN_EVAL
from app.models import AgentEvent, AgentRun, TestResult
from app.schemas.test_failure_analysis import TestFailureAnalysisRead

EVENT_TYPE = "test_failure_analysis"
MAX_ANALYSIS_SOURCE_BYTES = 32_768
MAX_COMMAND_BYTES = 1_024
MAX_SUMMARY_BYTES = 512
MAX_EXCERPT_BYTES = 512
MAX_ITEMS_PER_FIELD = 8
TRUNCATION_MARKERS = ("[truncated to last ", "[feedback truncated]", "_truncated")

_PYTEST_NODE_RE = re.compile(r"(?m)^(?:FAILED|ERROR)\s+([^\s]+(?:::[^\s]+)+)")
_PYTEST_HEADER_RE = re.compile(r"(?m)^_{2,}\s+([^\n]+?)\s+_{2,}$")
_STACK_FILE_RE = re.compile(r'^\s*File\s+["\']([^"\']+)["\'],\s+line\s+\d+', re.IGNORECASE)
_PATH_LINE_RE = re.compile(
    r"(?<![\w.-])([\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|rb|php|cs|cpp|c|h))"
    r"(?::\d+)?"
)
_ERROR_LINE_RE = re.compile(
    r"(?:AssertionError|\bassert\b|(?:^|\s)E\s+|[A-Za-z]+Error:|Exception:|expected\b|actual\b)",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"(?:timed?\s*out|timeout|exceeded\s+(?:the\s+)?timeout)", re.IGNORECASE)


class TestFailureAnalysisService:
    """Create bounded, shareable analyses from immutable test result records."""

    def __init__(self, db: Session, agent_run_id: UUID) -> None:
        self._db = db
        self._agent_run_id = agent_run_id

    def list_for_run(self) -> list[TestFailureAnalysisRead]:
        if self._db.get(AgentRun, self._agent_run_id) is None:
            raise LookupError("Agent run not found.")
        results = list(
            self._db.scalars(
                select(TestResult)
                .where(TestResult.agent_run_id == self._agent_run_id, TestResult.passed.is_(False))
                .order_by(TestResult.created_at.asc(), TestResult.id.asc())
            )
        )
        return self.analyze_results(results)

    def analyze_results(
        self,
        results: Iterable[TestResult],
        *,
        persist: bool = True,
        trusted: bool = False,
    ) -> list[TestFailureAnalysisRead]:
        failed = [result for result in results if not result.passed]
        analyses: list[TestFailureAnalysisRead] = []
        hidden_added = False
        for result in failed:
            if result.agent_run_id != self._agent_run_id:
                raise ValueError("Test result does not belong to this agent run.")
            hidden_result = result.phase == TEST_PHASE_HIDDEN_EVAL
            if hidden_result and not trusted:
                if hidden_added:
                    continue
                analysis = self._hidden_aggregate()
                hidden_added = True
                key = "hidden_eval:aggregate"
            else:
                analysis = _analyze_visible_result(result)
                key = f"test_result:{result.id}"
            should_persist = persist and not (hidden_result and trusted)
            analyses.append(self._persist(key, analysis) if should_persist else analysis)
        return analyses

    def _hidden_aggregate(self) -> TestFailureAnalysisRead:
        hidden_results = list(
            self._db.scalars(
                select(TestResult)
                .where(
                    TestResult.agent_run_id == self._agent_run_id,
                    TestResult.phase == TEST_PHASE_HIDDEN_EVAL,
                )
                .order_by(TestResult.created_at.asc(), TestResult.id.asc())
            )
        )
        failed = [result for result in hidden_results if not result.passed]
        timed_out = any(_is_timeout(result) for result in failed)
        summary = (
            f"Hidden evaluation failed: {len(failed)} of {len(hidden_results)} commands failed. "
            "Command, test, path, excerpt, and stack details are restricted."
        )
        return TestFailureAnalysisRead(
            phase=TEST_PHASE_HIDDEN_EVAL,
            concise_failure_summary=summary,
            timed_out=timed_out,
            command_failed=bool(failed) and not timed_out,
            output_truncated=any(_output_was_truncated(result) for result in failed),
            hidden_details_redacted=True,
            failed_result_count=len(failed),
            total_result_count=len(hidden_results),
        )

    def _persist(
        self,
        analysis_key: str,
        analysis: TestFailureAnalysisRead,
    ) -> TestFailureAnalysisRead:
        existing = next(
            (
                event
                for event in self._db.scalars(
                    select(AgentEvent).where(
                        AgentEvent.agent_run_id == self._agent_run_id,
                        AgentEvent.event_type == EVENT_TYPE,
                    )
                )
                if event.payload_json.get("analysis_key") == analysis_key
            ),
            None,
        )
        payload = analysis.model_dump(
            mode="json", exclude={"event_id", "created_at"}, exclude_none=False
        )
        payload.update({"schema_version": 1, "analysis_key": analysis_key})
        if existing is None:
            existing = AgentEvent(
                agent_run_id=self._agent_run_id,
                event_type=EVENT_TYPE,
                payload_json=payload,
            )
            self._db.add(existing)
        else:
            existing.payload_json = payload
        self._db.commit()
        self._db.refresh(existing)
        return analysis.model_copy(
            update={"event_id": existing.id, "created_at": existing.created_at}
        )


def format_repair_feedback(
    analyses: list[TestFailureAnalysisRead],
    *,
    total_results: int,
    include_details: bool,
) -> str:
    lines = [
        f"Post-patch test phase failed: {len(analyses)} of {total_results} commands failed.",
        "Structured failure analysis:",
    ]
    for position, analysis in enumerate(analyses[:5], start=1):
        lines.extend(
            [
                f"Failure {position}:",
                (
                    f"- command: {analysis.failed_command or '[restricted]'}"
                    if include_details
                    else "- command: [details disabled]"
                ),
                f"- phase: {analysis.phase}",
                f"- exit_code: {analysis.exit_code}",
                (
                    f"- summary: {analysis.concise_failure_summary}"
                    if include_details
                    else f"- summary: Test command exited with code {analysis.exit_code}."
                ),
                f"- timed_out: {str(analysis.timed_out).lower()}",
                f"- output_truncated: {str(analysis.output_truncated).lower()}",
            ]
        )
        if include_details:
            if analysis.likely_failing_test_names:
                lines.append("- likely_tests: " + ", ".join(analysis.likely_failing_test_names))
            if analysis.affected_file_paths:
                lines.append("- affected_files: " + ", ".join(analysis.affected_file_paths))
            if analysis.assertion_error_excerpts:
                lines.append("- error_excerpts: " + " | ".join(analysis.assertion_error_excerpts))
            if analysis.stack_trace_snippets:
                lines.append("- stack_snippets: " + " | ".join(analysis.stack_trace_snippets))
    return "\n".join(lines)


def _analyze_visible_result(result: TestResult) -> TestFailureAnalysisRead:
    raw_output = "\n".join(value for value in (result.stderr, result.stdout) if value)
    output_truncated = _output_was_truncated(result)
    safe_output, source_truncated = _bounded(
        redact_common_secrets(raw_output), MAX_ANALYSIS_SOURCE_BYTES
    )
    output_truncated = output_truncated or source_truncated
    lines = [line.strip() for line in safe_output.splitlines() if line.strip()]
    tests = _likely_test_names(safe_output)
    excerpts = _matching_lines(lines, _ERROR_LINE_RE)
    stack = _stack_snippets(lines)
    paths = _affected_paths(safe_output)
    timed_out = _is_timeout(result)
    summary = _failure_summary(result, lines, excerpts, timed_out)
    command, _ = _bounded(redact_common_secrets(result.command), MAX_COMMAND_BYTES)
    return TestFailureAnalysisRead(
        test_result_id=result.id,
        phase=result.phase,
        failed_command=command,
        exit_code=result.exit_code,
        concise_failure_summary=summary,
        likely_failing_test_names=tests,
        assertion_error_excerpts=excerpts,
        stack_trace_snippets=stack,
        affected_file_paths=paths,
        timed_out=timed_out,
        command_failed=result.exit_code != 0 and not timed_out,
        output_truncated=output_truncated,
    )


def _failure_summary(
    result: TestResult,
    lines: list[str],
    excerpts: list[str],
    timed_out: bool,
) -> str:
    if timed_out:
        return "Test command timed out before completing."
    preferred = next(
        (line for line in excerpts if re.search(r"(?:AssertionError|[A-Za-z]+Error:)", line)),
        None,
    )
    candidate = preferred or (excerpts[0] if excerpts else None)
    candidate = candidate or (lines[-1] if lines else "Test command failed.")
    candidate, _ = _bounded(candidate, MAX_SUMMARY_BYTES)
    return candidate or f"Test command exited with code {result.exit_code}."


def _likely_test_names(text: str) -> list[str]:
    values = list(_PYTEST_NODE_RE.findall(text))
    for header in _PYTEST_HEADER_RE.findall(text):
        name = header.strip()
        if name.lower().startswith(("short test summary", "failures", "errors")):
            continue
        if "test" in name.lower():
            values.append(name)
    return _bounded_unique(values)


def _matching_lines(lines: list[str], pattern: re.Pattern[str]) -> list[str]:
    return _bounded_unique(line for line in lines if pattern.search(line))


def _stack_snippets(lines: list[str]) -> list[str]:
    return _bounded_unique(
        line
        for line in lines
        if _STACK_FILE_RE.search(line) or (_PATH_LINE_RE.search(line) and ":" in line)
    )


def _affected_paths(text: str) -> list[str]:
    values = list(_STACK_FILE_RE.findall(text)) + list(_PATH_LINE_RE.findall(text))
    safe: list[str] = []
    for value in values:
        normalized = str(PurePath(value.replace("\\", "/"))).replace("\\", "/")
        lowered = normalized.lower()
        if any(marker in lowered for marker in ("gold_patch", "gold_solution", "hidden_eval")):
            continue
        safe.append(normalized)
    return _bounded_unique(safe)


def _bounded_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        bounded, _ = _bounded(redact_common_secrets(value.strip()), MAX_EXCERPT_BYTES)
        if bounded and bounded not in result:
            result.append(bounded)
        if len(result) >= MAX_ITEMS_PER_FIELD:
            break
    return result


def _is_timeout(result: TestResult) -> bool:
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    return result.exit_code == 124 or bool(_TIMEOUT_RE.search(output))


def _output_was_truncated(result: TestResult) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(marker in output for marker in TRUNCATION_MARKERS)


def _bounded(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    marker = b" [truncated]"
    prefix = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return prefix + marker.decode(), True
