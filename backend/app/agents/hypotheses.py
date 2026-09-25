from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import redact_prompt_text
from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
from app.models import AgentEvent
from app.schemas.agent_hypothesis import AgentHypothesisInput, AgentHypothesisRead

MAX_HYPOTHESIS_BYTES = 16_384


class AgentHypothesisService:
    """Stores bounded run hypotheses without consulting trusted benchmark data."""

    def __init__(self, db: Session, run_id: UUID, tools: AgentWorkspaceTools) -> None:
        self.db = db
        self.run_id = run_id
        self.tools = tools

    def require_for_patch(self, *, enabled: bool) -> None:
        if enabled and active_run_hypothesis(self.db, self.run_id) is None:
            raise ToolSafetyError(
                "An active or confirmed root-cause hypothesis is required before submit_patch. "
                "Inspect the repository, then call submit_hypothesis with supporting evidence."
            )

    def submit(self, **arguments: object) -> dict:
        try:
            candidate = AgentHypothesisInput.model_validate(arguments)
        except ValidationError as exc:
            raise ToolSafetyError(
                "Invalid hypothesis: provide summary, suspected_files, supporting_evidence, "
                "confidence (low/medium/high), and status (active/revised/rejected/confirmed)."
            ) from exc

        if len(candidate.model_dump_json().encode("utf-8")) > MAX_HYPOTHESIS_BYTES:
            raise ToolSafetyError(
                "Hypothesis exceeds the 16384-byte limit; submit a shorter revision."
            )
        try:
            candidate.suspected_files = list(
                dict.fromkeys(
                    self.tools.validate_plan_path(path, must_exist=True)
                    for path in candidate.suspected_files
                )
            )
        except (OSError, ValueError) as exc:
            raise ToolSafetyError("Invalid hypothesis file path.") from exc

        hypothesis = _redacted_hypothesis(candidate)
        if len(hypothesis.model_dump_json().encode("utf-8")) > MAX_HYPOTHESIS_BYTES:
            raise ToolSafetyError(
                "Redacted hypothesis exceeds the 16384-byte limit; submit a shorter revision."
            )

        events = _hypothesis_events(self.db, self.run_id)
        revision = len(events) + 1
        if hypothesis.status in {"active", "confirmed"}:
            for event in events:
                payload = dict(event.payload_json or {})
                if payload.get("status") in {"active", "confirmed"}:
                    event.payload_json = {
                        **payload,
                        "status": "revised",
                        "superseded_by_revision": revision,
                    }

        record = AgentHypothesisRead(
            **hypothesis.model_dump(),
            revision=revision,
            created_at=datetime.now(UTC),
        )
        self.db.add(
            AgentEvent(
                agent_run_id=self.run_id,
                event_type="hypothesis_submitted",
                payload_json={"tool_name": "submit_hypothesis", **record.model_dump(mode="json")},
                created_at=record.created_at,
            )
        )
        self.db.commit()
        return record.model_dump(mode="json")


def run_hypotheses(db: Session, run_id: UUID) -> list[AgentHypothesisRead]:
    hypotheses: list[AgentHypothesisRead] = []
    for event in _hypothesis_events(db, run_id):
        try:
            record = AgentHypothesisRead.model_validate(event.payload_json)
            hypotheses.append(_redacted_hypothesis_read(record))
        except (ValidationError, TypeError):
            continue
    return hypotheses


def active_run_hypothesis(db: Session, run_id: UUID) -> AgentHypothesisRead | None:
    for hypothesis in reversed(run_hypotheses(db, run_id)):
        if hypothesis.status in {"active", "confirmed"}:
            return hypothesis
    return None


def _hypothesis_events(db: Session, run_id: UUID) -> list[AgentEvent]:
    return list(
        db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == run_id,
                AgentEvent.event_type == "hypothesis_submitted",
            )
            .order_by(AgentEvent.created_at.asc(), AgentEvent.id.asc())
        )
    )


def _redacted_hypothesis(hypothesis: AgentHypothesisInput) -> AgentHypothesisInput:
    return AgentHypothesisInput.model_validate(
        {
            **hypothesis.model_dump(),
            "summary": redact_prompt_text(hypothesis.summary),
            "suspected_files": [redact_prompt_text(path) for path in hypothesis.suspected_files],
            "supporting_evidence": [
                redact_prompt_text(evidence) for evidence in hypothesis.supporting_evidence
            ],
        }
    )


def _redacted_hypothesis_read(hypothesis: AgentHypothesisRead) -> AgentHypothesisRead:
    safe = _redacted_hypothesis(
        AgentHypothesisInput.model_validate(
            hypothesis.model_dump(exclude={"revision", "superseded_by_revision", "created_at"})
        )
    )
    return AgentHypothesisRead(
        **safe.model_dump(),
        revision=hypothesis.revision,
        superseded_by_revision=hypothesis.superseded_by_revision,
        created_at=hypothesis.created_at,
    )
