from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.loop import registered_tool_names
from app.core.config import settings
from app.core.redaction import is_sensitive_key, redact_common_secrets, redact_structured_value
from app.models import AgentEvent, AgentRun
from app.run_traces import AgentRunTraceService
from app.schemas.agent_run import AgentRunConfig
from app.schemas.replay_snapshot import (
    AgentRunReplaySnapshot,
    ReplayBenchmarkContext,
    ReplayIntegrityMetadata,
    ReplayModelContext,
    ReplayModelResponse,
    ReplayPatchVersion,
    ReplayPromptSections,
    ReplayRepositoryContext,
    ReplayText,
    ReplayTimestamps,
    ReplayToolCall,
)

MAX_REPLAY_TEXT_BYTES = 8_192
MAX_REPLAY_COMMENT_COUNT = 50
MAX_REPLAY_TOOL_CALLS = 500
MAX_REPLAY_MODEL_RESPONSES = 250

_PROMPT_KEYS = (
    "system_prompt",
    "developer_safety_prompt",
    "issue_context_prompt",
    "tool_use_instructions",
    "patch_submission_instructions",
)


class AgentRunReplaySnapshotService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, run_id: UUID) -> AgentRunReplaySnapshot | None:
        run = self.db.get(AgentRun, run_id)
        if run is None:
            return None

        trace = AgentRunTraceService(self.db).get(run.id)
        if trace is None:
            return None

        raw_events = list(
            self.db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == run.id)
                .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
            )
        )
        config, prompt_sections = self._stored_context(raw_events)
        config_payload = config.model_dump(mode="json") if config else {}
        allowed_tools = (
            registered_tool_names(enable_test_tool=config.enable_test_tool) if config else []
        )
        task = run.benchmark_task
        repository = task.repository

        snapshot = AgentRunReplaySnapshot(
            run_id=run.id,
            run_status=run.status,
            benchmark_context=ReplayBenchmarkContext(
                id=task.id,
                issue_number=task.issue_number,
                issue_title=_safe_text(task.issue_title),
                issue_body=_bounded_text(task.issue_body or ""),
                issue_comments=(
                    _safe_comments(task.issue_comments or [])
                    if config and config.include_issue_comments
                    else []
                ),
                base_commit=task.base_commit,
                configured_test_commands=(
                    [_safe_text(command)[:1_000] for command in task.test_commands or []]
                    if config and config.enable_test_tool
                    else []
                ),
                repository=ReplayRepositoryContext(
                    id=repository.id,
                    owner=_safe_text(repository.owner),
                    name=_safe_text(repository.name),
                    url=_safe_text(repository.url),
                    default_branch=_safe_text(repository.default_branch),
                    language=_safe_text(repository.language) if repository.language else None,
                ),
            ),
            model=ReplayModelContext(provider=run.model_provider, model=run.model_name),
            run_configuration=config_payload,
            rendered_prompt_sections=prompt_sections,
            allowed_tools=allowed_tools,
            tool_call_sequence=_tool_call_sequence(trace.events),
            model_response_summaries=_model_response_summaries(trace.events),
            patch_versions=[
                ReplayPatchVersion.model_validate(patch.model_dump())
                for patch in trace.generated_patches
            ],
            test_phases=trace.test_phases,
            failure_classification=trace.failure,
            evaluation_metrics=trace.metrics,
            timestamps=ReplayTimestamps(
                run_started_at=run.started_at,
                run_completed_at=run.completed_at,
                first_event_at=trace.events[0].created_at if trace.events else None,
                last_event_at=trace.events[-1].created_at if trace.events else None,
            ),
            integrity=ReplayIntegrityMetadata(
                snapshot_created_at=datetime.now(UTC),
                source_commit_sha=(
                    _safe_text(settings.source_commit_sha)[:100]
                    if settings.source_commit_sha
                    else None
                ),
                app_version=settings.app_version,
                run_event_count=len(raw_events),
                patch_count=len(run.generated_patches),
                test_result_count=len(run.test_results),
                checksum_sha256="",
                redaction_applied=False,
                truncation_applied=False,
            ),
        )
        snapshot.integrity.redaction_applied = _contains_marker(snapshot, "[REDACTED")
        snapshot.integrity.truncation_applied = _contains_truncation(snapshot)
        snapshot.integrity.checksum_sha256 = _snapshot_checksum(snapshot)
        return snapshot

    def render_markdown(self, snapshot: AgentRunReplaySnapshot) -> str:
        context = snapshot.benchmark_context
        lines = [
            "# Agent Run Replay Snapshot",
            "",
            f"Snapshot created: {snapshot.integrity.snapshot_created_at.isoformat()}",
            f"Checksum: `{snapshot.integrity.checksum_sha256}`",
            "",
            "## Run",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Run ID | `{snapshot.run_id}` |",
            f"| Status | {_md(snapshot.run_status)} |",
            f"| Model | {_md(snapshot.model.provider)} / {_md(snapshot.model.model)} |",
            f"| Started | {snapshot.timestamps.run_started_at.isoformat()} |",
            f"| Completed | {_datetime(snapshot.timestamps.run_completed_at)} |",
            f"| App version | {_md(snapshot.integrity.app_version)} |",
            f"| Source commit | {_md(snapshot.integrity.source_commit_sha or 'unavailable')} |",
            "",
            "## Agent-Visible Task Context",
            "",
            f"Repository: `{context.repository.owner}/{context.repository.name}`",
            "",
            f"Issue: {_md(context.issue_title)}",
            "",
            context.issue_body.text or "(no issue body)",
            "",
            "## Run Configuration",
            "",
            "```json",
            json.dumps(snapshot.run_configuration, indent=2, sort_keys=True),
            "```",
            "",
            "## Rendered Prompt Sections",
            "",
        ]
        for label, value in snapshot.rendered_prompt_sections.model_dump().items():
            lines.extend([f"### {label.replace('_', ' ').title()}", ""])
            if value is None:
                lines.extend(["(not stored)", ""])
            else:
                suffix = "\n\n_[truncated]_" if value["truncated"] else ""
                lines.extend([value["text"] + suffix, ""])

        lines.extend(["## Allowed Tools", ""])
        lines.extend(f"- `{tool}`" for tool in snapshot.allowed_tools)
        lines.extend(["", "## Tool Call Sequence", ""])
        if snapshot.tool_call_sequence:
            lines.extend(["| # | Tool | Status | Files |", "| ---: | --- | --- | --- |"])
            for call in snapshot.tool_call_sequence:
                lines.append(
                    f"| {call.sequence} | `{_md(call.tool_name)}` | {_md(call.status)} | "
                    f"{_md(', '.join(call.file_paths) or '-')} |"
                )
                detail = {
                    "input": call.sanitized_input,
                    "output": call.sanitized_output,
                    "error": call.error_summary,
                }
                lines.extend(
                    [
                        "",
                        f"<details><summary>Tool call {call.sequence} sanitized detail</summary>",
                        "",
                        "```json",
                        json.dumps(detail, indent=2, sort_keys=True, default=str),
                        "```",
                        "</details>",
                    ]
                )
        else:
            lines.append("No tool calls were recorded.")

        lines.extend(["", "## Model Response Summaries", ""])
        if snapshot.model_response_summaries:
            for response in snapshot.model_response_summaries:
                tools = ", ".join(response.requested_tools) or "none"
                lines.append(
                    f"- {response.sequence}. {_md(response.summary)}; tools: {_md(tools)}; "
                    f"tokens: {response.input_tokens or 0} in / {response.output_tokens or 0} out"
                )
        else:
            lines.append("No model responses were recorded.")

        lines.extend(["", "## Patch Versions", ""])
        if snapshot.patch_versions:
            lines.extend(
                ["| Version | Selected | Changed files | Review |", "| ---: | --- | --- | --- |"]
            )
            for patch in snapshot.patch_versions:
                lines.append(
                    f"| {patch.version} | {'yes' if patch.is_selected else 'no'} | "
                    f"{_md(', '.join(patch.changed_files) or '-')} | {_md(patch.review_status)} |"
                )
        else:
            lines.append("No generated patches were stored.")

        lines.extend(["", "## Test Phases", ""])
        if snapshot.test_phases:
            lines.extend(
                [
                    "| Phase | Commands | Passed | Failed | Duration |",
                    "| --- | ---: | ---: | ---: | ---: |",
                ]
            )
            for phase in snapshot.test_phases:
                lines.append(
                    f"| {_md(phase.phase)} | {phase.command_count} | {phase.passed_count} | "
                    f"{phase.failed_count} | {phase.duration_seconds:.4f}s |"
                )
        else:
            lines.append("No test results were stored.")

        lines.extend(["", "## Failure Classification", ""])
        if snapshot.failure_classification:
            lines.extend(
                [
                    f"- Category: `{_md(snapshot.failure_classification.category)}`",
                    f"- Summary: {_md(snapshot.failure_classification.summary)}",
                ]
            )
        else:
            lines.append("No failure classification was stored.")

        lines.extend(["", "## Evaluation Metrics", ""])
        if snapshot.evaluation_metrics:
            lines.extend(
                [
                    "```json",
                    json.dumps(
                        snapshot.evaluation_metrics.model_dump(mode="json"),
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                ]
            )
        else:
            lines.append("No evaluation metrics were stored.")

        lines.extend(
            [
                "",
                "## Integrity and Safety",
                "",
                f"- Events: {snapshot.integrity.run_event_count}",
                f"- Patches: {snapshot.integrity.patch_count}",
                f"- Test results: {snapshot.integrity.test_result_count}",
                f"- Redaction applied: {'yes' if snapshot.integrity.redaction_applied else 'no'}",
                f"- Truncation applied: {'yes' if snapshot.integrity.truncation_applied else 'no'}",
                "- Gold solutions and hidden-test contents are excluded.",
                "",
            ]
        )
        return "\n".join(lines)

    def _stored_context(
        self, events: list[AgentEvent]
    ) -> tuple[AgentRunConfig | None, ReplayPromptSections]:
        configured = next(
            (event for event in reversed(events) if event.event_type == "agent_run_configured"),
            None,
        )
        payload = configured.payload_json if configured else {}
        config_payload = payload.get("config")
        try:
            config = AgentRunConfig.model_validate(config_payload) if config_payload else None
        except ValueError:
            config = None
        preview = payload.get("prompt_preview")
        if not isinstance(preview, dict):
            preview = {}
        return config, ReplayPromptSections(
            **{
                key: _bounded_text(str(preview[key])) if isinstance(preview.get(key), str) else None
                for key in _PROMPT_KEYS
            }
        )


def _tool_call_sequence(events: list[Any]) -> list[ReplayToolCall]:
    terminal_by_id: dict[str, Any] = {}
    for event in events:
        if event.event_type not in {"tool_call_completed", "tool_call_failed"}:
            continue
        call_id = _call_id(event.sanitized_payload)
        if call_id:
            terminal_by_id[call_id] = event

    calls: list[ReplayToolCall] = []
    for event in events:
        if event.event_type == "tool_call_requested":
            call = event.sanitized_payload.get("tool_call")
            if not isinstance(call, dict):
                call = event.sanitized_payload
            terminal = terminal_by_id.get(str(call.get("id") or ""))
            terminal_payload = terminal.sanitized_payload if terminal else {}
            success = terminal_payload.get("success")
            status = (
                "pending"
                if terminal is None
                else ("completed" if success is not False else "failed")
            )
            calls.append(
                ReplayToolCall(
                    sequence=len(calls) + 1,
                    event_id=event.id,
                    created_at=event.created_at,
                    tool_name=event.tool_name or str(call.get("name") or "unknown"),
                    status=status,
                    sanitized_input=call.get("arguments", {}),
                    sanitized_output=_tool_output(terminal_payload),
                    file_paths=list(
                        dict.fromkeys(event.file_paths + (terminal.file_paths if terminal else []))
                    ),
                    error_summary=_error_summary(terminal_payload) if status == "failed" else None,
                )
            )
        elif event.event_type == "agent_tool_call":
            payload = event.sanitized_payload
            calls.append(
                ReplayToolCall(
                    sequence=len(calls) + 1,
                    event_id=event.id,
                    created_at=event.created_at,
                    tool_name=event.tool_name or "unknown",
                    status="completed" if payload.get("success") is not False else "failed",
                    sanitized_input=payload.get("input") or payload.get("arguments") or {},
                    sanitized_output=_tool_output(payload),
                    file_paths=event.file_paths,
                    error_summary=_error_summary(payload)
                    if payload.get("success") is False
                    else None,
                )
            )
        if len(calls) >= MAX_REPLAY_TOOL_CALLS:
            break
    return calls


def _model_response_summaries(events: list[Any]) -> list[ReplayModelResponse]:
    responses: list[ReplayModelResponse] = []
    for event in events:
        if event.event_type != "model_call_completed":
            continue
        payload = event.sanitized_payload
        calls = payload.get("tool_calls")
        requested = []
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    requested.append(call["name"])
        responses.append(
            ReplayModelResponse(
                sequence=len(responses) + 1,
                event_id=event.id,
                created_at=event.created_at,
                summary=event.summary,
                success=payload.get("success")
                if isinstance(payload.get("success"), bool)
                else None,
                input_tokens=_optional_int(payload.get("input_tokens")),
                output_tokens=_optional_int(payload.get("output_tokens")),
                estimated_cost=_optional_float(payload.get("estimated_cost")),
                latency_seconds=_optional_float(payload.get("latency_seconds")),
                content_preview=payload.get("content_preview"),
                requested_tools=requested,
            )
        )
        if len(responses) >= MAX_REPLAY_MODEL_RESPONSES:
            break
    return responses


def _safe_comments(comments: list[dict[str, Any]]) -> list[dict[str, ReplayText | str | None]]:
    safe: list[dict[str, ReplayText | str | None]] = []
    for comment in comments[:MAX_REPLAY_COMMENT_COUNT]:
        safe.append(
            {
                "body": _bounded_text(str(comment.get("body") or "")),
                "html_url": _safe_text(str(comment.get("html_url") or "")) or None,
                "user_login": _safe_text(str(comment.get("user_login") or "")) or None,
                "created_at": _safe_text(str(comment.get("created_at") or "")) or None,
                "updated_at": _safe_text(str(comment.get("updated_at") or "")) or None,
            }
        )
    return safe


def _bounded_text(value: str) -> ReplayText:
    safe = _safe_text(value)
    encoded = safe.encode("utf-8")
    truncated = len(encoded) > MAX_REPLAY_TEXT_BYTES
    if truncated:
        safe = encoded[:MAX_REPLAY_TEXT_BYTES].decode("utf-8", errors="ignore")
    return ReplayText(
        text=safe,
        original_size_bytes=len(encoded),
        truncated=truncated,
        redacted="[REDACTED" in safe,
    )


def _safe_text(value: str) -> str:
    return redact_common_secrets(value)


def _call_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("tool_call_id") or payload.get("id")
    if value is None and isinstance(payload.get("tool_call"), dict):
        value = payload["tool_call"].get("id")
    return str(value) if value else None


def _tool_output(payload: dict[str, Any]) -> Any:
    for key in ("result", "observation", "output", "summary"):
        if key in payload:
            return _replay_safe_tool_value(payload[key])
    if not payload:
        return None
    return _replay_safe_tool_value(
        {
            key: value
            for key, value in payload.items()
            if key not in {"arguments", "input", "tool_call", "tool_call_id", "tool_name", "name"}
        }
    )


def _replay_safe_tool_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if is_sensitive_key(normalized):
                safe[str(key)] = "[REDACTED]"
            elif normalized in {"content", "patch_text", "stdout", "stderr", "raw_response"}:
                safe[str(key)] = "[OMITTED: raw content is not included in replay snapshots]"
            elif normalized in {"gold", "hidden_tests"} or any(
                marker in normalized
                for marker in (
                    "gold_patch",
                    "gold_solution",
                    "test_patch",
                    "hidden_test_commands",
                    "hidden_eval_commands",
                    "hidden_test_files",
                    "fail_to_pass",
                    "pass_to_pass",
                )
            ):
                safe[str(key)] = "[REDACTED: protected benchmark data]"
            else:
                safe[str(key)] = _replay_safe_tool_value(nested)
        return redact_structured_value(safe, max_string_chars=MAX_REPLAY_TEXT_BYTES)
    if isinstance(value, list):
        return [_replay_safe_tool_value(item) for item in value]
    return redact_structured_value(value, max_string_chars=MAX_REPLAY_TEXT_BYTES)


def _error_summary(payload: dict[str, Any]) -> str | None:
    value = payload.get("error_message") or payload.get("error")
    return _safe_text(str(value))[:2_000] if value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _snapshot_checksum(snapshot: AgentRunReplaySnapshot) -> str:
    payload = snapshot.model_dump(mode="json")
    integrity = payload["integrity"]
    integrity.pop("snapshot_created_at", None)
    integrity.pop("checksum_sha256", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _contains_marker(value: Any, marker: str) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item, marker) for item in value)
    return isinstance(value, str) and marker in value


def _contains_truncation(value: Any) -> bool:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        if value.get("truncated") is True or value.get("_truncated") is True:
            return True
        if "_truncated_items" in value or "_truncated_keys" in value:
            return True
        return any(_contains_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncation(item) for item in value)
    return isinstance(value, str) and value.startswith("[TRUNCATED:")


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _datetime(value: datetime | None) -> str:
    return value.isoformat() if value else "-"
