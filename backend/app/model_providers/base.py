from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelProviderConfigError(RuntimeError):
    pass


class ModelProviderRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelProviderResponse:
    content: str
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    latency_seconds: float = 0.0
    raw_response: Any | None = None


class ModelProvider(Protocol):
    provider_name: str
    model_name: str

    def generate_response(
        self,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> ModelProviderResponse:
        """Generate a provider-normalized model response."""

    def estimate_cost(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float | None:
        """Return an estimated request cost when pricing data is available."""
