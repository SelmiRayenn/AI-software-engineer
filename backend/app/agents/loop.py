from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.prompts import RenderedAgentPrompts, redact_prompt_text, render_agent_prompts
from app.agents.repairs import AgentRepairService
from app.agents.tools import (
    AgentWorkspaceTools,
    CodeSearchResult,
    DiffResult,
    FileListingResult,
    FileReadResult,
    FileWriteResult,
    RelevantFilesResult,
    SubmittedPatchResult,
    TestCommandResult,
)
from app.failures.categories import (
    FAILURE_MALFORMED_TOOL_CALL,
    FAILURE_MAX_STEPS_REACHED,
    FAILURE_MODEL_PROVIDER_ERROR,
    FAILURE_PATCH_APPLY_FAILED,
    FAILURE_PATCH_QUALITY_BLOCKED,
    FAILURE_TIMEOUT,
    FAILURE_TOOL_ERROR_LIMIT_REACHED,
    FAILURE_UNKNOWN_TOOL,
)
from app.model_providers import (
    ModelMessage,
    ModelProvider,
    ModelProviderResponse,
    ModelToolCall,
    ToolDefinition,
)
from app.models import AgentEvent, AgentRun, BenchmarkTask, Repository
from app.schemas.agent_run import AgentRunConfig, AgentRunTraceStep

BASE_TOOL_NAMES = (
    "retrieve_relevant_files",
    "list_files",
    "search_code",
    "read_file",
    "write_file",
    "get_diff",
    "submit_patch",
)


def registered_tool_names(*, enable_test_tool: bool) -> list[str]:
    names = list(BASE_TOOL_NAMES)
    if enable_test_tool:
        names.insert(5, "run_tests")
    return names


class AgentLoopError(RuntimeError):
    pass


class MalformedToolCallError(AgentLoopError):
    pass


class UnknownToolError(AgentLoopError):
    pass


@dataclass(frozen=True)
class AgentLoopResult:
    steps: list[AgentRunTraceStep]
    messages: list[ModelMessage]
    submitted_patch: SubmittedPatchResult | None
    stop_reason: str
    model_calls: int
    tool_errors: int
    error_message: str | None = None
    failure_category: str | None = None


@dataclass(frozen=True)
class _ToolContract:
    handler: Callable[..., Any]
    required: dict[str, type]
    optional: dict[str, type]


@dataclass(frozen=True)
class _ParsedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


