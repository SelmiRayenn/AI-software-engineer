from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.analytics.service import AnalyticsFilters, AnalyticsService
from app.api.routes.agent_run_orchestration import DbSession
from app.schemas.analytics import (
    AnalyticsSummary,
    FileLocalizationAnalytics,
    ModelLeaderboardRow,
    PackAnalytics,
    RepositoryAnalytics,
    ToolUsageAnalytics,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


def get_analytics_filters(
    benchmark_pack_id: Annotated[UUID | None, Query()] = None,
    repository_id: Annotated[UUID | None, Query()] = None,
    model_provider: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    model_name: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> AnalyticsFilters:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be before or equal to date_to")
    return AnalyticsFilters(
        benchmark_pack_id=benchmark_pack_id,
        repository_id=repository_id,
        model_provider=model_provider,
        model_name=model_name,
        date_from=date_from,
        date_to=date_to,
    )


AnalyticsFiltersDep = Annotated[AnalyticsFilters, Depends(get_analytics_filters)]


@router.get("/summary", response_model=AnalyticsSummary)
def get_analytics_summary(filters: AnalyticsFiltersDep, db: DbSession) -> AnalyticsSummary:
    return AnalyticsService(db).summary(filters)


@router.get("/by-repository", response_model=list[RepositoryAnalytics])
def get_analytics_by_repository(
    filters: AnalyticsFiltersDep, db: DbSession
) -> list[RepositoryAnalytics]:
    return AnalyticsService(db).by_repository(filters)


@router.get("/by-pack", response_model=list[PackAnalytics])
def get_analytics_by_pack(filters: AnalyticsFiltersDep, db: DbSession) -> list[PackAnalytics]:
    return AnalyticsService(db).by_pack(filters)


@router.get("/tool-usage", response_model=ToolUsageAnalytics)
def get_tool_usage(filters: AnalyticsFiltersDep, db: DbSession) -> ToolUsageAnalytics:
    return AnalyticsService(db).tool_usage(filters)


@router.get("/file-localization", response_model=FileLocalizationAnalytics)
def get_file_localization(
    filters: AnalyticsFiltersDep, db: DbSession
) -> FileLocalizationAnalytics:
    return AnalyticsService(db).file_localization(filters)


@router.get("/model-leaderboard", response_model=list[ModelLeaderboardRow])
def get_model_leaderboard(
    db: DbSession,
    benchmark_pack_id: Annotated[UUID | None, Query()] = None,
    repository_id: Annotated[UUID | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    min_runs: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
) -> list[ModelLeaderboardRow]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be before or equal to date_to")
    filters = AnalyticsFilters(
        benchmark_pack_id=benchmark_pack_id,
        repository_id=repository_id,
        date_from=date_from,
        date_to=date_to,
    )
    return AnalyticsService(db).model_leaderboard(filters, min_runs=min_runs)
