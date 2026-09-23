from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.routes.agent_run_orchestration import DbSession
from app.run_reports import PublicDemoSnapshotService
from app.schemas.public_demo_report import PublicDemoSnapshot

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/public-demo-snapshot", response_model=None)
def get_public_demo_snapshot(
    db: DbSession,
    response: Response,
    benchmark_pack_id: Annotated[UUID | None, Query()] = None,
    model_provider: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    model_name: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    output_format: Annotated[Literal["json", "md"], Query(alias="format")] = "json",
) -> PublicDemoSnapshot | Response:
    service = PublicDemoSnapshotService(db)
    snapshot = service.build(
        benchmark_pack_id=benchmark_pack_id,
        model_provider=model_provider.strip() if model_provider else None,
        model_name=model_name.strip() if model_name else None,
        limit=limit,
    )
    if output_format == "md":
        markdown = service.render_markdown(snapshot)
        return Response(
            content=markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="public-demo-snapshot.md"'},
        )
    response.headers["Content-Disposition"] = 'attachment; filename="public-demo-snapshot.json"'
    return snapshot