class AgentLoop:
    def __init__(
        self,
        *,
        db: Session,
        agent_run: AgentRun,
        benchmark_task: BenchmarkTask,
        repository: Repository,
        provider: ModelProvider,
        tools: AgentWorkspaceTools,
        config: AgentRunConfig | None = None,
        prompts: RenderedAgentPrompts | None = None,
        max_steps: int | None = None,
        max_tool_errors: int = 3,
        repairs: AgentRepairService | None = None,
    ) -> None:
        config = config or AgentRunConfig(
            model_provider=provider.provider_name,
            model_name=provider.model_name,
            max_steps=4 if max_steps is None else max_steps,
            max_tool_errors=max_tool_errors,
        )
        max_steps = config.max_steps
        max_tool_errors = config.max_tool_errors
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")
        if max_tool_errors < 1:
            raise ValueError("max_tool_errors must be at least 1.")

        self._db = db
        self._agent_run = agent_run
        self._benchmark_task = benchmark_task
        self._repository = repository
        self._provider = provider
        self._tools = tools
        self._config = config
        self._repairs = repairs
        self._max_steps = max_steps
        self._max_tool_errors = max_tool_errors
        self._contracts = self._build_tool_contracts()
        self._prompts = prompts or render_agent_prompts(
            task=benchmark_task,
            repository=repository,
            allowed_tools=self.registered_tool_names(),
            configured_test_commands=list(benchmark_task.test_commands or []),
            max_steps=config.max_steps,
            max_tool_errors=config.max_tool_errors,
            command_timeout_seconds=config.command_timeout_seconds,
            include_issue_comments=config.include_issue_comments,
            enable_test_tool=config.enable_test_tool,
            run_mode=config.run_mode,
            max_repair_attempts=config.max_repair_attempts,
            run_tests_after_patch=config.run_tests_after_patch,
            stop_on_first_passing_patch=config.stop_on_first_passing_patch,
            include_test_failure_feedback=config.include_test_failure_feedback,
        )

    def build_initial_messages(self) -> list[ModelMessage]:
        return self._prompts.messages()

    def prompt_preview(self) -> dict[str, str]:
        return self._prompts.redacted_preview()

    def registered_tool_names(self) -> list[str]:
        return registered_tool_names(enable_test_tool=self._config.enable_test_tool)

    def run(self) -> AgentLoopResult:
        messages = self.build_initial_messages()
        steps: list[AgentRunTraceStep] = []
        model_calls = 0
        tool_errors = 0
        last_tool_failure_category: str | None = None

        while model_calls < self._max_steps and len(steps) < self._max_steps:
            model_calls += 1
            self._log_event(
                "model_call_started",
                {
                    "step": model_calls,
                    "message_count": len(messages),
                    "provider_name": self._provider.provider_name,
                    "model_name": self._provider.model_name,
                },
            )
            try:
                response = self._provider.generate_response(
                    messages,
                    tools=self.tool_definitions(),
                )
            except Exception as exc:  # noqa: BLE001 - provider adapters are an external boundary
                error_message = redact_prompt_text(f"Model provider call failed: {exc}")[:2000]
                self._log_event(
                    "model_call_completed",
                    {
                        "step": model_calls,
                        "success": False,
                        "error_message": error_message,
                    },
                )
                return AgentLoopResult(
                    steps=steps,
                    messages=messages,
                    submitted_patch=None,
                    stop_reason="provider_error",
                    model_calls=model_calls,
                    tool_errors=tool_errors,
                    error_message=error_message,
                    failure_category=FAILURE_MODEL_PROVIDER_ERROR,
                )

            self._log_model_response(response, step=model_calls)
            messages.append(_assistant_message(response))
            raw_tool_calls = _response_tool_calls(response)

            if not raw_tool_calls:
                messages.append(
                    ModelMessage(
                        role="user",
                        content=(
                            "Continue by calling one of the registered tools. Submit the patch "
                            "only when the solution is ready."
                        ),
                    )
                )
                continue

            for call_index, raw_tool_call in enumerate(raw_tool_calls):
                if len(steps) >= self._max_steps:
                    return self._step_limit_result(
                        steps=steps,
                        messages=messages,
                        model_calls=model_calls,
                        tool_errors=tool_errors,
                    )

                request_payload = _raw_tool_call_payload(raw_tool_call)
                self._log_event(
                    "tool_call_requested",
                    {
                        "step": len(steps) + 1,
                        "tool_call": request_payload,
                    },
                )
                started = time.perf_counter()
                submission_started = False
                try:
                    tool_call = self._parse_tool_call(raw_tool_call)
                    if self._repairs and tool_call.name == "submit_patch":
                        self._repairs.begin_attempt()
                        submission_started = True
                    result = self._execute_tool(tool_call)
                except Exception as exc:  # noqa: BLE001 - controlled tools are an execution boundary
                    duration = time.perf_counter() - started
                    tool_errors += 1
                    tool_name = _raw_tool_name(raw_tool_call)
                    error_message = redact_prompt_text(str(exc))[:2000]
                    last_tool_failure_category = _tool_failure_category(exc, error_message)
                    steps.append(
                        AgentRunTraceStep(
                            step_name=tool_name,
                            success=False,
                            duration_seconds=duration,
                            error_message=error_message,
                        )
                    )
                    self._log_event(
                        "tool_call_failed",
                        {
                            "step": len(steps),
                            "tool_name": tool_name,
                            "tool_call_id": _raw_tool_call_id(raw_tool_call),
                            "error_message": error_message,
                            "duration_seconds": duration,
                            "tool_error_count": tool_errors,
                            "failure_category": last_tool_failure_category,
                        },
                    )
                    messages.append(
                        _tool_message(
                            tool_call_id=_raw_tool_call_id(raw_tool_call),
                            tool_name=tool_name,
                            success=False,
                            value={"error": error_message},
                        )
                    )
                    decision = None
                    if submission_started:
                        decision = self._repairs.assess(None, error=exc)
                        _skip_remaining_calls(messages, raw_tool_calls[call_index + 1 :])
                        messages.append(ModelMessage(role="user", content=decision.feedback))
                    if tool_errors >= self._max_tool_errors:
                        limit_error = (
                            f"Maximum tool error limit of {self._max_tool_errors} reached."
                        )
                        return AgentLoopResult(
                            steps=steps,
                            messages=messages,
                            submitted_patch=None,
                            stop_reason="max_tool_errors",
                            model_calls=model_calls,
                            tool_errors=tool_errors,
                            error_message=limit_error,
                            failure_category=(
                                last_tool_failure_category or FAILURE_TOOL_ERROR_LIMIT_REACHED
                            ),
                        )
                    if decision:
                        if decision.stop:
                            return AgentLoopResult(
                                steps=steps,
                                messages=messages,
                                submitted_patch=None,
                                stop_reason="patch_submitted",
                                model_calls=model_calls,
                                tool_errors=tool_errors,
                                error_message=decision.feedback,
                                failure_category=(
                                    last_tool_failure_category
                                    or _failure_category_for_error_message(decision.feedback)
                                ),
                            )
                        break
                    continue

                duration = time.perf_counter() - started
                steps.append(_trace_step(tool_call.name, result, duration))
                result_payload = _result_payload(result)
                self._log_event(
                    "tool_call_completed",
                    {
                        "step": len(steps),
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "duration_seconds": duration,
                        "result": _result_summary(result),
                    },
                )
                messages.append(
                    _tool_message(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        success=True,
                        value=result_payload,
                    )
                )

                if tool_call.name == "submit_patch":
                    if not isinstance(result, SubmittedPatchResult):
                        raise AgentLoopError("submit_patch returned an invalid result.")
                    self._log_event(
                        "patch_submitted",
                        {
                            "generated_patch_id": str(result.generated_patch_id),
                            "changed_files": result.changed_files,
                            "patch_size_bytes": len(result.patch_text.encode("utf-8")),
                            "patch_version": result.version,
                        },
                    )
                    if self._repairs:
                        decision = self._repairs.assess(result)
                        _skip_remaining_calls(messages, raw_tool_calls[call_index + 1 :])
                        messages.append(ModelMessage(role="user", content=decision.feedback))
                        if decision.invalid_patch:
                            tool_errors += 1
                            last_tool_failure_category = _failure_category_for_error_message(
                                decision.feedback
                            )
                            self._log_event(
                                "tool_call_failed",
                                {
                                    "tool_name": "submit_patch",
                                    "tool_call_id": tool_call.id,
                                    "error_message": decision.feedback,
                                    "tool_error_count": tool_errors,
                                    "failure_category": last_tool_failure_category,
                                },
                            )
                        if tool_errors >= self._max_tool_errors:
                            return AgentLoopResult(
                                steps=steps,
                                messages=messages,
                                submitted_patch=result,
                                stop_reason="max_tool_errors",
                                model_calls=model_calls,
                                tool_errors=tool_errors,
                                error_message=f"Maximum tool error limit of {self._max_tool_errors} reached.",
                                failure_category=(
                                    last_tool_failure_category or FAILURE_TOOL_ERROR_LIMIT_REACHED
                                ),
                            )
                        if not decision.stop:
                            break
                    return AgentLoopResult(
                        steps=steps,
                        messages=messages,
                        submitted_patch=result,
                        stop_reason="patch_submitted",
                        model_calls=model_calls,
                        tool_errors=tool_errors,
                    )

        return self._step_limit_result(
            steps=steps,
            messages=messages,
            model_calls=model_calls,
            tool_errors=tool_errors,
        )

    def tool_definitions(self) -> list[ToolDefinition]:
        test_commands = list(self._benchmark_task.test_commands or [])
        definitions = [
            ToolDefinition(
                name="retrieve_relevant_files",
                description=(
                    "Search the current run's deterministic repository index for likely relevant "
                    "files before reading or editing."
                ),
                input_schema=_object_schema(
                    required={"query": {"type": "string", "minLength": 1, "maxLength": 200}},
                    optional={
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 10,
                        },
                        "semantic": {
                            "type": "boolean",
                            "default": False,
                            "description": "Use optional stored embeddings; otherwise fall back to lexical search.",
                        },
                    },
                ),
            ),
            ToolDefinition(
                name="list_files",
                description="List workspace files under an optional relative directory.",
                input_schema=_object_schema(optional={"path": {"type": "string"}}),
            ),
            ToolDefinition(
                name="search_code",
                description="Search for text in workspace files.",
                input_schema=_object_schema(
                    required={"query": {"type": "string"}},
                    optional={"path": {"type": "string"}},
                ),
            ),
            ToolDefinition(
                name="read_file",
                description="Read one UTF-8 workspace file by relative path.",
                input_schema=_object_schema(required={"file_path": {"type": "string"}}),
            ),
            ToolDefinition(
                name="write_file",
                description="Write UTF-8 content to one workspace file by relative path.",
                input_schema=_object_schema(
                    required={
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    }
                ),
            ),
            ToolDefinition(
                name="get_diff",
                description="Return the current unified diff and changed file list.",
                input_schema=_object_schema(),
            ),
            ToolDefinition(
                name="submit_patch",
                description="Submit the current diff for validation and configured tests; inspect repair feedback if returned.",
                input_schema=_object_schema(),
            ),
        ]
        if self._config.enable_test_tool:
            definitions.insert(
                5,
                ToolDefinition(
                    name="run_tests",
                    description="Run one test command configured by the benchmark task.",
                    input_schema=_object_schema(
                        required={
                            "command": {
                                "type": "string",
                                "enum": test_commands,
                            }
                        }
                    ),
                ),
            )
        return definitions

    def _build_tool_contracts(self) -> dict[str, _ToolContract]:
        contracts = {
            "retrieve_relevant_files": _ToolContract(
                self._tools.retrieve_relevant_files,
                {"query": str},
                {"limit": int, "semantic": bool},
            ),
            "list_files": _ToolContract(self._tools.list_files, {}, {"path": str}),
            "search_code": _ToolContract(
                self._tools.search_code,
                {"query": str},
                {"path": str},
            ),
            "read_file": _ToolContract(self._tools.read_file, {"file_path": str}, {}),
            "write_file": _ToolContract(
                self._tools.write_file,
                {"file_path": str, "content": str},
                {},
            ),
            "get_diff": _ToolContract(self._tools.get_diff, {}, {}),
            "submit_patch": _ToolContract(self._tools.submit_patch, {}, {}),
        }
        if self._config.enable_test_tool:
            contracts["run_tests"] = _ToolContract(
                self._tools.run_tests,
                {"command": str},
                {},
            )
        return contracts

    def _parse_tool_call(self, raw_tool_call: Any) -> _ParsedToolCall:
        if isinstance(raw_tool_call, ModelToolCall):
            tool_call_id = raw_tool_call.id
            name = raw_tool_call.name
            arguments = raw_tool_call.arguments
        elif isinstance(raw_tool_call, dict):
            tool_call_id = raw_tool_call.get("id")
            name = raw_tool_call.get("name")
            arguments = raw_tool_call.get("arguments", {})
        else:
            raise MalformedToolCallError("Tool call must be a structured object.")

        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise MalformedToolCallError("Tool call id must be a non-empty string.")
        if not isinstance(name, str) or not name.strip():
            raise MalformedToolCallError("Tool name must be a non-empty string.")
        if not isinstance(arguments, dict):
            raise MalformedToolCallError("Tool arguments must be an object.")
        if name not in self._contracts:
            raise UnknownToolError(f"Unknown tool: {name}")

        contract = self._contracts[name]
        allowed_arguments = set(contract.required) | set(contract.optional)
        unexpected = sorted(set(arguments) - allowed_arguments)
        if unexpected:
            raise MalformedToolCallError(
                f"Unexpected arguments for {name}: {', '.join(unexpected)}"
            )
        missing = sorted(set(contract.required) - set(arguments))
        if missing:
            raise MalformedToolCallError(f"Missing arguments for {name}: {', '.join(missing)}")
        for argument_name, expected_type in {**contract.required, **contract.optional}.items():
            if argument_name in arguments and not isinstance(
                arguments[argument_name],
                expected_type,
            ):
                raise MalformedToolCallError(
                    f"Argument {argument_name} for {name} must be {expected_type.__name__}."
                )

        return _ParsedToolCall(
            id=tool_call_id,
            name=name,
            arguments=dict(arguments),
        )

    def _execute_tool(self, tool_call: _ParsedToolCall) -> Any:
        return self._contracts[tool_call.name].handler(**tool_call.arguments)

    def _log_model_response(self, response: ModelProviderResponse, *, step: int) -> None:
        tool_calls = [_raw_tool_call_payload(call) for call in _response_tool_calls(response)]
        payload = {
            "step": step,
            "success": True,
            "provider_name": self._provider.provider_name,
            "model_name": self._provider.model_name,
            "content_preview": response.content[:500],
            "tool_calls": tool_calls,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": response.estimated_cost,
            "latency_seconds": response.latency_seconds,
        }
        self._log_event("model_call_completed", payload)
        self._log_event("model_response", payload)

    def _step_limit_result(
        self,
        *,
        steps: list[AgentRunTraceStep],
        messages: list[ModelMessage],
        model_calls: int,
        tool_errors: int,
    ) -> AgentLoopResult:
        error_message = f"Maximum step limit of {self._max_steps} reached."
        self._log_event(
            "step_limit_reached",
            {
                "max_steps": self._max_steps,
                "model_calls": model_calls,
                "tool_calls": len(steps),
                "tool_errors": tool_errors,
            },
        )
        return AgentLoopResult(
            steps=steps,
            messages=messages,
            submitted_patch=None,
            stop_reason="max_steps",
            model_calls=model_calls,
            tool_errors=tool_errors,
            error_message=error_message,
            failure_category=FAILURE_MAX_STEPS_REACHED,
        )

    def _log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._db.add(
            AgentEvent(
                agent_run_id=self._agent_run.id,
                event_type=event_type,
                payload_json=_sanitize_for_log(_json_safe(payload)),
            )
        )
        self._db.commit()


