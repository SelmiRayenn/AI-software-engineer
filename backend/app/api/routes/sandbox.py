from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from app.sandbox import DockerSandboxRunner
from app.schemas.sandbox import SandboxRunRequest, SandboxRunResponse

router = APIRouter(prefix="/sandbox", tags=["sandbox"])
runner = DockerSandboxRunner()


@router.post("/run", response_model=SandboxRunResponse)
async def run_sandbox(request: SandboxRunRequest) -> SandboxRunResponse:
    return await run_in_threadpool(runner.run, request)
