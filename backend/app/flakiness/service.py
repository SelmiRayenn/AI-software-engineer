from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models import BenchmarkTask, FlakinessCheck, FlakinessCheckRun
from app.schemas.flakiness import FlakinessCheckRequest
from app.schemas.sandbox import SandboxCommandResult, SandboxRunRequest, SandboxRunResponse


class SandboxRunner(Protocol):
    def run(self, request: SandboxRunRequest) -> SandboxRunResponse: ...


class FlakinessCheckError(RuntimeError):
    pass


class FlakinessCheckTaskNotFound(FlakinessCheckError):
    pass


class FlakinessCheckService:
    def __init__(self, db: Session, *, runner: SandboxRunner) -> None:
        self._db = db
        self._runner = runner

    def run(self, task_id: UUID, request: FlakinessCheckRequest) -> FlakinessCheck:
        task = self._db.get(BenchmarkTask, task_id)
        if task is None:
            raise FlakinessCheckTaskNotFound("Benchmark task not found.")
        if task.repository is None:
            raise FlakinessCheckError("Benchmark task repository is unavailable.")
        if not _configured_commands(task.test_commands):
            raise FlakinessCheckError("Benchmark task has no configured test commands.")
        if not _configured_commands(task.setup_commands, allow_empty=True):
            raise FlakinessCheckError("Benchmark task setup commands are invalid.")

        timeout = min(
            request.command_timeout_seconds or settings.sandbox_command_timeout_seconds,
            settings.sandbox_max_command_timeout_seconds,
        )
        try:
            sandbox_request = SandboxRunRequest(
                repository_url=task.repository.url,
                base_commit=task.base_commit,
                setup_commands=list(task.setup_commands),
                test_commands=list(task.test_commands),
                command_timeout_seconds=timeout,
                network_enabled=False,
                test_repetitions=request.repetitions,
                stop_on_first_test_failure=request.stop_on_first_failure,
            )
        except ValidationError as exc:
            raise FlakinessCheckError("Benchmark task commands are not safe to execute.") from exc

        response = self._runner.run(sandbox_request)
        repetition_results = _group_repetitions(
            response.test_results,
            commands_per_repetition=len(task.test_commands),
            repetitions_requested=request.repetitions,
        )
        outcomes = [passed for passed, _ in repetition_results]
        status = _status(
            response,
            outcomes,
            repetitions_requested=request.repetitions,
        )
        durations = [sum(result.duration_seconds for result in results) for _, results in repetition_results]
        check = FlakinessCheck(
            benchmark_task_id=task.id,
            repetitions_requested=request.repetitions,
            repetitions_completed=len(repetition_results),
            pass_count=sum(outcomes),
            fail_count=len(outcomes) - sum(outcomes),
            inconsistent_results=len(set(outcomes)) > 1,
            average_duration_seconds=sum(durations) / len(durations) if durations else 0.0,
            status=status,
            stop_on_first_failure=request.stop_on_first_failure,
            command_timeout_seconds=timeout,
            workspace_id=response.workspace_id,
            workspace_retained=response.workspace_retained,
            setup_results=[result.model_dump(mode="json") for result in response.setup_results],
            error_code=response.error_code,
            error_summary=_safe_error_summary(response),
        )
        self._db.add(check)
        self._db.flush()
        for number, ((passed, results), duration) in enumerate(
            zip(repetition_results, durations, strict=True), start=1
        ):
            self._db.add(
                FlakinessCheckRun(
                    flakiness_check_id=check.id,
                    repetition_number=number,
                    passed=passed,
                    duration_seconds=duration,
                    command_results=[result.model_dump(mode="json") for result in results],
                )
            )
        self._db.commit()
        return self.get(check.id)

    def list_for_task(self, task_id: UUID) -> list[FlakinessCheck]:
        if self._db.get(BenchmarkTask, task_id) is None:
            raise FlakinessCheckTaskNotFound("Benchmark task not found.")
        statement = (
            select(FlakinessCheck)
            .options(selectinload(FlakinessCheck.runs))
            .where(FlakinessCheck.benchmark_task_id == task_id)
            .order_by(FlakinessCheck.created_at.desc(), FlakinessCheck.id.desc())
        )
        return list(self._db.scalars(statement).all())

    def get(self, check_id: UUID) -> FlakinessCheck:
        statement = (
            select(FlakinessCheck)
            .options(selectinload(FlakinessCheck.runs))
            .where(FlakinessCheck.id == check_id)
        )
        check = self._db.scalar(statement)
        if check is None:
            raise FlakinessCheckError("Flakiness check could not be loaded after creation.")
        return check


def _configured_commands(commands: object, *, allow_empty: bool = False) -> bool:
    return isinstance(commands, list) and (allow_empty or bool(commands)) and all(
        isinstance(command, str) and bool(command.strip()) and len(command) <= 4000
        for command in commands
    )


def _group_repetitions(
    results: Sequence[SandboxCommandResult],
    *,
    commands_per_repetition: int,
    repetitions_requested: int,
) -> list[tuple[bool, list[SandboxCommandResult]]]:
    grouped: list[tuple[bool, list[SandboxCommandResult]]] = []
    cursor = 0
    for _ in range(repetitions_requested):
        current = list(results[cursor : cursor + commands_per_repetition])
        if not current:
            break
        cursor += len(current)
        complete = len(current) == commands_per_repetition
        grouped.append((complete and all(result.passed for result in current), current))
        if not complete:
            break
    return grouped


def _status(
    response: SandboxRunResponse,
    outcomes: list[bool],
    *,
    repetitions_requested: int,
) -> str:
    if response.status == "setup_failed":
        return "failed_setup"
    if response.status in {"clone_failed", "checkout_failed", "sandbox_error"} or not outcomes:
        return "inconclusive"
    if len(set(outcomes)) > 1:
        return "flaky"
    if len(outcomes) < repetitions_requested:
        return "inconclusive"
    return "stable"


def _safe_error_summary(response: SandboxRunResponse) -> str | None:
    if response.status == "clone_failed":
        return "Repository clone failed."
    if response.status == "checkout_failed":
        return "Base commit checkout failed."
    if response.status == "setup_failed":
        return "Configured setup command failed."
    if response.status == "sandbox_error":
        if response.error_code == "docker_unavailable":
            return "Docker is unavailable for the flakiness check."
        return "Sandbox execution failed."
    return None
