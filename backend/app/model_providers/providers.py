from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

import httpx

from app.core.config import Settings, settings
from app.model_providers.base import (
    ModelMessage,
    ModelProviderConfigError,
    ModelProviderRequestError,
    ModelProviderResponse,
    ModelToolCall,
    ToolDefinition,
)

OPENAI_PRICING_USD_PER_MILLION_TOKENS = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

ANTHROPIC_PRICING_USD_PER_MILLION_TOKENS = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
}

ANTHROPIC_MAX_OUTPUT_TOKENS = 4096


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
            else self._default_tool_calls(messages, tools)
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

    def _default_tool_calls(
        self, messages: Sequence[ModelMessage], tools: Sequence[ToolDefinition] | None = None
    ) -> list[ModelToolCall]:
        call_number = self._response_index + 1
        if call_number == 1:
            return [
                ModelToolCall(
                    id="mock-read-file",
                    name="read_file",
                    arguments={"file_path": _mock_read_target(messages)},
                )
            ]
        if call_number == 2:
            calls: list[ModelToolCall] = []
            if any(tool.name == "submit_plan" for tool in tools or []):
                calls.append(
                    ModelToolCall(
                        id="mock-submit-plan",
                        name="submit_plan",
                        arguments={
                            "issue_summary": "Inspect the reported issue in this deterministic mock run.",
                            "suspected_root_cause": "Not established; mock mode proposes no code changes.",
                            "files_inspected": [_mock_read_target(messages)],
                            "files_likely_to_modify": [],
                            "test_strategy": "Use the configured baseline and post-patch tests.",
                            "risk_rollback_notes": "No-op patch; no source changes to revert.",
                        },
                    )
                )
            if any(tool.name == "submit_hypothesis" for tool in tools or []):
                calls.append(
                    ModelToolCall(
                        id="mock-submit-hypothesis",
                        name="submit_hypothesis",
                        arguments={
                            "summary": "Mock mode found no source change to make.",
                            "suspected_files": [_mock_read_target(messages)],
                            "supporting_evidence": [
                                "The inspected file provides context for the deterministic no-op run."
                            ],
                            "confidence": "low",
                            "status": "active",
                        },
                    )
                )
            if any(tool.name == "submit_candidate_files" for tool in tools or []):
                calls.append(
                    ModelToolCall(
                        id="mock-submit-candidate-files",
                        name="submit_candidate_files",
                        arguments={
                            "ranked_files": [
                                {
                                    "path": _mock_read_target(messages),
                                    "reason": "This is the file inspected by the deterministic mock run.",
                                    "confidence": "low",
                                }
                            ]
                        },
                    )
                )
            if calls:
                return calls
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
        model_name: str | None = None,
        api_key: str | None = None,
        app_settings: Settings = settings,
        client: Any | None = None,
    ) -> None:
        if not app_settings.enable_real_model_calls:
            raise ModelProviderConfigError(
                "Anthropic model calls are disabled. Set ENABLE_REAL_MODEL_CALLS=true to enable "
                "AnthropicProvider."
            )

        self.model_name = model_name or app_settings.anthropic_default_model
        self._api_key = api_key if api_key is not None else app_settings.anthropic_api_key
        if not self._api_key:
            raise ModelProviderConfigError("ANTHROPIC_API_KEY is required for AnthropicProvider.")
        self._client = client

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        started = time.perf_counter()
        system, anthropic_messages = _anthropic_input(messages)
        request: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": ANTHROPIC_MAX_OUTPUT_TOKENS,
            "messages": anthropic_messages,
            "thinking": {"type": "disabled"},
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [_anthropic_tool_definition(tool) for tool in tools]
            request["tool_choice"] = {"type": "auto"}

        response = self._get_client().messages.create(**request)
        input_tokens, output_tokens = _anthropic_usage(response)
        return ModelProviderResponse(
            content=_anthropic_response_content(response),
            tool_calls=_anthropic_tool_calls(response),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=self.estimate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            latency_seconds=time.perf_counter() - started,
            raw_response=_safe_anthropic_response(response),
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ModelProviderConfigError(
                "The Anthropic Python SDK is not installed. Install the backend dependencies."
            ) from exc
        self._client = Anthropic(api_key=self._api_key)
        return self._client

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        pricing = ANTHROPIC_PRICING_USD_PER_MILLION_TOKENS.get(self.model_name)
        if pricing is None or input_tokens is None or output_tokens is None:
            return None
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return round(input_cost + output_cost, 8)


def _anthropic_input(messages: Sequence[ModelMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            system_parts.append(message.content)
            continue
        if message.role == "assistant":
            content = _anthropic_assistant_content(message.content)
            if content:
                _append_anthropic_message(items, role="assistant", content=content)
            continue
        if message.role == "tool":
            content = [_anthropic_tool_result(message.content)]
            _append_anthropic_message(items, role="user", content=content)
            continue
        if message.role != "user":
            raise ValueError(f"Unsupported model message role for Anthropic: {message.role}")
        _append_anthropic_message(
            items,
            role="user",
            content=[{"type": "text", "text": message.content}],
        )
    return "\n\n".join(system_parts), items


def _anthropic_assistant_content(content: str) -> list[dict[str, Any]]:
    payload = _agent_loop_assistant_payload(content)
    if payload is None:
        return [{"type": "text", "text": content}] if content else []

    blocks: list[dict[str, Any]] = []
    text_content = payload.get("content")
    if isinstance(text_content, str) and text_content:
        blocks.append({"type": "text", "text": text_content})
    for tool_call in payload.get("tool_calls", []):
        blocks.append(_anthropic_tool_use_input(tool_call))
    return blocks


def _anthropic_tool_use_input(tool_call: Any) -> dict[str, Any]:
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
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def _anthropic_tool_result(content: str) -> dict[str, Any]:
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
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": content,
        "is_error": payload.get("success") is False,
    }


def _append_anthropic_message(
    messages: list[dict[str, Any]],
    *,
    role: str,
    content: list[dict[str, Any]],
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
        return
    messages.append({"role": role, "content": content})


def _anthropic_tool_definition(tool: ToolDefinition) -> dict[str, Any]:
    input_schema = tool.input_schema or {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": input_schema,
    }


def _anthropic_response_content(response: Any) -> str:
    text_parts = [
        text
        for block in _as_sequence(_value(response, "content", []))
        if _value(block, "type") == "text" and isinstance((text := _value(block, "text")), str)
    ]
    return "\n".join(text_parts)


def _anthropic_tool_calls(response: Any) -> list[ModelToolCall]:
    tool_calls: list[ModelToolCall] = []
    for block in _as_sequence(_value(response, "content", [])):
        if _value(block, "type") != "tool_use":
            continue
        call_id = _value(block, "id")
        name = _value(block, "name")
        arguments = _value(block, "input", {})
        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError("Anthropic returned a tool call without an id.")
        if not isinstance(name, str) or not name:
            raise RuntimeError("Anthropic returned a tool call without a name.")
        if not isinstance(arguments, dict):
            raise TypeError("Anthropic returned tool arguments that were not an object.")
        tool_calls.append(ModelToolCall(id=call_id, name=name, arguments=dict(arguments)))
    return tool_calls


def _anthropic_usage(response: Any) -> tuple[int | None, int | None]:
    usage = _value(response, "usage")
    return _optional_int(_value(usage, "input_tokens")), _optional_int(
        _value(usage, "output_tokens")
    )


def _safe_anthropic_response(response: Any) -> dict[str, Any]:
    input_tokens, output_tokens = _anthropic_usage(response)
    content_types = [
        block_type
        for block in _as_sequence(_value(response, "content", []))[:100]
        if isinstance((block_type := _value(block, "type")), str)
    ]
    return {
        "id": _safe_scalar(_value(response, "id")),
        "model": _safe_scalar(_value(response, "model")),
        "stop_reason": _safe_scalar(_value(response, "stop_reason")),
        "stop_sequence": _safe_scalar(_value(response, "stop_sequence")),
        "content_types": content_types,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


class LocalModelProvider:
    provider_name = "local"

    def __init__(
        self,
        model_name: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        app_settings: Settings = settings,
        http_client: Any | None = None,
    ) -> None:
        if not app_settings.enable_local_model_calls:
            raise ModelProviderConfigError(
                "Local model calls are disabled. Set ENABLE_LOCAL_MODEL_CALLS=true to enable "
                "LocalModelProvider."
            )

        self.model_name = model_name or app_settings.local_model_default_model
        configured_endpoint = (
            endpoint if endpoint is not None else app_settings.local_model_endpoint
        )
        if not configured_endpoint or not configured_endpoint.strip():
            raise ModelProviderConfigError(
                "LOCAL_MODEL_ENDPOINT is required for LocalModelProvider."
            )
        self._endpoint = configured_endpoint.strip()
        self._api_key = api_key if api_key is not None else app_settings.local_model_api_key
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else app_settings.local_model_timeout_seconds
        )
        if self._timeout_seconds <= 0:
            raise ModelProviderConfigError("LOCAL_MODEL_TIMEOUT_SECONDS must be greater than zero.")
        self._http_client = http_client

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        started = time.perf_counter()
        request_body: dict[str, Any] = {
            "model": self.model_name,
            "messages": _local_model_messages(messages),
            "stream": False,
        }
        if tools:
            request_body["tools"] = [_local_model_tool_definition(tool) for tool in tools]
            request_body["tool_choice"] = "auto"

        response_data = self._post(request_body)
        input_tokens, output_tokens = _local_model_usage(response_data)
        return ModelProviderResponse(
            content=_local_model_content(response_data),
            tool_calls=_local_model_tool_calls(response_data),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=0.0,
            latency_seconds=time.perf_counter() - started,
            raw_response=_safe_local_model_response(response_data),
        )

    def _post(self, request_body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request_url = _local_model_chat_url(self._endpoint)

        try:
            if self._http_client is not None:
                response = self._http_client.post(
                    request_url,
                    json=request_body,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self._timeout_seconds) as client:
                    response = client.post(request_url, json=request_body, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ModelProviderRequestError(
                f"Local model request timed out after {self._timeout_seconds:g} seconds."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ModelProviderRequestError(
                f"Local model server returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderRequestError(
                f"Local model server request failed ({type(exc).__name__})."
            ) from exc

        try:
            response_data = response.json()
        except (TypeError, ValueError) as exc:
            raise ModelProviderRequestError("Local model server returned invalid JSON.") from exc
        if not isinstance(response_data, dict):
            raise ModelProviderRequestError("Local model server response must be a JSON object.")
        return response_data

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float:
        return 0.0


def _local_model_chat_url(endpoint: str) -> str:
    normalized = endpoint.strip().rstrip("/")
    if not normalized:
        raise ModelProviderConfigError("LOCAL_MODEL_ENDPOINT must not be blank.")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _local_model_messages(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant":
            assistant_payload = _agent_loop_assistant_payload(message.content)
            if assistant_payload is not None:
                item: dict[str, Any] = {
                    "role": "assistant",
                    "content": assistant_payload.get("content") or None,
                }
                tool_calls = assistant_payload.get("tool_calls", [])
                if tool_calls:
                    item["tool_calls"] = [
                        _local_model_tool_call_input(tool_call) for tool_call in tool_calls
                    ]
                if item["content"] is not None or item.get("tool_calls"):
                    items.append(item)
                continue
        if message.role == "tool":
            items.append(_local_model_tool_result_message(message.content))
            continue
        if message.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"Unsupported model message role for local model: {message.role}")
        items.append(
            {
                "role": "system" if message.role == "developer" else message.role,
                "content": message.content,
            }
        )
    return items


def _local_model_tool_call_input(tool_call: Any) -> dict[str, Any]:
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
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    }


def _local_model_tool_result_message(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent loop tool result must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise TypeError("Agent loop tool result must be an object.")
    call_id = payload.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("Agent loop tool result is missing a call id.")
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _local_model_tool_definition(tool: ToolDefinition) -> dict[str, Any]:
    parameters = tool.input_schema or {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def _local_model_choice(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelProviderRequestError("Local model response is missing choices[0].")
    return choices[0]


def _local_model_message(response: dict[str, Any]) -> dict[str, Any]:
    message = _local_model_choice(response).get("message")
    if not isinstance(message, dict):
        raise ModelProviderRequestError("Local model response is missing choices[0].message.")
    return message


def _local_model_content(response: dict[str, Any]) -> str:
    content = _local_model_message(response).get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ModelProviderRequestError("Local model response content must be text or null.")
    return content


def _local_model_tool_calls(response: dict[str, Any]) -> list[ModelToolCall]:
    raw_tool_calls = _local_model_message(response).get("tool_calls", [])
    if raw_tool_calls is None:
        return []
    if not isinstance(raw_tool_calls, list):
        raise ModelProviderRequestError("Local model response tool_calls must be a list.")

    tool_calls: list[ModelToolCall] = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict):
            raise ModelProviderRequestError("Local model returned an invalid tool call.")
        call_id = raw_tool_call.get("id")
        function = raw_tool_call.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise ModelProviderRequestError("Local model returned a tool call without an id.")
        if not isinstance(function, dict):
            raise ModelProviderRequestError("Local model tool call is missing function data.")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ModelProviderRequestError("Local model returned a tool call without a name.")
        arguments = _local_model_tool_arguments(function.get("arguments", {}))
        tool_calls.append(ModelToolCall(id=call_id, name=name, arguments=arguments))
    return tool_calls


def _local_model_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ModelProviderRequestError(
                "Local model returned invalid JSON tool arguments."
            ) from exc
    if not isinstance(arguments, dict):
        raise ModelProviderRequestError("Local model tool arguments must be an object.")
    return dict(arguments)


def _local_model_usage(response: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None, None
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return _optional_int(input_tokens), _optional_int(output_tokens)


def _safe_local_model_response(response: dict[str, Any]) -> dict[str, Any]:
    choice = _local_model_choice(response)
    message = _local_model_message(response)
    input_tokens, output_tokens = _local_model_usage(response)
    raw_tool_calls = message.get("tool_calls")
    tool_call_count = len(raw_tool_calls) if isinstance(raw_tool_calls, list) else 0
    return {
        "id": _safe_scalar(response.get("id")),
        "model": _safe_scalar(response.get("model")),
        "object": _safe_scalar(response.get("object")),
        "created": _safe_scalar(response.get("created")),
        "finish_reason": _safe_scalar(choice.get("finish_reason")),
        "tool_call_count": tool_call_count,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


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
