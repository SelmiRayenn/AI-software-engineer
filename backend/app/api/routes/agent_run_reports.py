from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.run_reports import AgentRunReportService
from app.schemas.run_report import AgentRunReport

router = APIRouter(prefix="/agent-runs", tags=["agent run reports"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}/report.json", response_model=AgentRunReport)
def get_agent_run_json_report(
    run_id: UUID,
    response: Response,
    db: DbSession,
) -> AgentRunReport:
    report = AgentRunReportService(db).build(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="agent-run-{run_id}-report.json"'
    )
    return report


@router.get("/{run_id}/report.md")
def get_agent_run_markdown_report(run_id: UUID, db: DbSession) -> Response:
    service = AgentRunReportService(db)
    report = service.build(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return Response(
        content=service.render_markdown(report),
        media_type="text/markdown",
        headers={"Content-Disposition": (f'attachment; filename="agent-run-{run_id}-report.md"')},
    )
