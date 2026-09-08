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
- `OpenAIProvider`: opt-in OpenAI Responses API adapter with function-tool support.
- `AnthropicProvider`: configuration and cost-estimation skeleton.
- `LocalModelProvider`: endpoint-backed skeleton for local runtimes.

Anthropic and local providers remain skeletons. The OpenAI provider is implemented, but real
network calls remain disabled unless an operator explicitly enables them.

## Configuration

```text
ENABLE_REAL_MODEL_CALLS=false
OPENAI_API_KEY=
OPENAI_DEFAULT_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=
LOCAL_MODEL_ENDPOINT=
```

The mock provider requires no credentials and remains available while real calls are disabled.
OpenAI requires both `ENABLE_REAL_MODEL_CALLS=true` and a non-empty `OPENAI_API_KEY`.
`OPENAI_DEFAULT_MODEL` is used when a run does not supply `model_name`. Anthropic and local
providers retain their existing credential validation.

## Enabling OpenAI Runs

Set these values only in a local `.env` or deployment secret store:

```text
ENABLE_REAL_MODEL_CALLS=true
OPENAI_API_KEY=your-secret-key
OPENAI_DEFAULT_MODEL=gpt-4o-mini
```

Then start a run with `model_provider` set to `openai`, `run_mode` set to `tool_loop`,
and an optional `model_name`. Do not commit `.env` or API keys. The backend sends requests with
OpenAI response storage disabled and exposes the same controlled tools used by mock runs.

The adapter translates platform messages and function definitions to the OpenAI Responses API.
It normalizes response text, function calls, token usage, latency, and estimated cost back into
`ModelProviderResponse`. The `raw_response` field is intentionally a safe summary containing only
the response id, model, status, output item types, and aggregate token counts.

References: [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
and [GPT-4o mini model](https://developers.openai.com/api/docs/models/gpt-4o-mini).

## Cost Estimates

`OPENAI_PRICING_USD_PER_MILLION_TOKENS` in
`backend/app/model_providers/providers.py` is the local pricing table. It currently includes
`gpt-4o-mini` at USD 0.15 per million input tokens and USD 0.60 per million output tokens. Update
the table when provider pricing changes. Models absent from the table return `estimated_cost=null`
rather than guessing.

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

- OpenAI calls are synchronous and use the Responses API without streaming.
- Cost is an estimate based on the local pricing table; billed cost remains provider-authoritative.
- Anthropic and local model API calls are not implemented yet.
- Tests use injected mock SDK clients and never make real provider calls.

## Embeddings Providers

`backend/app/embeddings/` defines a separate `EmbeddingsProvider` interface with `provider_name`,
`model_name`, `dimensions`, and `embed_texts(texts: list[str]) -> list[list[float]]`. Vectors are
returned in input order. `last_usage` reports input tokens, estimated USD cost, and latency for the
most recent call; unavailable token/cost values are null. Embedding usage events are kept separate
from coding-model token metrics.

- `MockEmbeddingsProvider` returns deterministic normalized hashed-token vectors (64 dimensions
  unless configured) and zero token/cost usage. It supports development and tests, not semantic
  quality measurement.
- `OpenAIEmbeddingsProvider` uses the installed SDK's `client.embeddings.create`, explicitly asks
  for float vectors, normalizes response order, validates dimensions and finite nonzero vectors,
  and reports prompt tokens. Calls use a 30-second timeout and no automatic retries.
- `LocalEmbeddingsProvider` is a skeleton and raises a clear configuration error; it makes no
  network requests.

`create_embeddings_provider()` selects `EMBEDDINGS_PROVIDER`, which defaults to `mock`. The real
embedding flag is independent of `ENABLE_REAL_MODEL_CALLS`. OpenAI construction and every call
require `ENABLE_REAL_EMBEDDINGS=true` and a non-empty `OPENAI_API_KEY`, even with an injected client.

To opt into OpenAI embeddings, configure these deployment/local environment values:

```text
ENABLE_REAL_EMBEDDINGS=true
EMBEDDINGS_PROVIDER=openai
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_BATCH_SIZE=32
EMBEDDING_VECTOR_DIMENSIONS=512
EMBEDDING_AUTO_BUILD=false
```

Supply `OPENAI_API_KEY` through the existing local secret configuration. Restart the backend,
apply migrations, and explicitly build the run's embeddings using
`POST /agent-runs/{run_id}/index/embeddings`. To prepare embeddings during new runs, additionally
set `EMBEDDING_AUTO_BUILD=true`. This sends accepted repository chunks to OpenAI; subsequent
`semantic=true` searches send the query. Ordinary lexical searches and index builds make no
embedding calls. Mock retrieval remains available while the real flag is false.

The optional dimensions field is omitted from OpenAI requests when unset. Provider-supported
dimensions depend on the selected model. Local limits enforce 8,000 UTF-8 bytes per text and
128,000 per request, including context, plus the configured batch count. Details and fallback
behavior are documented in [repository indexing](repository-indexing.md#optional-embeddings).

`OPENAI_EMBEDDING_USD_PER_MILLION_TOKENS` in `backend/app/embeddings/providers.py` currently
estimates `text-embedding-3-small` at USD 0.02 per million input tokens (checked September 5, 2026).
Unknown model pricing returns null. Tests inject fake SDK responses and block SDK construction;
they do not contact OpenAI or require credentials.

References: [OpenAI embeddings API](https://developers.openai.com/api/reference/python/resources/embeddings/methods/create)
and [text-embedding-3-small pricing](https://developers.openai.com/api/docs/models/text-embedding-3-small).
