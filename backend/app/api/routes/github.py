from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.trusted import require_trusted_operator
from app.github import GitHubClientError, GitHubService
from app.schemas.github import (
    GitHubIssuePreviewRequest,
    GitHubIssuePreviewResponse,
    GitHubPullRequestPreviewRequest,
    GitHubPullRequestPreviewResponse,
    GitHubTrustedPullRequestPreviewResponse,
)

router = APIRouter(prefix="/github", tags=["github"])
TrustedOperator = Annotated[None, Depends(require_trusted_operator)]


@router.post("/preview-issue", response_model=GitHubIssuePreviewResponse)
async def preview_issue(request: GitHubIssuePreviewRequest) -> GitHubIssuePreviewResponse:
    service = GitHubService()
    try:
        return await run_in_threadpool(
            service.preview_issue,
            request.repository_url,
            request.issue_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail={
                "message": str(exc),
                "github_status_code": exc.status_code,
                "github_response": exc.response_json,
            },
        ) from exc


@router.post("/preview-pr", response_model=GitHubPullRequestPreviewResponse)
async def preview_pull_request(
    request: GitHubPullRequestPreviewRequest,
) -> GitHubPullRequestPreviewResponse:
    service = GitHubService()
    try:
        return await run_in_threadpool(
            service.preview_pull_request,
            request.repository_url,
            request.pull_request_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail={
                "message": str(exc),
                "github_status_code": exc.status_code,
                "github_response": exc.response_json,
            },
        ) from exc


@router.post("/preview-pr/trusted", response_model=GitHubTrustedPullRequestPreviewResponse)
async def preview_pull_request_trusted(
    request: GitHubPullRequestPreviewRequest,
    _: TrustedOperator,
) -> GitHubTrustedPullRequestPreviewResponse:
    service = GitHubService()
    try:
        return await run_in_threadpool(
            service.preview_pull_request_trusted,
            request.repository_url,
            request.pull_request_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail={
                "message": str(exc),
                "github_status_code": exc.status_code,
                "github_response": exc.response_json,
            },
        ) from exc
