from app.core.redaction import (
    REDACTION_MARKER,
    TRUNCATION_MARKER,
    redact_and_truncate,
    redact_common_secrets,
    redact_structured_value,
)
from app.schemas.sandbox import SandboxCommandResult


def test_common_secret_patterns_are_redacted(fake_secret_samples: dict[str, object]) -> None:
    redacted = redact_common_secrets(str(fake_secret_samples["text"]))

    for secret in fake_secret_samples["secrets"]:
        assert secret not in redacted
    assert redacted.count(REDACTION_MARKER) >= len(fake_secret_samples["secrets"])


def test_structured_redaction_preserves_safe_debug_context() -> None:
    payload = {
        "authorization": "Bearer sensitive-value",
        "nested": {
            "database_url": "postgresql://user:password@db/app",
            "message": "request failed while reading src/app.py",
        },
        "input_tokens": 42,
    }

    redacted = redact_structured_value(payload)

    assert redacted["authorization"] == REDACTION_MARKER
    assert redacted["nested"]["database_url"] == REDACTION_MARKER
    assert redacted["nested"]["message"] == "request failed while reading src/app.py"
    assert redacted["input_tokens"] == 42


def test_redaction_bounds_long_values_with_stable_marker() -> None:
    redacted = redact_and_truncate("password=secret-value " + ("x" * 100), max_chars=24)

    assert "secret-value" not in redacted
    assert REDACTION_MARKER in redacted
    assert TRUNCATION_MARKER in redacted


def test_redaction_is_idempotent() -> None:
    once = redact_common_secrets("token=secret-value Authorization: Bearer credentials")

    assert redact_common_secrets(once) == once


def test_sandbox_command_output_is_redacted(fake_secret_samples: dict[str, object]) -> None:
    result = SandboxCommandResult(
        phase="post_patch",
        command="pytest -q",
        passed=False,
        exit_code=1,
        stdout=str(fake_secret_samples["text"]),
        stderr="Authorization: Bearer command-error-token",
        duration_seconds=0.1,
    )
    serialized = result.model_dump_json()

    for secret in fake_secret_samples["secrets"]:
        assert secret not in serialized
    assert "command-error-token" not in serialized
    assert REDACTION_MARKER in serialized
