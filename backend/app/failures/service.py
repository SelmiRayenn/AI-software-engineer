from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.failures.categories import (
    FAILURE_BASELINE_TESTS_FAILED,
    FAILURE_CANCELLED,
    FAILURE_CATEGORIES,
    FAILURE_DOCKER_UNAVAILABLE,
    FAILURE_MALFORMED_TOOL_CALL,
    FAILURE_MAX_REPAIR_ATTEMPTS_REACHED,
    FAILURE_MAX_STEPS_REACHED,
    FAILURE_MODEL_PROVIDER_ERROR,
    FAILURE_PATCH_APPLY_FAILED,
    FAILURE_PATCH_GENERATION_FAILED,
    FAILURE_PATCH_QUALITY_BLOCKED,
    FAILURE_POST_PATCH_TESTS_FAILED,
    FAILURE_REPOSITORY_CHECKOUT_FAILED,
    FAILURE_SETUP_FAILED,
    FAILURE_TIMEOUT,
    FAILURE_TOOL_ERROR_LIMIT_REACHED,
    FAILURE_UNKNOWN,
    FAILURE_UNKNOWN_TOOL,
)
from app.models import AgentEvent, AgentRun, AgentRunFailure

MAX_FAILURE_SUMMARY_LENGTH = 2000


class FailureClassificationError(RuntimeError):
    pass


class AgentRunNotFoundError(FailureClassificationError):
    pass


class AgentRunFailureNotFoundError(FailureClassificationError):
    pass


class FailureClassificationService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def classify(
        self,
        *,
        agent_run_id: UUID,
        category: str,
        summary: str,
        source_event_id: UUID | None = None,
    ) -> AgentRunFailure:
        run = self._db.get(AgentRun, agent_run_id)
        if run is None:
            raise AgentRunNotFoundError("Agent run not found.")
        if category not in FAILURE_CATEGORIES:
            raise FailureClassificationError(f"Unknown failure category: {category}")
        if source_event_id is not None:
            source_event = self._db.get(AgentEvent, source_event_id)
            if source_event is None or source_event.agent_run_id != agent_run_id:
                raise FailureClassificationError(
                    "Failure source event must belong to the classified agent run."
                )

        safe_summary = _safe_summary(summary, category)
        failure = run.failure
        if failure is None:
            failure = AgentRunFailure(
                agent_run_id=agent_run_id,
                category=category,
                human_readable_summary=safe_summary,
                source_event_id=source_event_id,
            )
            self._db.add(failure)
        else:
            failure.category = category
            failure.human_readable_summary = safe_summary
            failure.source_event_id = source_event_id
        run.failure_summary = safe_summary
        self._db.add(run)
        self._db.commit()
        self._db.refresh(failure)
        return failure

    def get(self, agent_run_id: UUID) -> AgentRunFailure:
        run = self._db.get(AgentRun, agent_run_id)
        if run is None:
            raise AgentRunNotFoundError("Agent run not found.")
        if run.failure is None:
            raise AgentRunFailureNotFoundError("Failure classification not found for agent run.")
        return run.failure

    def get_or_classify(self, agent_run_id: UUID) -> AgentRunFailure:
        run = self._db.get(AgentRun, agent_run_id)
        if run is None:
            raise AgentRunNotFoundError("Agent run not found.")
        if run.failure is not None:
            return run.failure
        inferred = self._infer(run)
        if inferred is None:
            raise AgentRunFailureNotFoundError("Failure classification not found for agent run.")
        category, summary, source_event_id = inferred
        return self.classify(
            agent_run_id=run.id,
            category=category,
            summary=summary,
            source_event_id=source_event_id,
        )

    def _infer(self, run: AgentRun) -> tuple[str, str, UUID | None] | None:
        events = list(
            self._db.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == run.id)
                .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
            )
        )
        for event in events:
            payload = event.payload_json or {}
            category = _category_for_event(event.event_type, payload)
            if category:
                summary = str(
                    payload.get("error_message")
                    or payload.get("failure_summary")
                    or run.failure_summary
                    or _default_summary(category)
                )
                return category, summary, event.id
        if run.status == "cancelled":
            return (
                FAILURE_CANCELLED,
                run.failure_summary or _default_summary(FAILURE_CANCELLED),
                None,
            )
        if run.status == "failed":
            summary = run.failure_summary or "Agent run failed for an unclassified reason."
            return classify_failure_text(summary), summary, None
        return None


