from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.benchmark_imports.schemas import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    TASK_ID_PATTERN,
    BenchmarkImportError,
    BenchmarkImportResult,
    BenchmarkImportTask,
)
from app.github.test_detection import is_likely_test_file
from app.models import (
    BenchmarkImport,
    BenchmarkPack,
    BenchmarkPackTask,
    BenchmarkTask,
    GoldPatch,
    HiddenEvalTest,
    Repository,
)
from app.patches.service import PatchSafetyError, extract_patch_paths
from app.test_execution.hidden_workspace import validate_payload_path

ImportFormat = Literal["auto", "json", "jsonl"]


class BenchmarkImportService:
    """Operator-only import. Each valid row commits independently; no network or execution."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def import_file(
        self,
        path: Path,
        *,
        format: ImportFormat = "auto",
        pack_id: UUID | None = None,
    ) -> BenchmarkImportResult:
        try:
            with path.open("rb") as source:
                payload = source.read(MAX_IMPORT_BYTES + 1)
        except OSError:
            return _document_error("file_unreadable", "Import file could not be read.")
        return self.import_bytes(payload, format=format, pack_id=pack_id)

    def import_bytes(
        self,
        payload: bytes,
        *,
        format: ImportFormat = "auto",
        pack_id: UUID | None = None,
    ) -> BenchmarkImportResult:
        pack_order = self._next_pack_order(pack_id) if pack_id is not None else None
        if pack_id is not None and pack_order is None:
            return _document_error("pack_not_found", "Benchmark pack not found.")
        if len(payload) > MAX_IMPORT_BYTES:
            return _document_error("file_too_large", "Import exceeds the 20 MiB limit.")
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _document_error("invalid_encoding", "Import must contain UTF-8 text.")
        if format == "auto":
            format = "json" if text.lstrip().startswith("[") else "jsonl"
        if format not in {"json", "jsonl"}:
            return _document_error("invalid_format", "Expected json or jsonl format.")
        if not text.strip():
            return _document_error("empty_file", "Import contains no records.")
        if format == "json":
            try:
                records = json.loads(text)
            except (ValueError, RecursionError):
                return _document_error("invalid_json", "Invalid JSON array document.")
            if not isinstance(records, list):
                return _document_error("invalid_json", "JSON import must be an array.")
            rows = list(enumerate(records, start=1))
        else:
            rows = [
                (number, line)
                for number, line in enumerate(text.split("\n"), start=1)
                if line.strip()
            ]
        if len(rows) > MAX_IMPORT_ROWS:
            return _document_error("too_many_rows", "Import exceeds the 10,000 record limit.")

        result = BenchmarkImportResult()
        for row, raw in rows:
            if format == "jsonl":
                try:
                    raw = json.loads(raw)
                except (ValueError, RecursionError):
                    _add_failure(result, row, None, "invalid_json", "Invalid JSON on this line.")
                    continue
            task_id = _safe_task_id(raw)
            try:
                record = BenchmarkImportTask.model_validate(raw)
            except ValidationError as exc:
                # Never echo raw input, custom exception text, or unknown field names: they may
                # contain gold/test patches or credentials.
                fields = sorted(
                    {
                        str(e["loc"][0])
                        for e in exc.errors(include_input=False)
                        if e["loc"] and e["loc"][0] in BenchmarkImportTask.model_fields
                    }
                )
                message = "Invalid task record" + (
                    f"; check: {', '.join(fields)}." if fields else "."
                )
                _add_failure(result, row, task_id, "invalid_record", message)
                continue
            try:
                if self._find_import(record.task_id):
                    _add_duplicate(result, row, record.task_id)
                    continue
                created_id = self._create_task(record, pack_id=pack_id, pack_order=pack_order)
                self._db.commit()
            except IntegrityError:
                self._db.rollback()
                if self._find_import(record.task_id):
                    _add_duplicate(result, row, record.task_id)
                else:
                    _add_failure(
                        result,
                        row,
                        record.task_id,
                        "storage_conflict",
                        "Database constraint conflict; retry this row.",
                    )
                continue
            except (ValueError, PatchSafetyError):
                self._db.rollback()
                _add_failure(
                    result,
                    row,
                    record.task_id,
                    "invalid_patch",
                    "Patch headers must contain safe relative file paths.",
                )
                continue
            except SQLAlchemyError:
                self._db.rollback()
                _add_failure(
                    result,
                    row,
                    record.task_id,
                    "storage_error",
                    "Could not store this row; inspect database availability.",
                )
                continue
            result.imported_count += 1
            result.created_task_ids.append(created_id)
            if pack_order is not None:
                pack_order += 1
        return result

    def _find_import(self, task_id: str) -> BenchmarkImport | None:
        return self._db.scalar(select(BenchmarkImport).where(BenchmarkImport.task_id == task_id))

    def _create_task(
        self,
        record: BenchmarkImportTask,
        *,
        pack_id: UUID | None = None,
        pack_order: int | None = None,
    ) -> UUID:
        gold_files = _patch_files(record.patch) if record.patch else []
        hidden_files = _patch_files(record.test_patch) if record.test_patch else []
        assert record.repo is not None
        owner, name = record.repo.split("/")
        repository = self._db.scalar(
            select(Repository).where(
                func.lower(Repository.owner) == owner, func.lower(Repository.name) == name
            )
        )
        if repository is None:
            repository = Repository(owner=owner, name=name, url=record.repo_url)
            self._db.add(repository)
            self._db.flush()
        task = BenchmarkTask(
            repository_id=repository.id,
            issue_number=record.issue_number,
            issue_title=record.problem_statement.splitlines()[0][:500],
            issue_body=record.problem_statement,
            base_commit=record.base_commit,
            status="draft",
            setup_commands=[],
            test_commands=[],
        )
        self._db.add(task)
        self._db.flush()
        self._db.add(
            BenchmarkImport(
                task_id=record.task_id,
                benchmark_task_id=task.id,
                environment_setup_commit=record.environment_setup_commit,
                hints_text=record.hints_text,
                source_created_at=record.created_at,
            )
        )
        if record.patch is not None:
            self._db.add(
                GoldPatch(
                    benchmark_task_id=task.id,
                    patch_text=record.patch,
                    changed_files=gold_files,
                    test_files=sorted(
                        {path for path in gold_files if is_likely_test_file(path)}
                        | set(hidden_files)
                    ),
                )
            )
        if any(
            value is not None
            for value in (record.test_patch, record.fail_to_pass, record.pass_to_pass)
        ):
            self._db.add(
                HiddenEvalTest(
                    benchmark_task_id=task.id,
                    name=f"Imported evaluation: {record.task_id}"[:255],
                    commands=[],
                    patch_text=record.test_patch,
                    evaluation_metadata={
                        key: value
                        for key, value in {
                            "fail_to_pass": record.fail_to_pass,
                            "pass_to_pass": record.pass_to_pass,
                        }.items()
                        if value is not None
                    },
                    enabled=False,
                )
            )
        if pack_id is not None and pack_order is not None:
            self._db.add(
                BenchmarkPackTask(
                    benchmark_pack_id=pack_id,
                    benchmark_task_id=task.id,
                    order_index=pack_order,
                    difficulty=None,
                    tags=[],
                )
            )
        self._db.flush()
        return task.id

    def _next_pack_order(self, pack_id: UUID) -> int | None:
        if self._db.get(BenchmarkPack, pack_id) is None:
            return None
        maximum = self._db.scalar(
            select(func.max(BenchmarkPackTask.order_index)).where(
                BenchmarkPackTask.benchmark_pack_id == pack_id
            )
        )
        return 0 if maximum is None else maximum + 1


def _patch_files(patch: str) -> list[str]:
    paths = extract_patch_paths(patch)
    if patch.strip() and not paths:
        raise ValueError("Missing patch file headers.")
    normalized = set()
    for path in paths:
        if path.startswith(("a/", "b/")):
            path = path[2:]
        normalized.add(validate_payload_path(path))
    return sorted(normalized)


def _safe_task_id(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("task_id")
    if isinstance(value, str) and len(value) <= 255 and re.fullmatch(TASK_ID_PATTERN, value):
        return value
    return None


def _document_error(code: str, message: str) -> BenchmarkImportResult:
    return BenchmarkImportResult(
        failed_count=1, errors=[BenchmarkImportError(row=None, code=code, message=message)]
    )


def _add_failure(
    result: BenchmarkImportResult,
    row: int,
    task_id: str | None,
    code: str,
    message: str,
) -> None:
    result.failed_count += 1
    result.errors.append(BenchmarkImportError(row=row, task_id=task_id, code=code, message=message))


def _add_duplicate(result: BenchmarkImportResult, row: int, task_id: str) -> None:
    result.skipped_count += 1
    result.errors.append(
        BenchmarkImportError(
            row=row,
            task_id=task_id,
            code="duplicate_task_id",
            outcome="skipped",
            message="task_id already imported; existing data was left unchanged.",
        )
    )
