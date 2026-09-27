from app.review_readiness import ReviewReadinessInputs, calculate_review_readiness


def inputs(**overrides: object) -> ReviewReadinessInputs:
    values: dict[str, object] = {
        "patch_applied": True,
        "visible_tests_passed": True,
        "hidden_tests_passed": True,
        "hidden_tests_run_count": 1,
        "lint_passed": True,
        "format_check_passed": True,
        "minimization_score": 1.0,
        "minimization_warnings": [],
        "patch_quality_warnings": [],
        "hard_limit_violations": [],
        "unrelated_files_count": 0,
        "guardrail_override_used": False,
        "failure_category": None,
        "human_review_status": "pending",
        "block_on_lint_failure": False,
        "block_on_format_check_failure": False,
    }
    values.update(overrides)
    return ReviewReadinessInputs(**values)  # type: ignore[arg-type]


def test_passing_clean_patch_is_review_ready() -> None:
    result = calculate_review_readiness(inputs())

    assert result.review_ready is True
    assert result.code_quality_score == 1.0
    assert result.review_blockers == []
    assert result.review_warnings == []


def test_failing_visible_tests_block_readiness() -> None:
    result = calculate_review_readiness(inputs(visible_tests_passed=False))

    assert result.review_ready is False
    assert "visible_tests_failed" in result.review_blockers


def test_hidden_test_failure_blocks_readiness() -> None:
    result = calculate_review_readiness(inputs(hidden_tests_passed=False))

    assert result.review_ready is False
    assert "hidden_tests_failed" in result.review_blockers


def test_lint_failure_warns_by_default_and_can_be_configured_as_blocker() -> None:
    warning = calculate_review_readiness(inputs(lint_passed=False))
    blocking = calculate_review_readiness(inputs(lint_passed=False, block_on_lint_failure=True))

    assert warning.review_ready is True
    assert "lint_failed" in warning.review_warnings
    assert blocking.review_ready is False
    assert "lint_failed" in blocking.review_blockers


def test_rejected_human_review_blocks_readiness() -> None:
    result = calculate_review_readiness(inputs(human_review_status="rejected"))

    assert result.review_ready is False
    assert "human_review_rejected" in result.review_blockers


def test_approved_review_does_not_override_failed_tests() -> None:
    result = calculate_review_readiness(
        inputs(human_review_status="approved", visible_tests_passed=False)
    )

    assert result.review_ready is False
    assert result.review_blockers == ["visible_tests_failed"]


def test_missing_hidden_tests_lowers_confidence_without_blocking() -> None:
    result = calculate_review_readiness(inputs(hidden_tests_passed=None, hidden_tests_run_count=0))

    assert result.review_ready is True
    assert result.code_quality_score == 0.95
    assert result.review_warnings == ["hidden_tests_not_run"]


def test_hard_quality_rejection_and_guardrail_override_are_reflected() -> None:
    rejected = calculate_review_readiness(inputs(hard_limit_violations=["unsafe edit"]))
    overridden = calculate_review_readiness(inputs(guardrail_override_used=True))

    assert rejected.review_ready is False
    assert "patch_quality_rejected" in rejected.review_blockers
    assert overridden.review_ready is True
    assert "trusted_guardrail_override_used" in overridden.review_warnings
