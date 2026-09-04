from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from app.core.config import Settings, settings
from app.model_providers.base import (
    ModelMessage,
    ModelProviderConfigError,
    ModelProviderResponse,
    ModelToolCall,
    ToolDefinition,
)

OPENAI_PRICING_USD_PER_MILLION_TOKENS = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

ANTHROPIC_ESTIMATED_PRICING_PER_1K = {
    "claude-3-5-haiku-latest": {"input": 0.00080, "output": 0.00400},
}


class MockModelProvider:
    provider_name = "mock"

    def __init__(
        self,
        model_name: str = "mock-model",
        content: str = "Mock model response.",
        tool_calls: Sequence[ModelToolCall] | None = None,
        responses: Sequence[ModelProviderResponse] | None = None,
    ) -> None:
        self.model_name = model_name
        self._content = content
        self._tool_calls = list(tool_calls or [])
        self._tool_calls_configured = tool_calls is not None
        self._responses = list(responses or [])
        self._response_index = 0

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        started = time.perf_counter()
        if self._responses:
            response_index = min(self._response_index, len(self._responses) - 1)
            self._response_index += 1
            return self._responses[response_index]

        tool_calls = (
            list(self._tool_calls)
            if self._tool_calls_configured
            else self._default_tool_calls(messages)
        )
        self._response_index += 1
        input_tokens = _estimate_tokens_from_messages(messages)
        output_tokens = _estimate_tokens(self._content)
        return ModelProviderResponse(
            content=self._content,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=self.estimate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            latency_seconds=time.perf_counter() - started,
            raw_response={
                "provider": self.provider_name,
                "model": self.model_name,
                "tools_available": [tool.name for tool in tools or []],
            },
        )

    def _default_tool_calls(self, messages: Sequence[ModelMessage]) -> list[ModelToolCall]:
        call_number = self._response_index + 1
        if call_number == 1:
            return [ModelToolCall(id="mock-list-files", name="list_files")]
        if call_number == 2:
            return [
                ModelToolCall(
                    id="mock-read-file",
                    name="read_file",
                    arguments={"file_path": _mock_read_target(messages)},
                )
            ]
        if call_number == 3:
            return [ModelToolCall(id="mock-get-diff", name="get_diff")]
        return [ModelToolCall(id="mock-submit-patch", name="submit_patch")]

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float:
        return 0.0


class OpenAIProvider:
    provider_name = "openai"

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        app_settings: Settings = settings,
        client: Any | None = None,
    ) -> None:
        if not app_settings.enable_real_model_calls:
            raise ModelProviderConfigError(
                "OpenAI model calls are disabled. Set ENABLE_REAL_MODEL_CALLS=true to enable "
                "OpenAIProvider."
            )

        self.model_name = model_name or app_settings.openai_default_model
        self._api_key = api_key if api_key is not None else app_settings.openai_api_key
        if not self._api_key:
            raise ModelProviderConfigError("OPENAI_API_KEY is required for OpenAIProvider.")
        self._client = client

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        started = time.perf_counter()
        request: dict[str, Any] = {
            "model": self.model_name,
            "input": _openai_input(messages),
            "store": False,
        }
        if tools:
            request["tools"] = [_openai_tool_definition(tool) for tool in tools]
            request["tool_choice"] = "auto"

        response = self._get_client().responses.create(**request)
        input_tokens, output_tokens = _openai_usage(response)
        return ModelProviderResponse(
            content=_openai_response_content(response),
            tool_calls=_openai_tool_calls(response),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=self.estimate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            latency_seconds=time.perf_counter() - started,
            raw_response=_safe_openai_response(response),
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelProviderConfigError(
                "The OpenAI Python SDK is not installed. Install the backend dependencies."
            ) from exc
        self._client = OpenAI(api_key=self._api_key)
        return self._client

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        pricing = OPENAI_PRICING_USD_PER_MILLION_TOKENS.get(self.model_name)
        if pricing is None or input_tokens is None or output_tokens is None:
            return None
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return round(input_cost + output_cost, 8)


def _openai_input(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant":
            assistant_payload = _agent_loop_assistant_payload(message.content)
            if assistant_payload is not None:
                content = assistant_payload.get("content")
                if isinstance(content, str) and content:
                    items.append({"role": "assistant", "content": content})
                for tool_call in assistant_payload.get("tool_calls", []):
                    items.append(_openai_function_call_input(tool_call))
                continue

        if message.role == "tool":
            items.append(_openai_function_output_input(message.content))
            continue

        if message.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"Unsupported model message role for OpenAI: {message.role}")
        items.append({"role": message.role, "content": message.content})
    return items


def _agent_loop_assistant_payload(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_calls"), list):
        return None
    return payload


def _openai_function_call_input(tool_call: Any) -> dict[str, Any]:
    if not isinstance(tool_call, dict):
        raise TypeError("Agent loop tool-call history must contain objects.")
    call_id = tool_call.get("id")
    name = tool_call.get("name")
    arguments = tool_call.get("arguments", {})
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("Agent loop tool-call history is missing a call id.")
    if not isinstance(name, str) or not name:
        raise ValueError("Agent loop tool-call history is missing a tool name.")
    if not isinstance(arguments, dict):
        raise TypeError("Agent loop tool-call history arguments must be an object.")
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, separators=(",", ":")),
    }


