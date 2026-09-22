from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import redact_prompt_text
from app.models import AgentEvent, AgentRun, TestResult
from app.schemas.run_trace import (
    AgentRunTraceEvent,
    AgentRunTraceRead,
    TraceFailureSummary,
    TraceMetricSummary,
    TracePatchSummary,
    TraceTestPhaseSummary,
)

MAX_TRACE_STRING_CHARS = 2_000
MAX_TRACE_LIST_ITEMS = 50
MAX_TRACE_OBJECT_KEYS = 100
MAX_TRACE_DEPTH = 8
MAX_TRACE_PAYLOAD_BYTES = 16_384

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "refresh_token",
    "access_token",
    "auth_token",
    "secret",
)
_PROTECTED_KEY_PARTS = (
    "gold_patch",
    "gold_solution",
    "test_patch",
    "hidden_test_commands",
    "hidden_eval_commands",
    "hidden_test_files",
    "fail_to_pass",
    "pass_to_pass",
)
_HIDDEN_SAFE_KEYS = {
    "phase",
    "passed",
    "success",
    "command_count",
    "total_count",
    "passed_count",
    "failed_count",
    "duration_seconds",
    "hidden_tests_passed",
    "hidden_tests_run_count",
    "hidden_tests_failed_count",
}
_PATH_KEYS = {"path", "file_path", "files", "files_read", "files_modified", "changed_files"}


class AgentRunTraceService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, run_id: UUID) -> AgentRunTraceRead | None:
        run = self.db.get(AgentRun, run_id)
        if run is None:
            return None

        events = list(
            self.db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == run.id)
                .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
            )
        )
        return AgentRunTraceRead(
            run_id=run.id,
            status=run.status,
            events=[self._event_read(event) for event in events],
            generated_patches=[
                TracePatchSummary(
                    id=patch.id,
                    version=patch.version,
                    is_selected=patch.is_selected,
                    changed_files=_safe_paths(patch.changed_files),
                    review_status=patch.review_status,
                    created_at=patch.created_at,
                )
                for patch in sorted(run.generated_patches, key=lambda item: item.version)
            ],
            test_phases=self._test_phase_summaries(run.id),
            failure=(
                TraceFailureSummary(
                    category=run.failure.category,
                    summary=_safe_text(run.failure.human_readable_summary)[:MAX_TRACE_STRING_CHARS],
                    source_event_id=run.failure.source_event_id,
                    created_at=run.failure.created_at,
                )
                if run.failure
                else None
            ),
            metrics=(
                TraceMetricSummary.model_validate(run.evaluation_metric, from_attributes=True)
                if run.evaluation_metric
                else None
            ),
        )

    def _event_read(self, event: AgentEvent) -> AgentRunTraceEvent:
        raw_payload = event.payload_json or {}
        hidden_context = _is_hidden_context(event.event_type, raw_payload)
        sanitized = _sanitize_payload(raw_payload, hidden_context=hidden_context)
        return AgentRunTraceEvent(
            id=event.id,
            created_at=event.created_at,
            event_type=event.event_type,
            summary=_event_summary(event.event_type, sanitized),
            sanitized_payload=sanitized,
            tool_name=_tool_name(sanitized),
            file_paths=[] if hidden_context else _extract_file_paths(raw_payload),
            severity=_event_severity(event.event_type, sanitized),
        )

    def _test_phase_summaries(self, run_id: UUID) -> list[TraceTestPhaseSummary]:
        results = list(
            self.db.scalars(
                select(TestResult)
                .where(TestResult.agent_run_id == run_id)
                .order_by(TestResult.created_at.asc(), TestResult.id.asc())
            )
        )
        grouped: dict[str, list[TestResult]] = defaultdict(list)
        for result in results:
            grouped[result.phase].append(result)

        preferred_order = {"setup": 0, "baseline": 1, "post_patch": 2, "hidden_eval": 3}
        return [
            TraceTestPhaseSummary(
                phase=phase,
                command_count=len(phase_results),
                passed_count=sum(result.passed for result in phase_results),
                failed_count=sum(not result.passed for result in phase_results),
                duration_seconds=round(
                    sum(result.duration_seconds or 0.0 for result in phase_results), 4
                ),
            )
            for phase, phase_results in sorted(
                grouped.items(), key=lambda item: (preferred_order.get(item[0], 99), item[0])
            )
        ]


