from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.review_decisions import (
    REVIEW_DECISION_APPROVED,
    REVIEW_DECISION_REJECTED,
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    REVIEW_STATUS_REJECTED,
)
from app.models import AgentEvent, GeneratedPatch, HumanReview


class PatchApprovalError(RuntimeError):
    pass


class PatchApprovalDuplicateReviewError(PatchApprovalError):
    pass


class PatchApprovalExportBlockedError(PatchApprovalError):
    pass


@dataclass(frozen=True)
class PatchReviewStatus:
    generated_patch_id: UUID
    review_status: str
    review: HumanReview | None
    export_eligible: bool


class PatchApprovalService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def review_status(self, generated_patch_id: UUID) -> PatchReviewStatus:
        generated_patch = self._get_generated_patch(generated_patch_id)
        review = self._get_review(generated_patch.id)
        status = _review_status(review)
        return PatchReviewStatus(
            generated_patch_id=generated_patch.id,
            review_status=status,
            review=review,
            export_eligible=status == REVIEW_STATUS_APPROVED,
        )

    def approve_patch(
        self,
        generated_patch_id: UUID,
        *,
        reviewer_name: str,
        review_notes: str | None = None,
        update_existing: bool = False,
    ) -> PatchReviewStatus:
        return self._record_review(
            generated_patch_id,
            decision=REVIEW_DECISION_APPROVED,
            reviewer_name=reviewer_name,
            review_notes=review_notes,
            update_existing=update_existing,
        )

    def reject_patch(
        self,
        generated_patch_id: UUID,
        *,
        reviewer_name: str,
        review_notes: str,
        update_existing: bool = False,
    ) -> PatchReviewStatus:
        return self._record_review(
            generated_patch_id,
            decision=REVIEW_DECISION_REJECTED,
            reviewer_name=reviewer_name,
            review_notes=review_notes,
            update_existing=update_existing,
        )

    def ensure_patch_exportable(self, generated_patch_id: UUID) -> GeneratedPatch:
        generated_patch = self._get_generated_patch(generated_patch_id)
        review = self._get_review(generated_patch.id)
        status = _review_status(review)
        if status != REVIEW_STATUS_APPROVED:
            raise PatchApprovalExportBlockedError(
                "Generated patch cannot be exported until a human reviewer approves it."
            )
        return generated_patch

    def _record_review(
        self,
        generated_patch_id: UUID,
        *,
        decision: str,
        reviewer_name: str,
        review_notes: str | None,
        update_existing: bool,
    ) -> PatchReviewStatus:
        generated_patch = self._get_generated_patch(generated_patch_id)
        existing_review = self._get_review(generated_patch.id)
        if existing_review is not None and not update_existing:
            raise PatchApprovalDuplicateReviewError(
                "Generated patch already has a human review. Set update_existing=true to replace it."
            )

        now = datetime.now(UTC)
        if existing_review is None:
            review = HumanReview(
                generated_patch_id=generated_patch.id,
                decision=decision,
                reviewer_name=reviewer_name,
                review_notes=review_notes,
                reviewed_at=now,
            )
            self._db.add(review)
        else:
            review = existing_review
            review.decision = decision
            review.reviewer_name = reviewer_name
            review.review_notes = review_notes
            review.reviewed_at = now
            self._db.add(review)

        self._db.commit()
        self._db.refresh(review)
        self._log_review_event(generated_patch, review, updated=existing_review is not None)
        return self.review_status(generated_patch.id)

    def _get_generated_patch(self, generated_patch_id: UUID) -> GeneratedPatch:
        generated_patch = self._db.get(GeneratedPatch, generated_patch_id)
        if generated_patch is None:
            raise PatchApprovalError("Generated patch not found.")
        return generated_patch

    def _get_review(self, generated_patch_id: UUID) -> HumanReview | None:
        return self._db.scalar(
            select(HumanReview).where(HumanReview.generated_patch_id == generated_patch_id)
        )

    def _log_review_event(
        self,
        generated_patch: GeneratedPatch,
        review: HumanReview,
        *,
        updated: bool,
    ) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=generated_patch.agent_run_id,
                event_type="human_review_recorded",
                payload_json={
                    "generated_patch_id": str(generated_patch.id),
                    "human_review_id": str(review.id),
                    "decision": review.decision,
                    "reviewer_name": review.reviewer_name,
                    "updated": updated,
                },
            )
        )
        self._db.commit()


def _review_status(review: HumanReview | None) -> str:
    if review is None:
        return REVIEW_STATUS_PENDING
    if review.decision == REVIEW_DECISION_APPROVED:
        return REVIEW_STATUS_APPROVED
    if review.decision == REVIEW_DECISION_REJECTED:
        return REVIEW_STATUS_REJECTED
    return REVIEW_STATUS_PENDING