def _openai_function_output_input(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent loop tool result must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise TypeError("Agent loop tool result must be an object.")
    call_id = payload.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("Agent loop tool result is missing a call id.")
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": content,
    }


def _openai_tool_definition(tool: ToolDefinition) -> dict[str, Any]:
    parameters = tool.input_schema or {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": parameters,
        "strict": False,
    }


def _openai_response_content(response: Any) -> str:
    output_text = _value(response, "output_text")
    if isinstance(output_text, str):
        return output_text

    text_parts: list[str] = []
    for item in _as_sequence(_value(response, "output", [])):
        if _value(item, "type") != "message":
            continue
        for content_item in _as_sequence(_value(item, "content", [])):
            if _value(content_item, "type") not in {"output_text", "text"}:
                continue
            text = _value(content_item, "text")
            if isinstance(text, str):
                text_parts.append(text)
    return "\n".join(text_parts)


def _openai_tool_calls(response: Any) -> list[ModelToolCall]:
    tool_calls: list[ModelToolCall] = []
    for item in _as_sequence(_value(response, "output", [])):
        if _value(item, "type") != "function_call":
            continue
        call_id = _value(item, "call_id") or _value(item, "id")
        name = _value(item, "name")
        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError("OpenAI returned a function call without an id.")
        if not isinstance(name, str) or not name:
            raise RuntimeError("OpenAI returned a function call without a name.")
        arguments = _openai_tool_arguments(_value(item, "arguments", {}))
        tool_calls.append(ModelToolCall(id=call_id, name=name, arguments=arguments))
    return tool_calls


def _openai_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError("OpenAI returned invalid JSON tool arguments.") from exc
    if not isinstance(arguments, dict):
        raise TypeError("OpenAI returned tool arguments that were not an object.")
    return dict(arguments)


def _openai_usage(response: Any) -> tuple[int | None, int | None]:
    usage = _value(response, "usage")
    return _optional_int(_value(usage, "input_tokens")), _optional_int(
        _value(usage, "output_tokens")
    )


def _safe_openai_response(response: Any) -> dict[str, Any]:
    input_tokens, output_tokens = _openai_usage(response)
    output_types = [
        item_type
        for item in _as_sequence(_value(response, "output", []))[:100]
        if isinstance((item_type := _value(item, "type")), str)
    ]
    return {
        "id": _safe_scalar(_value(response, "id")),
        "model": _safe_scalar(_value(response, "model")),
        "status": _safe_scalar(_value(response, "status")),
        "output_types": output_types,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


class AnthropicProvider:
    provider_name = "anthropic"

    def __init__(
        self,
        model_name: str = "claude-3-5-haiku-latest",
        api_key: str | None = None,
        app_settings: Settings = settings,
    ) -> None:
        self.model_name = model_name
        self._api_key = api_key if api_key is not None else app_settings.anthropic_api_key
        if not self._api_key:
            raise ModelProviderConfigError("ANTHROPIC_API_KEY is required for AnthropicProvider.")

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        raise NotImplementedError("AnthropicProvider API calls are not implemented yet.")

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        return _estimate_cost(
            ANTHROPIC_ESTIMATED_PRICING_PER_1K.get(self.model_name),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class LocalModelProvider:
    provider_name = "local"

    def __init__(
        self,
        model_name: str = "local-model",
        endpoint: str | None = None,
        app_settings: Settings = settings,
    ) -> None:
        self.model_name = model_name
        self._endpoint = endpoint if endpoint is not None else app_settings.local_model_endpoint
        if not self._endpoint:
            raise ModelProviderConfigError(
                "LOCAL_MODEL_ENDPOINT is required for LocalModelProvider."
            )

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        raise NotImplementedError("LocalModelProvider API calls are not implemented yet.")

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        return None


def _estimate_tokens_from_messages(messages: Sequence[ModelMessage]) -> int:
    return sum(_estimate_tokens(message.content) for message in messages)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text.split()))


def _estimate_cost(
    pricing: dict[str, float] | None,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    return round(input_cost + output_cost, 8)


def _mock_read_target(messages: Sequence[ModelMessage]) -> str:
    for message in reversed(messages):
        if message.role != "tool":
            continue
        try:
            observation = json.loads(message.content)
        except (TypeError, json.JSONDecodeError):
            continue
        if observation.get("tool_name") != "list_files" or not observation.get("success"):
            continue
        files = observation.get("result", {}).get("files", [])
        if not isinstance(files, list):
            continue
        readable_files = [file_path for file_path in files if isinstance(file_path, str)]
        for file_path in readable_files:
            if PurePosixPath(file_path).name.lower() in {
                "readme",
                "readme.md",
                "readme.rst",
                "readme.txt",
            }:
                return file_path
        source_extensions = {
            ".c",
            ".cc",
            ".cpp",
            ".go",
            ".h",
            ".hpp",
            ".java",
            ".js",
            ".jsx",
            ".php",
            ".py",
            ".rb",
            ".rs",
            ".ts",
            ".tsx",
        }
        for file_path in readable_files:
            if PurePosixPath(file_path).suffix.lower() in source_extensions:
                return file_path
        if readable_files:
            return readable_files[0]
    return "README.md"
