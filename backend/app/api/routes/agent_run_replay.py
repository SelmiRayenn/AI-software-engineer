from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.replay_snapshots import AgentRunReplaySnapshotService
from app.schemas.replay_snapshot import AgentRunReplaySnapshot

router = APIRouter(prefix="/agent-runs", tags=["agent run replay"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{run_id}/replay-snapshot", response_model=None)
def get_agent_run_replay_snapshot(
    run_id: UUID,
    db: DbSession,
    format: Literal["json", "md"] = Query(default="json"),
) -> AgentRunReplaySnapshot | Response:
    service = AgentRunReplaySnapshotService(db)
    snapshot = service.build(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    if format == "md":
        return Response(
            content=service.render_markdown(snapshot),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="agent-run-{run_id}-replay.md"'},
        )
    return snapshot
