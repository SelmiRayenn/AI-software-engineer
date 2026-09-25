from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import redact_prompt_text
from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
from app.models import AgentEvent
from app.schemas.agent_plan import AgentPlanInput, AgentPlanRead
from app.schemas.agent_run import AgentRunConfig

MAX_PLAN_BYTES = 16_384


class AgentPlanningService:
    """A run-local gate grounded only in successful, agent-visible tool observations."""

    def __init__(
        self, db: Session, run_id: UUID, tools: AgentWorkspaceTools, config: AgentRunConfig
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.tools = tools
        self.config = config
        self.evidence: set[str] = set()
        self.revision = 0
        self.accepted = False

    def observe(self, tool_name: str, files: list[str]) -> None:
        if tool_name in {"read_file", "search_code", "retrieve_relevant_files"}:
            self.evidence.update(files)

    def check_edit(self, tool_name: str) -> None:
        if (
            self.config.require_plan_before_edit
            and tool_name in {"write_file", "submit_patch"}
            and not self.accepted
        ):
            raise ToolSafetyError(
                "An accepted plan is required before editing or submitting a patch. "
                "Inspect files, then call submit_plan with evidence and a test/rollback strategy."
            )

    def submit(self, **arguments: object) -> dict:
        self.revision += 1
        self.accepted = False
        plan = None
        reason = None
        try:
            if self.revision > 1 + self.config.max_plan_revisions:
                raise ToolSafetyError("Plan revision limit reached; no further plans are allowed.")
            try:
                candidate = AgentPlanInput.model_validate(arguments)
            except ValidationError as exc:
                # Never echo invalid input (including unknown trusted-data fields).
                raise ToolSafetyError(
                    "Invalid plan: provide all six fields with non-empty text (max 2000 "
                    "characters each) and at most 50 paths (max 300 characters each)."
                ) from exc
            if len(candidate.model_dump_json().encode("utf-8")) > MAX_PLAN_BYTES:
                raise ToolSafetyError(
                    "Plan exceeds the 16384-byte limit; submit a shorter revision."
                )
            candidate.files_inspected = list(
                dict.fromkeys(
                    self.tools.validate_plan_path(path, must_exist=True)
                    for path in candidate.files_inspected
                )
            )
            candidate.files_likely_to_modify = list(
                dict.fromkeys(
                    self.tools.validate_plan_path(path, must_exist=False)
                    for path in candidate.files_likely_to_modify
                )
            )
            # Keep only a validated, redacted shape in storage and tool feedback.
            redacted = _redacted_plan(candidate)
            if len(redacted.model_dump_json().encode("utf-8")) > MAX_PLAN_BYTES:
                raise ToolSafetyError("Redacted plan exceeds the 16384-byte limit; shorten it.")
            plan = redacted
            if not set(candidate.files_inspected).issubset(self.evidence):
                raise ToolSafetyError(
                    "Plan cites unobserved files. Retrieve, search, or read those files, then revise."
                )
            if len(candidate.files_inspected) < self.config.plan_min_evidence_files:
                raise ToolSafetyError(
                    f"Plan requires at least {self.config.plan_min_evidence_files} distinct "
                    "observed evidence files. Inspect files and submit a revised plan."
                )
        except (ToolSafetyError, OSError, ValueError) as error:
            # Path errors may contain host paths; use a safe message for those errors.
            reason = str(error) if isinstance(error, ToolSafetyError) else "Invalid plan file path."

        self.accepted = reason is None
        review = AgentPlanRead(
            revision=self.revision,
            status="accepted" if self.accepted else "rejected",
            accepted=self.accepted,
            reason=reason,
            plan=plan,
            created_at=datetime.now(UTC),
        )
        self.db.add(
            AgentEvent(
                agent_run_id=self.run_id,
                event_type="plan_submitted",
                payload_json={"tool_name": "submit_plan", **review.model_dump(mode="json")},
                created_at=review.created_at,
            )
        )
        self.db.commit()
        if reason:
            raise ToolSafetyError(reason)
        return review.model_dump(mode="json")


def latest_run_plan(db: Session, run_id: UUID) -> AgentPlanRead | None:
    event = db.scalar(
        select(AgentEvent)
        .where(AgentEvent.agent_run_id == run_id, AgentEvent.event_type == "plan_submitted")
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return None
    try:
        review = AgentPlanRead.model_validate(event.payload_json)
        if review.plan:
            review.plan = _redacted_plan(review.plan)
        if review.reason:
            review.reason = redact_prompt_text(review.reason)[:2000]
        return review
    except (ValidationError, TypeError):
        return None


def _redacted_plan(plan: AgentPlanInput) -> AgentPlanInput:
    return AgentPlanInput.model_validate(
        {
            key: [redact_prompt_text(path) for path in value]
            if isinstance(value, list)
            else redact_prompt_text(value)
            for key, value in plan.model_dump().items()
        }
    )
