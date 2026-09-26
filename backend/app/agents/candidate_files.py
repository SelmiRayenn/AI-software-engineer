from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import redact_prompt_text
from app.agents.tools import AgentWorkspaceTools, ToolSafetyError
from app.models import AgentEvent
from app.schemas.agent_candidate_files import (
    CandidateFileRank,
    CandidateFilesInput,
    CandidateFilesSubmission,
)
from app.schemas.agent_run import AgentRunConfig

MAX_CANDIDATE_SUBMISSION_BYTES = 16_384


class AgentCandidateFilesService:
    """Validates ranked candidates against agent-visible read and retrieval evidence."""

    def __init__(
        self, db: Session, run_id: UUID, tools: AgentWorkspaceTools, config: AgentRunConfig
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.tools = tools
        self.config = config
        self.read_files: set[str] = set()
        self.retrieved_files: set[str] = set()
        latest = latest_run_candidate_submission(db, run_id)
        self.submitted = latest is not None
        self.revision = latest.revision if latest else 0
        self.editing_started = _run_has_successful_write(db, run_id)

    def begin_repair(self) -> None:
        # Only the loop calls this after failed post-patch tests with budget remaining.
        # Preserve evidence and earlier events; freeze the new ranking on the next write.
        self.editing_started = False

    def observe(self, tool_name: str, files: list[str]) -> None:
        if tool_name == "read_file":
            self.read_files.update(files)
        elif tool_name == "retrieve_relevant_files":
            self.retrieved_files.update(files)
        elif tool_name == "write_file":
            self.editing_started = True

    def check_edit(self, tool_name: str) -> None:
        if (
            self.config.require_candidate_files_before_edit
            and tool_name == "write_file"
            and not self.submitted
        ):
            raise ToolSafetyError(
                "Ranked candidate files are required before the first write_file call. "
                "Read or retrieve likely files, then call submit_candidate_files."
            )

    def submit(self, **arguments: object) -> dict:
        if self.editing_started:
            raise ToolSafetyError(
                "Candidate file rankings are frozen after the first successful write_file call."
            )
        try:
            candidate = CandidateFilesInput.model_validate(arguments)
        except ValidationError as exc:
            raise ToolSafetyError(
                "Invalid candidate files: provide ranked_files with path, reason, and "
                "confidence (low/medium/high)."
            ) from exc

        if len(candidate.ranked_files) > self.config.max_candidate_files:
            raise ToolSafetyError(
                f"Candidate file limit exceeded: at most {self.config.max_candidate_files} "
                "ranked files are allowed."
            )
        if len(candidate.model_dump_json().encode("utf-8")) > MAX_CANDIDATE_SUBMISSION_BYTES:
            raise ToolSafetyError(
                "Candidate file submission exceeds the 16384-byte limit; shorten the reasons."
            )

        ranked_files: list[CandidateFileRank] = []
        seen: set[str] = set()
        for item in candidate.ranked_files:
            path = self.tools.validate_plan_path(item.path, must_exist=True)
            if path in seen:
                raise ToolSafetyError("Candidate file paths must be unique and ranked once.")
            if path not in self.read_files and path not in self.retrieved_files:
                raise ToolSafetyError(
                    f"Candidate file '{path}' lacks read or retrieval evidence. Read the file "
                    "or include it in retrieve_relevant_files results before ranking it."
                )
            seen.add(path)
            ranked_files.append(
                CandidateFileRank(
                    path=path,
                    reason=redact_prompt_text(item.reason),
                    confidence=item.confidence,
                )
            )

        submission = CandidateFilesSubmission(
            ranked_files=ranked_files,
            revision=self.revision + 1,
            created_at=datetime.now(UTC),
        )
        if len(submission.model_dump_json().encode("utf-8")) > MAX_CANDIDATE_SUBMISSION_BYTES:
            raise ToolSafetyError(
                "Redacted candidate file submission exceeds the 16384-byte limit."
            )
        self.db.add(
            AgentEvent(
                agent_run_id=self.run_id,
                event_type="candidate_files_submitted",
                payload_json={
                    "tool_name": "submit_candidate_files",
                    **submission.model_dump(mode="json"),
                },
                created_at=submission.created_at,
            )
        )
        self.db.commit()
        self.submitted = True
        self.revision = submission.revision
        return submission.model_dump(mode="json")


def latest_run_candidate_submission(db: Session, run_id: UUID) -> CandidateFilesSubmission | None:
    event = db.scalar(
        select(AgentEvent)
        .where(
            AgentEvent.agent_run_id == run_id,
            AgentEvent.event_type == "candidate_files_submitted",
        )
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return None
    try:
        submission = CandidateFilesSubmission.model_validate(event.payload_json)
        return CandidateFilesSubmission(
            revision=submission.revision,
            ranked_files=[
                CandidateFileRank(
                    path=redact_prompt_text(item.path),
                    reason=redact_prompt_text(item.reason),
                    confidence=item.confidence,
                )
                for item in submission.ranked_files
            ],
            created_at=submission.created_at,
        )
    except (ValidationError, TypeError):
        return None


def latest_run_candidate_files(db: Session, run_id: UUID) -> list[CandidateFileRank]:
    submission = latest_run_candidate_submission(db, run_id)
    return submission.ranked_files if submission else []


def _run_has_successful_write(db: Session, run_id: UUID) -> bool:
    events = db.scalars(
        select(AgentEvent).where(
            AgentEvent.agent_run_id == run_id,
            AgentEvent.event_type == "agent_tool_call",
        )
    )
    return any(
        (event.payload_json or {}).get("tool_name") == "write_file"
        and (event.payload_json or {}).get("success") is True
        for event in events
    )
