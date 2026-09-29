from app.core.config import settings
from app.schemas.sandbox import SandboxNetworkDecision


class SandboxNetworkPolicyError(RuntimeError):
    error_code = "network_mode_unsupported"


def resolve_network_policy(
    phase: str, *, requested_mode: str | None = None
) -> SandboxNetworkDecision:
    mode = (requested_mode if requested_mode is not None else settings.sandbox_network_mode).strip()
    if mode not in {"none", "bridge"}:
        raise SandboxNetworkPolicyError(
            "Unsupported sandbox network mode. Only 'none' and 'bridge' are supported; "
            "host, shared-container and custom networking are prohibited."
        )
    setup = phase == "setup"
    permitted = (
        settings.sandbox_allow_network_during_setup
        if setup
        else settings.sandbox_allow_network_during_tests
    )
    # An explicit setup opt-in selects bridge even with the safe global default of none.
    # A request for none always narrows policy, and unknown phases always fail closed.
    known_phase = phase in {
        "setup",
        "test",
        "baseline",
        "post_patch",
        "hidden_eval",
        "lint",
        "format_check",
        "flakiness",
        "agent_test",
    }
    enabled = known_phase and permitted and (mode == "bridge" or (setup and requested_mode is None))
    if enabled and settings.sandbox_allowed_hosts.strip():
        raise SandboxNetworkPolicyError(
            "SANDBOX_ALLOWED_HOSTS is reserved for future use; hostname filtering is not "
            "implemented. Refusing network access with an unenforced allowlist."
        )
    return SandboxNetworkDecision(
        phase=phase,
        requested_network_mode=mode,
        effective_network_mode="bridge" if enabled else "none",
        network_exception=enabled,
        reason="explicit_setup_exception"
        if enabled and setup
        else ("explicit_test_exception" if enabled else "network_disabled"),
    )
