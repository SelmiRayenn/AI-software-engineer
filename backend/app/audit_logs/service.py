from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redaction import redact_and_truncate, redact_common_secrets, redact_structured_value
from app.models import AgentEvent, AgentRun, GeneratedPatch, TestResult
from app.run_traces import AgentRunTraceService
from app.schemas.audit_log import AuditLogEvent, AuditLogExport

AUDIT_SUMMARY_MAX_CHARS = 2_000
INTEGRITY_NOTICE = (
    "The SHA-256 hash chain provides deterministic integrity evidence for this sanitized export. "
    "It is not an external signature, timestamp authority, or tamper-proof database guarantee."
)


@dataclass(frozen=True)
class _PendingAuditEvent:
    timestamp: datetime
    event_type: str
    actor_type: Literal["system", "agent", "human", "operator"]
    summary: str
    related_entity_id: UUID
    source_rank: int

    @property
    def sort_key(self) -> tuple[datetime, int, str, str]:
        return (
            _as_utc(self.timestamp),
            self.source_rank,
            self.event_type,
            str(self.related_entity_id),
        )


class AgentRunAuditLogService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def for_run(self, run_id: UUID) -> AuditLogExport | None:
        run = self._db.get(AgentRun, run_id)
        if run is None:
            return None
        return self._build(run=run, scope_type="agent_run", scope_id=run.id)

    def for_patch(self, patch_id: UUID) -> AuditLogExport | None:
        patch = self._db.get(GeneratedPatch, patch_id)
        if patch is None:
            return None
        return self._build(
            run=patch.agent_run,
            scope_type="generated_patch",
            scope_id=patch.id,
            scoped_patch=patch,
        )

    def _build(
        self,
        *,
        run: AgentRun,
        scope_type: Literal["agent_run", "generated_patch"],
        scope_id: UUID,
        scoped_patch: GeneratedPatch | None = None,
    ) -> AuditLogExport:
        raw_events = list(
            self._db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == run.id)
                .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
            )
        )
        configuration, configuration_event = _run_configuration(run, raw_events)
        configuration_hash = _stable_hash(configuration)
        pending = self._context_events(run, configuration_hash, configuration_event)
        pending.extend(self._agent_events(run, raw_events, scoped_patch))
        pending.extend(self._patch_events(run, scoped_patch))
        pending.extend(self._test_events(run, scoped_patch))
        pending.extend(self._outcome_events(run))
        pending.extend(self._review_fallback_events(run, raw_events, scoped_patch))
        events = _chain_events(sorted(pending, key=lambda item: item.sort_key))

        return AuditLogExport(
            scope_type=scope_type,
            scope_id=scope_id,
            agent_run_id=run.id,
            benchmark_task_id=run.benchmark_task_id,
            model_provider=redact_common_secrets(run.model_provider),
            model_name=redact_common_secrets(run.model_name),
            run_configuration_hash=configuration_hash,
            exported_at=_canonical_timestamp(datetime.now(UTC)),
            event_count=len(events),
            final_audit_hash=events[-1].event_hash if events else None,
            integrity_notice=INTEGRITY_NOTICE,
            events=events,
        )

    def _context_events(
        self,
        run: AgentRun,
        configuration_hash: str,
        configuration_event: AgentEvent | None,
    ) -> list[_PendingAuditEvent]:
        configured_at = configuration_event.created_at if configuration_event else run.started_at
        return [
            _PendingAuditEvent(
                timestamp=run.started_at,
                event_type="run_created",
                actor_type="system",
                summary=f"Agent run {run.id} was created.",
                related_entity_id=run.id,
                source_rank=0,
            ),
            _PendingAuditEvent(
                timestamp=run.started_at,
                event_type="benchmark_task_referenced",
                actor_type="system",
                summary=f"Benchmark task {run.benchmark_task_id} was assigned to the run.",
                related_entity_id=run.benchmark_task_id,
                source_rank=1,
            ),
            _PendingAuditEvent(
                timestamp=run.started_at,
                event_type="model_selected",
                actor_type="operator",
                summary=(
                    "Model configuration selected: "
                    f"{redact_common_secrets(run.model_provider)}/"
                    f"{redact_common_secrets(run.model_name)}."
                ),
                related_entity_id=run.id,
                source_rank=2,
            ),
            _PendingAuditEvent(
                timestamp=configured_at,
                event_type="run_configuration_recorded",
                actor_type="operator",
                summary=f"Sanitized run configuration hash: {configuration_hash}.",
                related_entity_id=configuration_event.id if configuration_event else run.id,
                source_rank=3,
            ),
        ]

    def _agent_events(
        self,
        run: AgentRun,
        raw_events: list[AgentEvent],
        scoped_patch: GeneratedPatch | None,
    ) -> list[_PendingAuditEvent]:
        trace = AgentRunTraceService(self._db).get(run.id)
        if trace is None:
            return []
        raw_by_id = {event.id: event for event in raw_events}
        pending: list[_PendingAuditEvent] = []
        for trace_event in trace.events:
            if trace_event.event_type == "agent_run_configured":
                continue
            raw = raw_by_id.get(trace_event.id)
            if raw is None or not _event_matches_patch_scope(raw, scoped_patch):
                continue
            pending.append(
                _PendingAuditEvent(
                    timestamp=trace_event.created_at,
                    event_type=trace_event.event_type,
                    actor_type=_actor_for_event(trace_event.event_type),
                    summary=_event_audit_summary(raw, trace_event.summary),
                    related_entity_id=trace_event.id,
                    source_rank=10,
                )
            )
        return pending

    def _patch_events(
        self, run: AgentRun, scoped_patch: GeneratedPatch | None
    ) -> list[_PendingAuditEvent]:
        patches = [scoped_patch] if scoped_patch else list(run.generated_patches)
        return [
            _PendingAuditEvent(
                timestamp=patch.created_at,
                event_type="patch_version_recorded",
                actor_type="agent",
                summary=(
                    f"Generated patch version {patch.version} was recorded with "
                    f"{len(patch.changed_files)} changed file(s); selected={patch.is_selected}."
                ),
                related_entity_id=patch.id,
                source_rank=20,
            )
            for patch in patches
            if patch is not None
        ]

    def _test_events(
        self, run: AgentRun, scoped_patch: GeneratedPatch | None
    ) -> list[_PendingAuditEvent]:
        results = list(
            self._db.scalars(
                select(TestResult)
                .where(TestResult.agent_run_id == run.id)
                .order_by(TestResult.created_at.asc(), TestResult.id.asc())
            )
        )
        if scoped_patch is not None:
            results = [
                result for result in results if result.generated_patch_id in {None, scoped_patch.id}
            ]
        return [
            _PendingAuditEvent(
                timestamp=result.created_at,
                event_type="test_result_recorded",
                actor_type="system",
                summary=(
                    f"{result.phase} command completed: passed={result.passed}, "
                    f"exit_code={result.exit_code}, attempt={result.attempt_number or 1}."
                ),
                related_entity_id=result.id,
                source_rank=30,
            )
            for result in results
        ]

    def _outcome_events(self, run: AgentRun) -> list[_PendingAuditEvent]:
        pending: list[_PendingAuditEvent] = []
        selected_patch = run.generated_patch
        if run.evaluation_metric is not None:
            metric = run.evaluation_metric
            if selected_patch is not None:
                pending.append(
                    _PendingAuditEvent(
                        timestamp=metric.created_at,
                        event_type="patch_application_status_recorded",
                        actor_type="system",
                        summary=(
                            "Selected patch application status recorded: "
                            f"applied={metric.patch_applied}."
                        ),
                        related_entity_id=selected_patch.id,
                        source_rank=39,
                    )
                )
            pending.append(
                _PendingAuditEvent(
                    timestamp=metric.created_at,
                    event_type="evaluation_metrics_recorded",
                    actor_type="system",
                    summary=(
                        "Evaluation metrics recorded: "
                        f"patch_applied={metric.patch_applied}, "
                        f"post_patch_tests_passed={metric.post_patch_tests_passed}, "
                        f"issue_resolved={metric.issue_resolved}, "
                        f"review_ready={metric.review_ready}."
                    ),
                    related_entity_id=metric.id,
                    source_rank=40,
                )
            )
        if run.repair_attempts_used > 0:
            pending.append(
                _PendingAuditEvent(
                    timestamp=run.completed_at or run.started_at,
                    event_type="repair_attempts_recorded",
                    actor_type="system",
                    summary=(
                        f"Run used {run.repair_attempts_used} bounded repair attempt(s); "
                        f"final_patch_passed_tests={run.final_patch_passed_tests}."
                    ),
                    related_entity_id=run.id,
                    source_rank=42,
                )
            )
        if run.failure is not None:
            failure = run.failure
            pending.append(
                _PendingAuditEvent(
                    timestamp=failure.created_at,
                    event_type="failure_classified",
                    actor_type="system",
                    summary=(
                        f"Failure classified as {failure.category}: "
                        f"{failure.human_readable_summary}"
                    ),
                    related_entity_id=failure.id,
                    source_rank=41,
                )
            )
        return pending

    def _review_fallback_events(
        self,
        run: AgentRun,
        raw_events: list[AgentEvent],
        scoped_patch: GeneratedPatch | None,
    ) -> list[_PendingAuditEvent]:
        logged_review_ids = {
            str((event.payload_json or {}).get("human_review_id"))
            for event in raw_events
            if event.event_type == "human_review_recorded"
        }
        patches = [scoped_patch] if scoped_patch else list(run.generated_patches)
        pending: list[_PendingAuditEvent] = []
        for patch in patches:
            if patch is None or patch.human_review is None:
                continue
            review = patch.human_review
            if str(review.id) in logged_review_ids:
                continue
            pending.append(
                _PendingAuditEvent(
                    timestamp=review.reviewed_at,
                    event_type=f"patch_review_{review.decision}",
                    actor_type="human",
                    summary=f"Human reviewer {review.decision} generated patch {patch.id}.",
                    related_entity_id=review.id,
                    source_rank=50,
                )
            )
        return pending


