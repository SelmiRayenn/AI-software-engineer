from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.benchmark_validation import (
    BenchmarkPackValidationNotFound,
    BenchmarkTaskValidationNotFound,
    BenchmarkValidationReportService,
)
from app.db.session import get_db
from app.schemas.benchmark_validation import (
    BenchmarkPackValidationReport,
    BenchmarkTaskValidationReport,
)

router = APIRouter(tags=["benchmark validation"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get(
    "/benchmark-tasks/{task_id}/validation-report",
    response_model=BenchmarkTaskValidationReport,
)
def get_benchmark_task_validation_report(
    task_id: UUID, db: DbSession
) -> BenchmarkTaskValidationReport:
    try:
        return BenchmarkValidationReportService(db).task_report(task_id)
    except BenchmarkTaskValidationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/benchmark-packs/{pack_id}/validation-report",
    response_model=BenchmarkPackValidationReport,
)
def get_benchmark_pack_validation_report(
    pack_id: UUID, db: DbSession
) -> BenchmarkPackValidationReport:
    try:
        return BenchmarkValidationReportService(db).pack_report(pack_id)
    except BenchmarkPackValidationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
