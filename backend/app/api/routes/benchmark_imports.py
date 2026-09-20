from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.benchmark_imports import BenchmarkImportService
from app.benchmark_imports.schemas import MAX_IMPORT_BYTES, BenchmarkImportResult
from app.core.trusted import require_trusted_operator
from app.db.session import get_db

router = APIRouter(tags=["benchmark imports"])


@router.post(
    "/benchmark-imports",
    response_model=BenchmarkImportResult,
    dependencies=[Depends(require_trusted_operator)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "array", "items": {"type": "object"}}},
                "application/x-ndjson": {"schema": {"type": "string"}},
            },
        }
    },
)
async def import_benchmark_tasks(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    pack_id: UUID | None = None,
) -> BenchmarkImportResult:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    formats = {"application/json": "json", "application/x-ndjson": "jsonl"}
    if content_type not in formats:
        raise HTTPException(415, "Use application/json or application/x-ndjson.")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "Import exceeds the 20 MiB limit.")
        payload.extend(chunk)
    return await run_in_threadpool(
        BenchmarkImportService(db).import_bytes,
        bytes(payload),
        format=formats[content_type],
        pack_id=pack_id,
    )