def calculate_audit_event_hash(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chain_events(pending: list[_PendingAuditEvent]) -> list[AuditLogEvent]:
    events: list[AuditLogEvent] = []
    previous_hash: str | None = None
    for sequence, item in enumerate(pending, start=1):
        hash_fields = {
            "sequence_number": sequence,
            "timestamp": _canonical_timestamp(item.timestamp),
            "event_type": redact_common_secrets(item.event_type),
            "actor_type": item.actor_type,
            "sanitized_summary": redact_and_truncate(
                item.summary, max_chars=AUDIT_SUMMARY_MAX_CHARS
            ),
            "related_entity_id": str(item.related_entity_id),
            "previous_event_hash": previous_hash,
        }
        event_hash = calculate_audit_event_hash(hash_fields)
        events.append(AuditLogEvent(**hash_fields, event_hash=event_hash))
        previous_hash = event_hash
    return events


def _run_configuration(
    run: AgentRun, events: list[AgentEvent]
) -> tuple[dict[str, Any], AgentEvent | None]:
    configured = next(
        (event for event in reversed(events) if event.event_type == "agent_run_configured"),
        None,
    )
    raw_config = (configured.payload_json or {}).get("config") if configured else None
    if not isinstance(raw_config, dict):
        raw_config = {
            "model_provider": run.model_provider,
            "model_name": run.model_name,
        }
    sanitized = redact_structured_value(raw_config, max_string_chars=8_192)
    return sanitized if isinstance(sanitized, dict) else {}, configured


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _event_matches_patch_scope(event: AgentEvent, scoped_patch: GeneratedPatch | None) -> bool:
    if scoped_patch is None:
        return True
    payload = event.payload_json or {}
    patch_id = payload.get("generated_patch_id") or payload.get("patch_id")
    return patch_id is None or str(patch_id) == str(scoped_patch.id)


def _event_audit_summary(event: AgentEvent, fallback: str) -> str:
    payload = event.payload_json or {}
    if event.event_type == "human_review_recorded":
        decision = str(payload.get("decision") or "recorded")
        patch_id = str(payload.get("generated_patch_id") or "unknown patch")
        return f"Human review {decision} for generated patch {patch_id}."
    return fallback


def _actor_for_event(event_type: str) -> Literal["system", "agent", "human", "operator"]:
    if event_type == "human_review_recorded" or event_type.startswith("patch_review_"):
        return "human"
    if "operator" in event_type or "override" in event_type:
        return "operator"
    if event_type in {
        "candidate_files_submitted",
        "hypothesis_submitted",
        "model_call_completed",
        "model_response",
        "patch_submitted",
        "plan_submitted",
        "tool_call_requested",
    }:
        return "agent"
    return "system"


def _canonical_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
