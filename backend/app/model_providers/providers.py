from __future__ import annotations

import time
from collections.abc import Sequence

from app.core.config import Settings, settings
from app.model_providers.base import (
    ModelMessage,
    ModelProviderConfigError,
    ModelProviderResponse,
    ModelToolCall,
    ToolDefinition,
)

OPENAI_ESTIMATED_PRICING_PER_1K = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
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
    ) -> None:
        self.model_name = model_name
        self._content = content
        self._tool_calls = list(tool_calls or [])

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        started = time.perf_counter()
        input_tokens = _estimate_tokens_from_messages(messages)
        output_tokens = _estimate_tokens(self._content)
        return ModelProviderResponse(
            content=self._content,
            tool_calls=list(self._tool_calls),
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
        model_name: str = "gpt-4o-mini",
        api_key: str | None = None,
        app_settings: Settings = settings,
    ) -> None:
        self.model_name = model_name
        self._api_key = api_key if api_key is not None else app_settings.openai_api_key
        if not self._api_key:
            raise ModelProviderConfigError("OPENAI_API_KEY is required for OpenAIProvider.")

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        raise NotImplementedError("OpenAIProvider API calls are not implemented yet.")

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        return _estimate_cost(
            OPENAI_ESTIMATED_PRICING_PER_1K.get(self.model_name),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


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