def _object_schema(
    *,
    required: dict[str, dict[str, Any]] | None = None,
    optional: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = required or {}
    optional = optional or {}
    return {
        "type": "object",
        "properties": {**required, **optional},
        "required": list(required),
        "additionalProperties": False,
    }


def _assistant_message(response: ModelProviderResponse) -> ModelMessage:
    return ModelMessage(
        role="assistant",
        content=json.dumps(
            {
                "content": response.content,
                "tool_calls": [
                    _raw_tool_call_payload(call) for call in _response_tool_calls(response)
                ],
            },
            default=str,
        ),
    )


def _skip_remaining_calls(messages: list[ModelMessage], calls: list[Any]) -> None:
    # Complete provider tool-call history without executing stale edits after a submission.
    for call in calls:
        messages.append(
            _tool_message(
                tool_call_id=_raw_tool_call_id(call),
                tool_name=_raw_tool_name(call),
                success=False,
                value={"error": "Not executed: patch submission ended this tool batch."},
            )
        )


def _response_tool_calls(response: ModelProviderResponse) -> list[Any]:
    raw_tool_calls = response.tool_calls
    if raw_tool_calls is None:
        return []
    if isinstance(raw_tool_calls, Sequence) and not isinstance(raw_tool_calls, (str, bytes)):
        return list(raw_tool_calls)
    return [raw_tool_calls]


def _tool_message(
    *,
    tool_call_id: str | None,
    tool_name: str,
    success: bool,
    value: Any,
) -> ModelMessage:
    return ModelMessage(
        role="tool",
        content=json.dumps(
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "success": success,
                "result": _json_safe(value),
            }
        ),
    )


