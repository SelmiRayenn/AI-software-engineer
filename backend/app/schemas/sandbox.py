from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    test_repetitions: int = Field(default=1, ge=1, le=20)
    stop_on_first_test_failure: bool = False

    @field_validator("setup_commands", "test_commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            if not command.strip():
                raise ValueError("Commands must not be empty")
            if len(command) > 4000:
                raise ValueError("Commands must be 4000 characters or fewer")
        return commands


class SandboxCommandResult(BaseModel):
    phase: Literal["clone", "checkout", "setup", "test"]
    command: str
    passed: bool
    timed_out: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_seconds: float


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
