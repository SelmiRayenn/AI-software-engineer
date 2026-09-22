# Agent Runs

Agent runs represent one attempt by a model-backed coding agent to solve a ready benchmark task.

## Start A Run

```text
POST /agent-runs/{benchmark_task_id}/start
```

Request:

```json
{
  "model_provider": "mock",
  "model_name": "mock-loop",
  "max_steps": 4,
  "max_tool_errors": 3,
  "command_timeout_seconds": 120,
  "include_issue_comments": true,
  "enable_test_tool": true,
  "run_mode": "tool_loop"
}
```

The task must already be `ready`. Draft, archived, running, completed, or failed benchmark tasks are rejected by the start endpoint.

## Run Configuration

Every start request is validated as an `AgentRunConfig`:

| Field | Default | Validation and behavior |
| --- | --- | --- |
| `model_provider` | `mock` | Non-empty provider registered by the provider factory |
| `model_name` | provider default | Stored as the resolved model name |
| `max_steps` | `4` | Between 1 and 50 model/tool steps |
| `max_tool_errors` | `3` | Between 1 and 20 cumulative tool failures |
| `command_timeout_seconds` | `120` | Between 1 and 600 seconds |
| `include_issue_comments` | `true` | Includes public issue comments in model context |
| `enable_test_tool` | `true` | Advertises and registers `run_tests` for the model |
| `run_mode` | `tool_loop` | `tool_loop` or deterministic `scripted` mode |

`scripted` mode requires the mock provider. It uses the mock provider's deterministic tool sequence
and is intended for development, smoke tests, and reproducible demonstrations. `tool_loop` consumes
structured decisions from the selected provider.

The backend normalizes provider/model defaults and stores the complete configuration in an
`agent_run_configured` event before execution. No database migration is required, and older run
rows remain readable.

## Prompt Preview

Both the start response and `GET /agent-runs/{run_id}` include:

```json
{
  "run_config": {
    "model_provider": "mock",
    "model_name": "mock-loop",
    "max_steps": 4,
    "max_tool_errors": 3,
    "command_timeout_seconds": 120,
    "include_issue_comments": true,
    "enable_test_tool": true,
    "run_mode": "tool_loop"
  },
  "prompt_preview": {
    "system_prompt": "...",
    "developer_safety_prompt": "...",
    "issue_context_prompt": "...",
    "tool_use_instructions": "...",
    "patch_submission_instructions": "..."
  }
}
```

The preview is the organized prompt content used to build the provider messages. Common API key,
token, password, and authorization patterns are redacted before storage or response. Legacy runs
without an `agent_run_configured` event return `null` for both fields.

## Agent Loop Flow

The orchestrator currently:

1. Creates an `AgentRun`.
2. Prepares a temporary sandbox workspace by cloning the task repository and checking out the task base commit.
3. Initializes the selected model provider.
4. Initializes controlled workspace tools.
5. Sends only agent-visible task context and tool definitions to the provider.
6. Executes validated model tool calls and returns structured observations to the model.
7. Repeats until `submit_patch`, the step limit, the tool-error limit, or a provider failure.
8. Runs post-patch tests after a successful patch submission.
9. Marks the run completed or failed and calculates metrics for completed runs.

The default provider is `mock`, so no real LLM call is required.

Prepared runs record `workspace_id` and `workspace_path`. Patch inspection and application endpoints
use that recorded workspace when it is still available.

Setup and baseline tests run before the agent loop. If setup fails, the run is marked
failed immediately after logs are stored.

## Statuses

Agent run statuses:

```text
queued
running
completed
failed
cancelled
```

The endpoint returns a structured trace with step names, success flags, durations, summaries, generated patch id, changed files, and any failure message.

## Run Detail

```text
GET /agent-runs/{run_id}
```

The detail endpoint returns the run status, model provider/name, timestamps, linked benchmark task
issue title, repository owner/name/url, generated patch review status, generated patch changed
files, and metric summary when metrics exist.

This response is safe for the dashboard and agent-facing review UI. It does not include
`GoldPatch.patch_text` or hidden gold changed files.

Test command logs are available through:

```text
GET /agent-runs/{run_id}/tests
```

## Run Trace

```text
GET /agent-runs/{run_id}/trace
```

The trace endpoint returns all stored `AgentEvent` records ordered by creation time and event ID.
Each event includes a normalized summary, severity, related tool name and repository-relative file
paths when available, plus a sanitized payload suitable for operator debugging.

The response also includes compact artifact summaries:

- Generated patch versions, selection state, changed files, and review status. Patch text is not
  included.
- Setup, baseline, post-patch, and hidden-evaluation phase counts and durations. Hidden commands
  and output are not included.
- Persisted failure category and redacted human-readable summary.
- Evaluation metric summary, including visible and aggregate hidden-evaluation outcomes.

Trace payloads are sanitized again when read, even though normal event writers already sanitize
their logs. Secret-bearing keys and common API key/token patterns are redacted. Long strings,
lists, mappings, and deeply nested structures are bounded. Gold patch/test payloads and hidden-test
definitions are removed. Hidden evaluation events expose aggregate status only.

The trace is an operator-facing audit view. It does not mutate the run, execute tools, rerun tests,
or expose the benchmark's trusted solution data.

## Safety

Prompt templates live in `backend/app/agents/prompts.py`. They cover the system role,
developer/safety constraints, issue context, tool-use rules, and patch-submission format.

The orchestrator does not read `GoldPatch` data. It builds model messages from repository metadata,
the issue title/body/optional public comments, configured tests, allowed tools, run constraints, and
the base commit only.

All repository inspection and patch submission goes through the controlled tool layer. Unknown or
malformed calls are rejected, and repeated tool errors stop the loop. See `docs/agent-loop.md` for
the conversation contract and event trace.

## Current Limits

- OpenAI calls are synchronous, opt-in, and disabled by default. Anthropic and local model API
  calls are not implemented yet.
- The default mock provider submits a no-op patch through the real loop for deterministic testing.