def _raw_tool_call_payload(raw_tool_call: Any) -> dict[str, Any]:
    if isinstance(raw_tool_call, ModelToolCall):
        return {
            "id": raw_tool_call.id,
            "name": raw_tool_call.name,
            "arguments": raw_tool_call.arguments,
        }
    if isinstance(raw_tool_call, dict):
        return dict(raw_tool_call)
    return {"value": repr(raw_tool_call)}


def _raw_tool_name(raw_tool_call: Any) -> str:
    if isinstance(raw_tool_call, ModelToolCall):
        return raw_tool_call.name if isinstance(raw_tool_call.name, str) else "malformed_tool_call"
    if isinstance(raw_tool_call, dict) and isinstance(raw_tool_call.get("name"), str):
        return raw_tool_call["name"]
    return "malformed_tool_call"


def _raw_tool_call_id(raw_tool_call: Any) -> str | None:
    if isinstance(raw_tool_call, ModelToolCall):
        return raw_tool_call.id if isinstance(raw_tool_call.id, str) else None
    if isinstance(raw_tool_call, dict) and isinstance(raw_tool_call.get("id"), str):
        return raw_tool_call["id"]
    return None


def _tool_failure_category(exc: Exception, message: str) -> str | None:
    if isinstance(exc, UnknownToolError):
        return FAILURE_UNKNOWN_TOOL
    if isinstance(exc, MalformedToolCallError):
        return FAILURE_MALFORMED_TOOL_CALL
    return _failure_category_for_error_message(message)


