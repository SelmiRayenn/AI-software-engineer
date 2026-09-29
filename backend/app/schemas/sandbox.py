from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.redaction import redact_and_truncate, redact_common_secrets

SandboxStatus = Literal[
    "passed",
    "clone_failed",
    "checkout_failed",
    "setup_failed",
    "tests_failed",
    "sandbox_error",
]


class SandboxRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_url: str = Field(min_length=1, max_length=2048)
    base_commit: str = Field(min_length=7, max_length=64)
    setup_commands: list[str] = Field(default_factory=list, max_length=20)
    test_commands: list[str] = Field(min_length=1, max_length=50)
    command_timeout_seconds: int | None = Field(default=None, ge=1)
    network_enabled: bool | None = None
    network_mode: str | None = Field(default=None, max_length=100)
    test_repetitions: int = Field(default=1, ge=1, le=20)
    stop_on_first_test_failure: bool = False

    @model_validator(mode="after")
    def validate_network_request(self):
        if self.network_enabled is not None and self.network_mode is not None:
            raise ValueError("Use network_mode or legacy network_enabled, not both.")
        return self

    @field_validator("setup_commands", "test_commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            if not command.strip():
                raise ValueError("Commands must not be empty")
            if len(command) > 4000:
                raise ValueError("Commands must be 4000 characters or fewer")
        return commands


class SandboxNetworkDecision(BaseModel):
    phase: str
    requested_network_mode: str
    effective_network_mode: Literal["none", "bridge"]
    network_exception: bool
    reason: str


class SandboxCommandResult(BaseModel):
    phase: str
    command: str
    passed: bool
    timed_out: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_seconds: float
    network_policy: SandboxNetworkDecision | None = None
    error_code: str | None = None

    @field_validator("command", "stdout", "stderr", mode="before")
    @classmethod
    def redact_command_output(cls, value: str) -> str:
        return redact_and_truncate(value, max_chars=64_000)


class SandboxRunResponse(BaseModel):
    status: SandboxStatus
    workspace_id: str
    workspace_path: str
    workspace_root: str
    workspace_retained: bool
    repository_url: str
    base_commit: str
    image: str
    network_enabled: bool
    command_timeout_seconds: int
    clone_result: SandboxCommandResult
    checkout_result: SandboxCommandResult | None = None
    setup_results: list[SandboxCommandResult] = Field(default_factory=list)
    test_results: list[SandboxCommandResult] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    cleanup_error: str | None = None

    @field_validator("repository_url", "error", "cleanup_error", mode="before")
    @classmethod
    def redact_response_text(cls, value: str | None) -> str | None:
        return redact_common_secrets(value) if value is not None else None
