from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.run_statuses import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
)
from app.core.test_phases import (
    TEST_PHASE_BASELINE,
    TEST_PHASE_HIDDEN_EVAL,
    TEST_PHASE_POST_PATCH,
    TEST_PHASE_SETUP,
)
from app.models import AgentEvent, AgentRun, GeneratedPatch, TestResult
from app.patches import PatchError, PatchService

TEST_EXECUTION_ALLOWED_STATUSES = {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}


class TestExecutionError(RuntimeError):
    pass


class TestExecutionWorkspaceError(TestExecutionError):
    pass


class TestExecutionSafetyError(TestExecutionError):
    pass


@dataclass(frozen=True)
class TestExecutionPhaseResult:
    agent_run_id: UUID
    phase: str
    passed: bool
    setup_results: list[TestResult] = field(default_factory=list)
    test_results: list[TestResult] = field(default_factory=list)
    patch_status: str | None = None
    error_message: str | None = None


class TestExecutionService:
    def __init__(
        self,
        *,
        db: Session,
        agent_run_id: UUID,
        workspace_path: str | Path | None = None,
        command_timeout_seconds: int | None = None,
        max_log_bytes: int | None = None,
    ) -> None:
        self._db = db
        self._agent_run_id = agent_run_id
        self._agent_run = db.get(AgentRun, agent_run_id)
        if self._agent_run is None:
            raise TestExecutionError("Agent run not found.")
        if self._agent_run.benchmark_task is None:
            raise TestExecutionError("Agent run benchmark task not found.")

        raw_workspace_path = workspace_path or self._agent_run.workspace_path
        if raw_workspace_path is None:
            raise TestExecutionWorkspaceError("Agent run does not have an active workspace path.")

        try:
            self._workspace_path = Path(raw_workspace_path).resolve(strict=True)
        except OSError as exc:
            raise TestExecutionWorkspaceError("Agent run workspace path does not exist.") from exc

        if not self._workspace_path.is_dir():
            raise TestExecutionWorkspaceError("Agent run workspace path must be a directory.")

        self._command_timeout_seconds = min(
            command_timeout_seconds or settings.sandbox_command_timeout_seconds,
            settings.sandbox_max_command_timeout_seconds,
        )
        self._max_log_bytes = max_log_bytes or settings.sandbox_max_log_bytes

    def list_results(self) -> list[TestResult]:
        statement = (
            select(TestResult)
            .where(TestResult.agent_run_id == self._agent_run_id)
            .order_by(TestResult.created_at.asc())
        )
        return list(self._db.scalars(statement).all())

    def run_setup_commands(self) -> TestExecutionPhaseResult:
        self._ensure_execution_allowed()
        self._mark_running_if_needed()
        results = self._run_configured_commands(
            phase=TEST_PHASE_SETUP,
            commands=self._agent_run.benchmark_task.setup_commands,
            allowed_commands=self._agent_run.benchmark_task.setup_commands,
        )
        passed = _all_passed(results)
        if not passed:
            self._mark_failed()

        self._log_phase_completed(
            phase=TEST_PHASE_SETUP,
            passed=passed,
            result_count=len(results),
            patch_status=None,
        )
        return TestExecutionPhaseResult(
            agent_run_id=self._agent_run_id,
            phase=TEST_PHASE_SETUP,
            passed=passed,
            setup_results=results,
            error_message=None if passed else "Setup command failed.",
        )

    def run_baseline_tests(self, *, run_setup: bool = True) -> TestExecutionPhaseResult:
        self._ensure_execution_allowed()
        self._mark_running_if_needed()
        setup_results: list[TestResult] = []
        if run_setup:
            setup_results = self._run_configured_commands(
                phase=TEST_PHASE_SETUP,
                commands=self._agent_run.benchmark_task.setup_commands,
                allowed_commands=self._agent_run.benchmark_task.setup_commands,
            )
            if not _all_passed(setup_results):
                self._mark_failed()
                self._log_phase_completed(
                    phase=TEST_PHASE_BASELINE,
                    passed=False,
                    result_count=len(setup_results),
                    patch_status=None,
                )
                return TestExecutionPhaseResult(
                    agent_run_id=self._agent_run_id,
                    phase=TEST_PHASE_BASELINE,
                    passed=False,
                    setup_results=setup_results,
                    error_message="Setup command failed.",
                )

        test_results = self._run_configured_commands(
            phase=TEST_PHASE_BASELINE,
            commands=self._agent_run.benchmark_task.test_commands,
            allowed_commands=self._agent_run.benchmark_task.test_commands,
        )
        passed = _all_passed(test_results)
        self._log_phase_completed(
            phase=TEST_PHASE_BASELINE,
            passed=passed,
            result_count=len(setup_results) + len(test_results),
            patch_status=None,
        )
        return TestExecutionPhaseResult(
            agent_run_id=self._agent_run_id,
            phase=TEST_PHASE_BASELINE,
            passed=passed,
            setup_results=setup_results,
            test_results=test_results,
            error_message=None if passed else "Baseline test command failed.",
        )

    def run_post_patch_tests(self) -> TestExecutionPhaseResult:
        self._ensure_execution_allowed()
        self._mark_running_if_needed()
        patch_status = self._apply_generated_patch_if_present()
        test_results = self._run_configured_commands(
            phase=TEST_PHASE_POST_PATCH,
            commands=self._agent_run.benchmark_task.test_commands,
            allowed_commands=self._agent_run.benchmark_task.test_commands,
        )
        passed = _all_passed(test_results)
        if passed:
            self._mark_completed()
        else:
            self._mark_failed()

        self._log_phase_completed(
            phase=TEST_PHASE_POST_PATCH,
            passed=passed,
            result_count=len(test_results),
            patch_status=patch_status,
        )
        return TestExecutionPhaseResult(
            agent_run_id=self._agent_run_id,
            phase=TEST_PHASE_POST_PATCH,
            passed=passed,
            test_results=test_results,
            patch_status=patch_status,
            error_message=None if passed else "Post-patch test command failed.",
        )

    def run_test_command(self, *, phase: str, command: str) -> TestResult:
        self._ensure_execution_allowed()
        if phase not in {TEST_PHASE_BASELINE, TEST_PHASE_POST_PATCH, TEST_PHASE_HIDDEN_EVAL}:
            raise TestExecutionSafetyError("Only test phases can execute test commands.")
        self._ensure_command_allowed(command, self._agent_run.benchmark_task.test_commands)
        self._mark_running_if_needed()
        return self._run_command(phase=phase, command=command)

    def _run_configured_commands(
        self,
        *,
        phase: str,
        commands: list[str],
        allowed_commands: list[str],
    ) -> list[TestResult]:
        results: list[TestResult] = []
        for command in commands:
            self._ensure_command_allowed(command, allowed_commands)
            results.append(self._run_command(phase=phase, command=command))
        return results

    def _run_command(self, *, phase: str, command: str) -> TestResult:
        started = time.perf_counter()
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                cwd=self._workspace_path,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self._command_timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
        except OSError as exc:
            exit_code = 1
            stdout = ""
            stderr = str(exc)

        if timed_out:
            timeout_message = f"Command exceeded timeout of {self._command_timeout_seconds} seconds."
            stderr = f"{stderr}\n{timeout_message}".strip()

        test_result = TestResult(
            agent_run_id=self._agent_run_id,
            phase=phase,
            command=command,
            passed=exit_code == 0 and not timed_out,
            exit_code=exit_code,
            stdout=self._limit_log(stdout),
            stderr=self._limit_log(stderr),
            duration_seconds=time.perf_counter() - started,
        )
        self._db.add(test_result)
        self._db.commit()
        self._db.refresh(test_result)
        return test_result

    def _apply_generated_patch_if_present(self) -> str:
        generated_patch = self._db.scalar(
            select(GeneratedPatch).where(GeneratedPatch.agent_run_id == self._agent_run_id)
        )
        if generated_patch is None:
            return "none"
        if not generated_patch.patch_text.strip():
            return "empty"

        try:
            applied = PatchService(
                db=self._db,
                agent_run_id=self._agent_run_id,
                workspace_path=self._workspace_path,
            ).ensure_patch_applied(generated_patch.patch_text)
        except PatchError as exc:
            self._mark_failed()
            raise TestExecutionError(f"Generated patch could not be applied: {exc}") from exc

        if applied.already_applied:
            return "already_applied"
        if applied.applied:
            return "applied"
        return "none"

    def _ensure_execution_allowed(self) -> None:
        if self._agent_run.status not in TEST_EXECUTION_ALLOWED_STATUSES:
            disallowed = ", ".join(
                sorted({RUN_STATUS_CANCELLED, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED})
            )
            raise TestExecutionSafetyError(
                f"Test execution is blocked for immutable run states: {disallowed}."
            )

    def _ensure_command_allowed(self, command: str, allowed_commands: list[str]) -> None:
        if command not in set(allowed_commands):
            raise TestExecutionSafetyError("Command is not configured for this benchmark task.")

    def _mark_running_if_needed(self) -> None:
        if self._agent_run.status == RUN_STATUS_RUNNING:
            return
        self._agent_run.status = RUN_STATUS_RUNNING
        self._db.add(self._agent_run)
        self._db.commit()
        self._db.refresh(self._agent_run)

    def _mark_completed(self) -> None:
        self._agent_run.status = RUN_STATUS_COMPLETED
        self._agent_run.completed_at = datetime.now(UTC)
        self._db.add(self._agent_run)
        self._db.commit()
        self._db.refresh(self._agent_run)

    def _mark_failed(self) -> None:
        self._agent_run.status = RUN_STATUS_FAILED
        self._agent_run.completed_at = datetime.now(UTC)
        self._db.add(self._agent_run)
        self._db.commit()
        self._db.refresh(self._agent_run)

    def _log_phase_completed(
        self,
        *,
        phase: str,
        passed: bool,
        result_count: int,
        patch_status: str | None,
    ) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=self._agent_run_id,
                event_type="test_phase_completed",
                payload_json={
                    "phase": phase,
                    "passed": passed,
                    "result_count": result_count,
                    "patch_status": patch_status,
                },
            )
        )
        self._db.commit()

    def _limit_log(self, value: str | None) -> str:
        if not value:
            return ""
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= self._max_log_bytes:
            return value
        truncated = encoded[-self._max_log_bytes :].decode("utf-8", errors="replace")
        return f"[truncated to last {self._max_log_bytes} bytes]\n{truncated}"


def _all_passed(results: list[TestResult]) -> bool:
    return all(result.passed for result in results)


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
