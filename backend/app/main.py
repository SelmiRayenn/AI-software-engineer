from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.agent_run_details import router as agent_run_details_router
from app.api.routes.agent_run_failures import router as agent_run_failures_router
from app.api.routes.agent_run_index import router as agent_run_index_router
from app.api.routes.agent_run_metrics import router as agent_run_metrics_router
from app.api.routes.agent_run_orchestration import router as agent_run_orchestration_router
from app.api.routes.agent_run_patches import router as agent_run_patches_router
from app.api.routes.agent_run_tests import router as agent_run_tests_router
from app.api.routes.agent_runs import router as agent_runs_router
from app.api.routes.benchmark_imports import router as benchmark_imports_router
from app.api.routes.benchmark_pack_runs import router as benchmark_pack_runs_router
from app.api.routes.benchmark_packs import router as benchmark_packs_router
from app.api.routes.benchmark_task_ingestion import evaluation_router
from app.api.routes.benchmark_task_ingestion import router as benchmark_task_ingestion_router
from app.api.routes.benchmark_tasks import router as benchmark_tasks_router
from app.api.routes.github import router as github_router
from app.api.routes.health import router as health_router
from app.api.routes.hidden_eval_tests import router as hidden_eval_tests_router
from app.api.routes.model_comparison import router as model_comparison_router
from app.api.routes.patch_quality import router as patch_quality_router
from app.api.routes.patch_reviews import router as patch_reviews_router
from app.api.routes.repositories import router as repositories_router
from app.api.routes.sandbox import router as sandbox_router
from app.core.config import settings
from app.db.init_db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.database_auto_create_tables:
        init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="API service for AI software engineering agent benchmarks.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(repositories_router, prefix="/api/v1")
    app.include_router(benchmark_tasks_router, prefix="/api/v1")
    app.include_router(agent_runs_router, prefix="/api/v1")
    app.include_router(sandbox_router)
    app.include_router(agent_run_details_router)
    app.include_router(agent_run_failures_router)
    app.include_router(agent_run_index_router)
    app.include_router(agent_run_orchestration_router)
    app.include_router(agent_run_metrics_router)
    app.include_router(agent_run_patches_router)
    app.include_router(agent_run_tests_router)
    app.include_router(model_comparison_router)
    app.include_router(patch_reviews_router)
    app.include_router(patch_quality_router)
    app.include_router(hidden_eval_tests_router)
    app.include_router(benchmark_imports_router)
    app.include_router(benchmark_packs_router)
    app.include_router(benchmark_pack_runs_router)
    app.include_router(github_router)
    app.include_router(benchmark_task_ingestion_router)
    app.include_router(evaluation_router)
    return app


app = create_app()