def classify_failure_text(summary: str, *, default: str = FAILURE_UNKNOWN) -> str:
    value = summary.lower()
    rules = (
        (FAILURE_DOCKER_UNAVAILABLE, ("docker is unavailable", "docker daemon")),
        (FAILURE_TIMEOUT, ("timed out", "timeout", "exceeded timeout")),
        (FAILURE_SETUP_FAILED, ("setup phase failed", "setup command failed")),
        (FAILURE_BASELINE_TESTS_FAILED, ("baseline test",)),
        (
            FAILURE_MODEL_PROVIDER_ERROR,
            ("provider call failed", "model provider", "real model calls"),
        ),
        (FAILURE_UNKNOWN_TOOL, ("unknown tool",)),
        (FAILURE_MALFORMED_TOOL_CALL, ("tool call must", "tool call id", "tool arguments")),
        (FAILURE_TOOL_ERROR_LIMIT_REACHED, ("tool error limit",)),
        (FAILURE_PATCH_QUALITY_BLOCKED, ("quality guardrail", "patch quality")),
        (FAILURE_PATCH_APPLY_FAILED, ("patch could not be applied", "does not apply cleanly")),
        (FAILURE_POST_PATCH_TESTS_FAILED, ("post-patch test", "post_patch test")),
        (FAILURE_MAX_STEPS_REACHED, ("step limit", "maximum steps")),
        (FAILURE_MAX_REPAIR_ATTEMPTS_REACHED, ("repair attempts", "repair limit")),
        (FAILURE_REPOSITORY_CHECKOUT_FAILED, ("workspace preparation", "checkout", "git clone")),
        (FAILURE_CANCELLED, ("cancelled", "canceled")),
        (FAILURE_PATCH_GENERATION_FAILED, ("no valid patch", "patch generation", "no patch")),
    )
    for category, phrases in rules:
        if any(phrase in value for phrase in phrases):
            return category
    return default


def _category_for_event(event_type: str, payload: dict) -> str | None:
    explicit_category = payload.get("failure_category")
    if explicit_category in FAILURE_CATEGORIES:
        return explicit_category
    if event_type == "repair_limit_reached":
        return FAILURE_MAX_REPAIR_ATTEMPTS_REACHED
    if event_type == "patch_quality_rejected":
        return FAILURE_PATCH_QUALITY_BLOCKED
    if event_type == "step_limit_reached":
        return FAILURE_MAX_STEPS_REACHED
    if event_type == "model_call_completed" and payload.get("success") is False:
        return FAILURE_MODEL_PROVIDER_ERROR
    if event_type == "tool_call_failed":
        return classify_failure_text(
            str(payload.get("error_message") or ""),
            default=FAILURE_TOOL_ERROR_LIMIT_REACHED,
        )
    if event_type == "test_phase_completed" and payload.get("passed") is False:
        return {
            "setup": FAILURE_SETUP_FAILED,
            "baseline": FAILURE_BASELINE_TESTS_FAILED,
            "post_patch": FAILURE_POST_PATCH_TESTS_FAILED,
        }.get(payload.get("phase"))
    if event_type == "agent_run_failed":
        return classify_failure_text(str(payload.get("error_message") or ""))
    return None


def _safe_summary(summary: str, category: str) -> str:
    value = _redact_summary(summary).strip() or _default_summary(category)
    return value[:MAX_FAILURE_SUMMARY_LENGTH]


def _default_summary(category: str) -> str:
    return category.replace("_", " ").capitalize() + "."


def _redact_summary(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    redacted = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b",
        "[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        redacted,
    )