def _failure_category_for_error_message(message: str) -> str | None:
    value = message.lower()
    if "quality guardrail" in value or "patch quality" in value:
        return FAILURE_PATCH_QUALITY_BLOCKED
    if "does not apply cleanly" in value or "could not be applied" in value:
        return FAILURE_PATCH_APPLY_FAILED
    if "timed out" in value or "timeout" in value:
        return FAILURE_TIMEOUT
    return None


def _result_payload(result: Any) -> dict[str, Any]:
    if is_dataclass(result):
        return _json_safe(asdict(result))
    if isinstance(result, dict):
        return _json_safe(result)
    return {"value": _json_safe(result)}


def _result_summary(result: Any) -> dict[str, Any]:
    if isinstance(result, RelevantFilesResult):
        return {
            "result_count": len(result.files),
            "files": [file.file_path for file in result.files],
            "retrieval_mode": result.retrieval_mode,
            "fallback_reason": result.fallback_reason,
        }
    if isinstance(result, FileListingResult):
        return {"file_count": len(result.files), "truncated": result.truncated}
    if isinstance(result, CodeSearchResult):
        return {"match_count": len(result.matches), "truncated": result.truncated}
    if isinstance(result, FileReadResult):
        return {"file_path": result.file_path, "size_bytes": result.size_bytes}
    if isinstance(result, FileWriteResult):
        return {"file_path": result.file_path, "bytes_written": result.bytes_written}
    if isinstance(result, TestCommandResult):
        return {
            "command": result.command,
            "passed": result.passed,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
        }
    if isinstance(result, (DiffResult, SubmittedPatchResult)):
        summary = {
            "changed_files": result.changed_files,
            "patch_size_bytes": len(result.patch_text.encode("utf-8")),
        }
        if isinstance(result, SubmittedPatchResult):
            summary["generated_patch_id"] = str(result.generated_patch_id)
        return summary
    return {}


