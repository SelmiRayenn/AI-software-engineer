from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.audit_logs import AgentRunAuditLogService
from app.db.session import get_db
from app.schemas.audit_log import AuditLogExport

router = APIRouter(tags=["audit logs"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/agent-runs/{run_id}/audit-log", response_model=AuditLogExport)
def get_agent_run_audit_log(run_id: UUID, response: Response, db: DbSession) -> AuditLogExport:
    audit_log = AgentRunAuditLogService(db).for_run(run_id)
    if audit_log is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    _set_integrity_headers(response, audit_log)
    return audit_log


@router.get("/patches/{patch_id}/audit-log", response_model=AuditLogExport)
def get_patch_audit_log(patch_id: UUID, response: Response, db: DbSession) -> AuditLogExport:
    audit_log = AgentRunAuditLogService(db).for_patch(patch_id)
    if audit_log is None:
        raise HTTPException(status_code=404, detail="Generated patch not found.")
    _set_integrity_headers(response, audit_log)
    return audit_log


def _set_integrity_headers(response: Response, audit_log: AuditLogExport) -> None:
    if audit_log.final_audit_hash:
        response.headers["ETag"] = f'"{audit_log.final_audit_hash}"'
        response.headers["X-Audit-Hash"] = audit_log.final_audit_hash
