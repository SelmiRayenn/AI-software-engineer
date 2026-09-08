# Agent Loop

The agent loop turns model responses into controlled repository actions. It does not give a model
shell access, Docker access, database access, approval rights, or publishing credentials.

The five prompt sections are maintained in `backend/app/agents/prompts.py`: system,
developer/safety, issue context, tool-use instructions, and patch-submission instructions. The
renderer receives only agent-visible task fields and explicit run configuration.

## Execution Flow

For each ready benchmark task, the orchestrator prepares an isolated checkout and runs configured
setup and baseline test commands. Managed sandboxes are indexed before the model is called. It then
starts `AgentLoop` with:

- agent-visible repository and issue context
- the selected model provider
- up to eight registered workspace tools
- a maximum model/tool step count
- a maximum cumulative tool error count

Each model response may request structured tool calls. The loop validates every call against the
registered tool name and its argument contract, executes it through `AgentWorkspaceTools`, and adds
a JSON tool observation to the conversation. The process continues until one of these conditions:

- `submit_patch` stores the workspace diff and ends the loop successfully
- `max_steps` is reached
- `max_tool_errors` is reached
- a provider call fails

Only a submitted patch proceeds to post-patch tests and evaluation. Limit and error exits mark the
agent run as failed.

## Start A Run

```text
POST /agent-runs/{benchmark_task_id}/start
```

```json
{
  "model_provider": "mock",
  "model_name": "mock-loop",
  "max_steps": 8,
  "max_tool_errors": 3,
  "command_timeout_seconds": 120,
  "include_issue_comments": true,
  "enable_test_tool": true,
  "run_mode": "tool_loop"
}
```

`max_steps` is capped at 50 and `max_tool_errors` is capped at 20 by request validation. Command
timeouts remain bounded by the sandbox configuration.

Set `run_mode` to `scripted` with the mock provider for a deterministic flow. Set
`enable_test_tool` to `false` to remove `run_tests` from both the advertised definitions and the
executable tool registry. Setup, baseline, and post-patch orchestration phases remain enabled.

## Registered Tools

The model can request only:

- `retrieve_relevant_files`
- `list_files`
- `search_code`
- `read_file`
- `write_file`
- `run_tests`
- `get_diff`
- `submit_patch`

Each tool retains the workspace boundary, path traversal, hidden gold path, file-size, output-size,
and configured-test-command checks documented in the controlled tool and sandbox services.

`retrieve_relevant_files` searches only the deterministic index associated with the current run.
It returns ranked path, language, file kind, matching symbols and snippets, score, and byte size. The
query must contain 1-200 characters; `limit` defaults to 10 and is capped at 50. Retrieval results
are logged as files read so localization metrics can account for index-assisted inspection.
The optional `semantic=true` argument uses compatible chunk embeddings and adds semantic scores
to lexical ranking. Missing embeddings or an unavailable provider keep lexical search available;
the observation reports the fallback reason. Operators can set `EMBEDDING_AUTO_BUILD=true` to
prepare embeddings before the loop. Real embedding calls additionally require their own explicit
`ENABLE_REAL_EMBEDDINGS=true` flag. See [repository indexing](repository-indexing.md#optional-embeddings).

## Instructions And Hidden Data

The initial conversation tells the model to retrieve likely files first, inspect before editing,
keep changes small and targeted, avoid unrelated files, run tests when useful, and submit only when
ready for human review. It explicitly forbids requesting gold solution data or publishing changes.

The task context contains repository metadata, base commit, issue title/body, and optionally public
issue comments. Tool instructions list the enabled tools, configured tests, limits, and exact patch
submission shape. The loop never queries or serializes `GoldPatch`, its patch text, its changed
files, the historical fix commit, or the linked fix PR.

## Events

The loop records:

- `model_call_started`
- `model_call_completed`
- `tool_call_requested`
- `tool_call_completed`
- `tool_call_failed`
- `patch_submitted`
- `step_limit_reached`

The existing `model_response` event is also retained for metrics compatibility, and controlled
tools continue to emit `agent_tool_call`. The normalized run config and redacted prompt preview are
stored once in `agent_run_configured`. Logged content is size-limited and common credential keys and
token patterns are redacted. Full tool observations exist only in the in-memory provider
conversation for the active run.

## Mock Provider

`MockModelProvider` supports an explicit ordered response sequence for deterministic service tests.
Without one, it drives a four-turn no-op flow: list files, read a README or first listed file, inspect
the diff, and submit it. This exercises the real loop without making an external API call.

OpenAI uses the common response contract through an opt-in Responses API adapter. Real calls are
disabled by default. Anthropic and local network adapters remain implementation placeholders.
