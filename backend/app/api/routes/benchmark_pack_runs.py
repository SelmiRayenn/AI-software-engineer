from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.agents.orchestrator import AgentRunOrchestrator
from app.api.routes.agent_run_orchestration import (
    DbSession,
    ModelProviderFactoryDep,
    WorkspacePreparerDep,
)
from app.benchmark_packs.runs import BenchmarkPackRunNotFound, BenchmarkPackRunService
from app.benchmark_packs.service import BenchmarkPackConflict, BenchmarkPackNotFound
from app.core.trusted import TRUSTED_OPERATOR_HEADER, verify_trusted_operator_token
from app.run_reports import BenchmarkPackRunReportService
from app.schemas.benchmark_pack_run import BenchmarkPackRunRead, BenchmarkPackRunRequest
from app.schemas.pack_run_report import BenchmarkPackRunReport

router = APIRouter(tags=["benchmark pack runs"])


@router.post(
    "/benchmark-packs/{pack_id}/runs",
    response_model=BenchmarkPackRunRead,
    status_code=status.HTTP_201_CREATED,
)
def start_benchmark_pack_run(
    pack_id: UUID,
    request: BenchmarkPackRunRequest,
    db: DbSession,
    workspace_preparer: WorkspacePreparerDep,
    provider_factory: ModelProviderFactoryDep,
    operator_token: Annotated[str | None, Header(alias=TRUSTED_OPERATOR_HEADER)] = None,
) -> BenchmarkPackRunRead:
    if request.include_hidden_tests:
        verify_trusted_operator_token(operator_token)
    service = BenchmarkPackRunService(
        db,
        provider_factory=provider_factory,
        orchestrator=AgentRunOrchestrator(
            db=db, workspace_preparer=workspace_preparer, provider_factory=provider_factory
        ),
    )
    try:
        return service.start(pack_id, request, trusted_operator=request.include_hidden_tests)
    except BenchmarkPackNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BenchmarkPackConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/benchmark-pack-runs/{pack_run_id}", response_model=BenchmarkPackRunRead)
def get_benchmark_pack_run(pack_run_id: UUID, db: DbSession) -> BenchmarkPackRunRead:
    try:
        return BenchmarkPackRunService(db).get(pack_run_id)
    except BenchmarkPackRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/benchmark-pack-runs/{pack_run_id}/report.json",
    response_model=BenchmarkPackRunReport,
)
def get_benchmark_pack_run_json_report(
    pack_run_id: UUID,
    response: Response,
    db: DbSession,
) -> BenchmarkPackRunReport:
    report = BenchmarkPackRunReportService(db).build(pack_run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Benchmark pack run not found.")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="benchmark-pack-run-{pack_run_id}-report.json"'
    )
    return report


@router.get("/benchmark-pack-runs/{pack_run_id}/report.md")
def get_benchmark_pack_run_markdown_report(pack_run_id: UUID, db: DbSession) -> Response:
    service = BenchmarkPackRunReportService(db)
    report = service.build(pack_run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Benchmark pack run not found.")
    return Response(
        content=service.render_markdown(report),
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="benchmark-pack-run-{pack_run_id}-report.md"'
            )
        },
    )
