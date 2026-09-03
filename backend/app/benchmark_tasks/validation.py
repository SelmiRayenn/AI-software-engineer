from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app import crud
from app.core.task_statuses import (
    ALLOWED_TASK_STATUS_TRANSITIONS,
    TASK_STATUS_READY,
    VALID_TASK_STATUSES,
)
from app.github import parse_github_repo_url
from app.models import BenchmarkTask
from app.schemas.benchmark_task import BenchmarkTaskValidationResult


class InvalidTaskStatusTransition(RuntimeError):
    pass


class BenchmarkTaskValidationFailed(RuntimeError):
    def __init__(self, result: BenchmarkTaskValidationResult) -> None:
        super().__init__("Benchmark task validation failed")
        self.result = result


def validate_benchmark_task(db: Session, task: BenchmarkTask) -> BenchmarkTaskValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    repository = task.repository or crud.get_repository(db, task.repository_id)
    if repository is None:
        errors.append("Repository record does not exist.")
    else:
        try:
            parse_github_repo_url(repository.url)
        except ValueError as exc:
            errors.append(f"repository_url is invalid: {exc}")

    if task.issue_number is None or task.issue_number <= 0:
        errors.append("issue_number must be a positive GitHub issue number.")

    pull_request_number = task.pull_request_number or _pull_request_number_from_url(
        task.linked_pr_url
    )
    if pull_request_number is None or pull_request_number <= 0:
        errors.append("pull_request_number must be present.")

    if not isinstance(task.base_commit, str) or not task.base_commit.strip():
        errors.append("base_commit must be present.")

    _validate_commands(errors, task.setup_commands, "setup_commands", require_nonempty=False)
    _validate_commands(errors, task.test_commands, "test_commands", require_nonempty=True)

    gold_patch = crud.get_gold_patch_for_task(db, task.id)
    if gold_patch is None:
        errors.append("GoldPatch must exist for evaluation.")
    else:
        if not isinstance(gold_patch.changed_files, list) or not gold_patch.changed_files:
            errors.append("Pull request must have changed files.")
        elif not all(isinstance(path, str) and path.strip() for path in gold_patch.changed_files):
            errors.append("GoldPatch.changed_files must contain non-empty file paths.")

        if not isinstance(gold_patch.test_files, list):
            errors.append("GoldPatch.test_files must be a list.")

        if not isinstance(gold_patch.patch_text, str) or not gold_patch.patch_text.strip():
            warnings.append("GoldPatch.patch_text is empty; evaluation detail will be limited.")

    if task.status not in VALID_TASK_STATUSES:
        allowed = ", ".join(sorted(VALID_TASK_STATUSES))
        errors.append(f"status must be one of: {allowed}.")

    return BenchmarkTaskValidationResult(
        task_id=task.id,
        status=task.status,
        valid=not errors,
        errors=errors,
        warnings=warnings,
    )


def mark_task_ready(db: Session, task: BenchmarkTask) -> BenchmarkTask:
    validation = validate_benchmark_task(db, task)
    if not validation.valid:
        raise BenchmarkTaskValidationFailed(validation)

    if task.status == TASK_STATUS_READY:
        return task

    if not can_transition_task_status(task.status, TASK_STATUS_READY):
        raise InvalidTaskStatusTransition(
            f"Cannot transition benchmark task from {task.status!r} to {TASK_STATUS_READY!r}."
        )

    task.status = TASK_STATUS_READY
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def can_transition_task_status(current_status: str, next_status: str) -> bool:
    if current_status == next_status:
        return True
    return next_status in ALLOWED_TASK_STATUS_TRANSITIONS.get(current_status, set())


def _validate_commands(
    errors: list[str],
    commands,
    field_name: str,
    *,
    require_nonempty: bool,
) -> None:
    if not isinstance(commands, list):
        errors.append(f"{field_name} must be a list.")
        return

    if require_nonempty and not commands:
        errors.append(f"{field_name} must include at least one command.")

    if not all(isinstance(command, str) and command.strip() for command in commands):
        errors.append(f"{field_name} must contain only non-empty strings.")


def _pull_request_number_from_url(url: str | None) -> int | None:
    if not url:
        return None

    match = re.search(r"/pull/(\d+)(?:$|[/?#])", url)
    if match is None:
        return None
    return int(match.group(1))