def _sanitize_payload(payload: dict[str, Any], *, hidden_context: bool) -> dict[str, Any]:
    if hidden_context:
        payload = {
            key: value
            for key, value in payload.items()
            if _normalized_key(key) in _HIDDEN_SAFE_KEYS
        }
    sanitized = _sanitize_value(payload, depth=0)
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}

    encoded = json.dumps(sanitized, sort_keys=True, default=str).encode("utf-8")
    if len(encoded) <= MAX_TRACE_PAYLOAD_BYTES:
        return sanitized
    return {
        "_truncated": True,
        "original_size_bytes": len(encoded),
        "preview": encoded[:MAX_TRACE_PAYLOAD_BYTES].decode("utf-8", errors="ignore"),
    }


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= MAX_TRACE_DEPTH:
        return "[TRUNCATED: maximum depth reached]"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        items = list(value.items())
        for key, nested in items[:MAX_TRACE_OBJECT_KEYS]:
            key_string = str(key)
            normalized = _normalized_key(key_string)
            if _is_sensitive_key(normalized):
                sanitized[key_string] = "[REDACTED]"
            elif _is_protected_key(normalized):
                sanitized[key_string] = "[REDACTED: protected benchmark data]"
            elif normalized in {
                "patch_text",
                "stdout",
                "stderr",
                "content",
                "raw_response",
            } and isinstance(nested, str):
                sanitized[key_string] = _bounded_text_value(nested)
            else:
                sanitized[key_string] = _sanitize_value(nested, depth=depth + 1)
        if len(items) > MAX_TRACE_OBJECT_KEYS:
            sanitized["_truncated_keys"] = len(items) - MAX_TRACE_OBJECT_KEYS
        return sanitized
    if isinstance(value, (list, tuple)):
        items = [_sanitize_value(item, depth=depth + 1) for item in value[:MAX_TRACE_LIST_ITEMS]]
        if len(value) > MAX_TRACE_LIST_ITEMS:
            items.append({"_truncated_items": len(value) - MAX_TRACE_LIST_ITEMS})
        return items
    if isinstance(value, str):
        return _bounded_text_value(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text_value(str(value))


def _bounded_text_value(value: Any) -> Any:
    text = _safe_text(str(value))
    if len(text) <= MAX_TRACE_STRING_CHARS:
        return text
    return {
        "size_bytes": len(text.encode("utf-8")),
        "preview": text[:MAX_TRACE_STRING_CHARS],
        "truncated": True,
    }


def _safe_text(value: str) -> str:
    return redact_prompt_text(value)


def _normalized_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    return (
        key == "token"
        or key.endswith("_token")
        or any(part in key for part in _SENSITIVE_KEY_PARTS)
    )


def _is_protected_key(key: str) -> bool:
    return key in {"gold", "hidden_tests"} or any(part in key for part in _PROTECTED_KEY_PARTS)


def _is_hidden_context(event_type: str, payload: dict[str, Any]) -> bool:
    return "hidden_eval" in event_type.lower() or payload.get("phase") == "hidden_eval"


def _tool_name(payload: dict[str, Any]) -> str | None:
    value = payload.get("tool_name") or payload.get("name")
    if isinstance(value, str) and value.strip():
        return value
    raw_tool_call = payload.get("tool_call")
    if isinstance(raw_tool_call, dict):
        nested_name = raw_tool_call.get("tool_name") or raw_tool_call.get("name")
        if isinstance(nested_name, str) and nested_name.strip():
            return nested_name
    return None


def _extract_file_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                visit(nested_value, _normalized_key(nested_key))
            return
        if isinstance(value, (list, tuple)):
            for nested in value[:MAX_TRACE_LIST_ITEMS]:
                visit(nested, key)
            return
        if key in _PATH_KEYS and isinstance(value, str):
            paths.append(value)

    visit(payload)
    return _safe_paths(paths)


def _safe_paths(paths: list[str]) -> list[str]:
    safe: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/").strip()
        lowered = normalized.lower()
        if not normalized or any(
            marker in lowered for marker in ("gold", "hidden_eval", ".benchmark")
        ):
            continue
        redacted = _safe_text(normalized)[:500]
        if redacted not in safe:
            safe.append(redacted)
        if len(safe) >= 50:
            break
    return safe


def _event_severity(event_type: str, payload: dict[str, Any]) -> str:
    event_name = event_type.lower()
    if (
        payload.get("success") is False
        or payload.get("passed") is False
        or any(marker in event_name for marker in ("failed", "error", "rejected"))
    ):
        return "error"
    if any(
        marker in event_name
        for marker in ("warning", "cancelled", "timeout", "limit_reached", "malformed", "unknown")
    ):
        return "warning"
    return "info"


def _event_summary(event_type: str, payload: dict[str, Any]) -> str:
    tool_name = _tool_name(payload)
    if event_type == "agent_tool_call" or event_type.startswith("tool_call_"):
        if event_type == "tool_call_requested":
            outcome = "requested"
        elif payload.get("success") is False or event_type.endswith("failed"):
            outcome = "failed"
        else:
            outcome = "completed"
        return f"Tool {outcome}: {tool_name or 'unknown tool'}"
    if event_type == "model_call_started":
        return "Model call started"
    if event_type in {"model_call_completed", "model_response"}:
        return "Model response received"
    if event_type in {"patch_submitted", "generated_patch_stored"}:
        return "Agent submitted a generated patch"
    if event_type == "patch_applied":
        return "Generated patch applied"
    if event_type == "test_phase_completed":
        phase = str(payload.get("phase") or "test").replace("_", " ")
        result = "passed" if payload.get("passed") is True else "failed"
        return f"{phase.capitalize()} phase {result}"
    if event_type == "evaluation_metric_calculated":
        return "Evaluation metrics calculated"
    if event_type == "agent_run_failed":
        return "Agent run failed"
    if event_type == "agent_run_completed":
        return "Agent run completed"
    return event_type.replace("_", " ").strip().capitalize() or "Agent event"
