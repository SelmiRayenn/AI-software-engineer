from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.patch_quality import PatchQualityNotFoundError, PatchQualityService
from app.schemas.patch_quality import PatchQualityRead

router = APIRouter(prefix="/patches", tags=["patch quality"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{patch_id}/quality", response_model=PatchQualityRead)
def get_patch_quality(patch_id: UUID, db: DbSession) -> PatchQualityRead:
    service = PatchQualityService(db)
    try:
        quality = service.get_or_create(patch_id)
    except PatchQualityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return PatchQualityRead(
        patch_id=quality.generated_patch_id,
        accepted=not quality.hard_limit_violations,
        changed_file_count=quality.changed_file_count,
        added_lines=quality.added_lines,
        removed_lines=quality.removed_lines,
        total_changed_lines=quality.total_changed_lines,
        changed_source_files=quality.changed_source_files,
        changed_test_files=quality.changed_test_files,
        changed_docs_config_files=quality.changed_docs_config_files,
        suspicious_generated_files=quality.suspicious_generated_files,
        unrelated_files_count=len(quality.unrelated_files),
        whitespace_only=quality.whitespace_only,
        touches_dependency_files=bool(quality.dependency_files),
        touches_lockfiles=bool(quality.lockfiles),
        dependency_files=quality.dependency_files,
        lockfiles=quality.lockfiles,
        warnings=quality.warnings,
        hard_limit_violations=quality.hard_limit_violations,
        max_patch_files=quality.max_patch_files,
        max_patch_changed_lines=quality.max_patch_changed_lines,
        created_at=quality.created_at,
    )
