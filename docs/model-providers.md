# Model Providers

The backend uses a provider abstraction so agent orchestration is not tied to one LLM vendor.

## Interface

Providers implement:

- `provider_name`
- `model_name`
- `generate_response(messages, tools=None)`
- `estimate_cost(input_tokens=..., output_tokens=...)`

`generate_response` returns a normalized `ModelProviderResponse`:

- `content`
- `tool_calls`
- `input_tokens`
- `output_tokens`
- `estimated_cost`
- `latency_seconds`
- `raw_response`

Messages use the lightweight `ModelMessage` shape. Tool definitions use `ToolDefinition`, and returned tool calls use `ModelToolCall`.

## Providers

Current providers:

- `MockModelProvider`: deterministic local/test provider.
- `OpenAIProvider`: configuration and cost-estimation skeleton.
- `AnthropicProvider`: configuration and cost-estimation skeleton.
- `LocalModelProvider`: endpoint-backed skeleton for local runtimes.

The OpenAI, Anthropic, and local providers intentionally do not make real API calls yet. They validate configuration and expose the shared interface so the agent loop can be wired without hardcoding a provider.

## Configuration

```text
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
LOCAL_MODEL_ENDPOINT=
```

The mock provider requires no credentials. OpenAI and Anthropic providers raise a clear configuration error when their API keys are missing. The local provider raises a configuration error when `LOCAL_MODEL_ENDPOINT` is missing.

## Factory

Use `ModelProviderFactory` or `create_model_provider`:

```python
from app.model_providers import create_model_provider

provider = create_model_provider("mock", model_name="mock-dev")
response = provider.generate_response(messages)
```

Supported provider names:

- `mock`
- `openai`
- `anthropic`
- `local`

## Current Limits

- Real provider API calls are not implemented.
- Pricing is a small placeholder table for early estimates only.
- Tool calls are normalized structurally, but provider-specific tool call translation will be added when real adapters are implemented.