def _trace_step(tool_name: str, result: Any, duration_seconds: float) -> AgentRunTraceStep:
    files_read: list[str] = []
    files_modified: list[str] = []
    if isinstance(result, RelevantFilesResult):
        files_read = [file.file_path for file in result.files]
    elif isinstance(result, FileReadResult):
        files_read = [result.file_path]
    elif isinstance(result, CodeSearchResult):
        files_read = list(dict.fromkeys(match.file_path for match in result.matches))
    elif isinstance(result, FileWriteResult):
        files_modified = [result.file_path]
    elif isinstance(result, (DiffResult, SubmittedPatchResult)):
        files_read = list(result.changed_files)

    return AgentRunTraceStep(
        step_name=tool_name,
        success=True,
        duration_seconds=duration_seconds,
        summary=_result_summary(result),
        files_read=files_read,
        files_modified=files_modified,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            normalized_key = key.lower().replace("-", "_")
            if (
                normalized_key in {"token", "access_token", "refresh_token", "auth_token"}
                or "api_key" in normalized_key
                or "apikey" in normalized_key
                or "password" in normalized_key
                or "secret" in normalized_key
                or "authorization" in normalized_key
            ):
                sanitized[key] = "[REDACTED]"
            elif key in {"content", "patch_text", "stdout", "stderr"} and isinstance(
                nested,
                str,
            ):
                sanitized[key] = {
                    "size_bytes": len(nested.encode("utf-8")),
                    "preview": _redact_secret_patterns(nested[:200]),
                }
            else:
                sanitized[key] = _sanitize_for_log(nested)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value[:100]]
    if isinstance(value, str):
        return _redact_secret_patterns(value[:2_000])
    return value


def _redact_secret_patterns(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    return re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b",
        "[REDACTED]",
        redacted,
    )
