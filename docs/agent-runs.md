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
  "model_name": "scripted-mock",
  "max_steps": 4,
  "command_timeout_seconds": 120
}
```

The task must already be `ready`. Draft, archived, running, completed, or failed benchmark tasks are rejected by the start endpoint.

## Current Scripted Flow

This is still a skeleton, not the full autonomous coding loop. The orchestrator currently:

1. Creates an `AgentRun`.
2. Prepares a temporary sandbox workspace by cloning the task repository and checking out the task base commit.
3. Initializes the selected model provider.
4. Sends only agent-visible task context to the provider.
5. Initializes controlled workspace tools.
6. Lists files.
7. Reads `README` or the first available source file.
8. Reads the current diff.
9. Submits the current diff as a generated patch.
10. Runs post-patch tests.
11. Marks the run completed or failed.

The default provider is `mock`, so no real LLM call is required.

Prepared runs record `workspace_id` and `workspace_path`. Patch inspection and application endpoints
use that recorded workspace when it is still available.

Setup and baseline tests run before the scripted tool sequence. If setup fails, the run is marked
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

## Safety

The orchestrator does not read `GoldPatch` data. It builds model messages from repository metadata, issue title/body, and the base commit only.

All repository inspection and patch submission goes through the controlled tool layer. Those tools enforce workspace path boundaries, block hidden gold-solution paths, require exact allowed test commands, and log each tool call to `agent_events`.

## Current Limits

- The run loop is scripted and no-op by design.
- Real OpenAI, Anthropic, and local model API calls are not implemented yet.
- Workspace preparation currently uses a retained Git checkout. Deeper Docker lifecycle and cleanup
  policies will be added when the autonomous loop is implemented.
