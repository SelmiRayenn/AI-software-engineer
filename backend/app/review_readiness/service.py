from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentEvent, AgentRun, EvaluationMetric


@dataclass(frozen=True)
class ReviewReadinessInputs:
    patch_applied: bool
    visible_tests_passed: bool
    hidden_tests_passed: bool | None
    hidden_tests_run_count: int
    lint_passed: bool | None
    format_check_passed: bool | None
    minimization_score: float | None
    minimization_warnings: list[str]
    patch_quality_warnings: list[str]
    hard_limit_violations: list[str]
    unrelated_files_count: int
    guardrail_override_used: bool
    failure_category: str | None
    human_review_status: str
    block_on_lint_failure: bool = False
    block_on_format_check_failure: bool = False


@dataclass(frozen=True)
class ReviewReadinessResult:
    code_quality_score: float
    review_ready: bool
    review_blockers: list[str]
    review_warnings: list[str]


def calculate_review_readiness(inputs: ReviewReadinessInputs) -> ReviewReadinessResult:
    blockers: list[str] = []
    warnings: list[str] = []
    score = 1.0

    if not inputs.patch_applied:
        blockers.append("patch_not_applied")
        score -= 0.30
    if not inputs.visible_tests_passed:
        blockers.append("visible_tests_failed")
        score -= 0.35

    if inputs.hidden_tests_run_count:
        if inputs.hidden_tests_passed is not True:
            blockers.append("hidden_tests_failed")
            score -= 0.20
    else:
        warnings.append("hidden_tests_not_run")
        score -= 0.05

    score = _quality_check(
        passed=inputs.lint_passed,
        blocker=inputs.block_on_lint_failure,
        code="lint_failed",
        penalty=0.10,
        score=score,
        blockers=blockers,
        warnings=warnings,
    )
    score = _quality_check(
        passed=inputs.format_check_passed,
        blocker=inputs.block_on_format_check_failure,
        code="format_check_failed",
        penalty=0.05,
        score=score,
        blockers=blockers,
        warnings=warnings,
    )

    if inputs.hard_limit_violations:
        blockers.append("patch_quality_rejected")
        score -= 0.30
    if inputs.patch_quality_warnings:
        warnings.append("patch_quality_warning")
        score -= min(0.10, 0.02 * len(inputs.patch_quality_warnings))

    warnings.extend(inputs.minimization_warnings)
    if inputs.minimization_score is None:
        warnings.append("patch_quality_unavailable")
        score -= 0.05
    else:
        score -= (1.0 - max(0.0, min(1.0, inputs.minimization_score))) * 0.25

    if inputs.unrelated_files_count:
        warnings.append("unrelated_files_modified")
        score -= min(0.20, inputs.unrelated_files_count * 0.05)
    if inputs.guardrail_override_used:
        warnings.append("trusted_guardrail_override_used")
        score -= 0.10
    if inputs.failure_category:
        blockers.append("run_failure")
        score -= 0.20
    if inputs.human_review_status == "rejected":
        blockers.append("human_review_rejected")
        score -= 0.20

    blockers = _deduplicate(blockers)
    warnings = _deduplicate(warnings)
    return ReviewReadinessResult(
        code_quality_score=round(max(0.0, min(1.0, score)), 4),
        review_ready=not blockers,
        review_blockers=blockers,
        review_warnings=warnings,
    )


class ReviewReadinessService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def refresh(self, agent_run_id: UUID) -> EvaluationMetric | None:
        run = self._db.get(AgentRun, agent_run_id)
        if run is None or run.evaluation_metric is None:
            return None

        metric = run.evaluation_metric
        patch = run.generated_patch
        quality = patch.quality if patch else None
        flags = self._stored_flags(run.id)
        result = calculate_review_readiness(
            ReviewReadinessInputs(
                patch_applied=metric.patch_applied,
                visible_tests_passed=metric.post_patch_tests_passed,
                hidden_tests_passed=metric.hidden_tests_passed,
                hidden_tests_run_count=metric.hidden_tests_run_count,
                lint_passed=metric.lint_passed,
                format_check_passed=metric.format_check_passed,
                minimization_score=quality.minimization_score if quality else None,
                minimization_warnings=list(quality.minimization_warnings) if quality else [],
                patch_quality_warnings=list(quality.warnings) if quality else [],
                hard_limit_violations=list(quality.hard_limit_violations) if quality else [],
                unrelated_files_count=metric.unrelated_files_count,
                guardrail_override_used=self._guardrail_override_used(run.id),
                failure_category=run.failure.category if run.failure else None,
                human_review_status=patch.review_status if patch else "pending",
                block_on_lint_failure=flags[0],
                block_on_format_check_failure=flags[1],
            )
        )
        metric.code_quality_score = result.code_quality_score
        metric.review_ready = result.review_ready
        metric.review_blockers = result.review_blockers
        metric.review_warnings = result.review_warnings
        self._db.add(metric)
        self._db.commit()
        self._db.refresh(metric)
        return metric

    def _stored_flags(self, run_id: UUID) -> tuple[bool, bool]:
        event = self._db.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.agent_run_id == run_id,
                AgentEvent.event_type == "agent_run_configured",
            )
            .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
            .limit(1)
        )
        config = (event.payload_json or {}).get("config", {}) if event else {}
        return (
            config.get("block_on_lint_failure") is True,
            config.get("block_on_format_check_failure") is True,
        )

    def _guardrail_override_used(self, run_id: UUID) -> bool:
        return (
            self._db.scalar(
                select(AgentEvent.id)
                .where(
                    AgentEvent.agent_run_id == run_id,
                    AgentEvent.event_type == "patch_file_guardrail_override",
                )
                .limit(1)
            )
            is not None
        )


def _quality_check(
    *,
    passed: bool | None,
    blocker: bool,
    code: str,
    penalty: float,
    score: float,
    blockers: list[str],
    warnings: list[str],
) -> float:
    if passed is not False:
        return score
    (blockers if blocker else warnings).append(code)
    return score - penalty


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
