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

- `submit_patch` produces an accepted candidate, or its configured repair budget is exhausted
- `max_steps` is reached
- `max_tool_errors` is reached
- a provider call fails

Each submitted patch proceeds to validation and optional post-patch tests. Failed candidates can
return feedback to the same conversation. A final accepted candidate is selected before evaluation.

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
  "run_mode": "tool_loop",
  "max_repair_attempts": 1,
  "run_tests_after_patch": true,
  "stop_on_first_passing_patch": true,
  "include_test_failure_feedback": true
}
```

`max_steps` is capped at 50 and `max_tool_errors` is capped at 20 by request validation. Command
timeouts remain bounded by the sandbox configuration.

Set `run_mode` to `scripted` with the mock provider for a deterministic flow. Set
`enable_test_tool` to `false` to remove `run_tests` from both the advertised definitions and the
executable tool registry. Setup and baseline remain enabled; `run_tests_after_patch` independently
controls automatic post-patch tests.

## Repair Attempts

`max_repair_attempts` defaults to 0 and accepts integers from 0 through 5. It counts additional
submission attempts after the first, including invalid submissions. A limit of 2 allows at most
three submissions. It does not add steps: both model calls and tool calls still share the original
`max_steps` budget, and invalid patches count against cumulative `max_tool_errors`. Increase
`max_steps` when enabling repairs. No counter resets between attempts.

`run_tests_after_patch` defaults to true. After a valid submission, only the task's configured test
commands run. On failure, the agent receives a bounded summary and may inspect, edit, and resubmit
within the remaining limits. Invalid patch size, application, or safety failures also permit repair;
infrastructure/provider errors stop execution. Tests remain in the same prepared workspace, and
setup/baseline execute once. Intermediate failures keep the run `running`.

`stop_on_first_passing_patch` defaults to true. When false, the model can continue submitting within
the same repair budget even after a passing candidate. Final selection prefers the latest passing
candidate, otherwise the latest stored candidate. Later failed or unfinished edits are safely
reverted through checked workspace diffs when restoring a selected candidate. Restoration failure
fails the run rather than deleting arbitrary files. A passing candidate can survive a step or tool
error limit; an unrecoverable provider/test/workspace error still marks the run failed.

When post-patch testing is disabled or no test commands are configured, a valid candidate is
selected immediately with `final_patch_passed_tests=null`. This does not claim test success:
`EvaluationMetric.tests_passed` remains false. Empty/no-op patches retain existing behavior and
have `patch_applied=false` in metrics.

`include_test_failure_feedback` defaults to true. Failure summaries include up to five failed
command outputs, redact common credential patterns, and are limited to 4,096 UTF-8 bytes. Set it
to false to return only failure counts without command output. Raw bounded command logs remain in
`TestResult` for human inspection. Test text is untrusted data and cannot authorize another command.

Every stored submission gets an immutable `GeneratedPatch` ID and increasing `version`, even if
the text repeats. Each version keeps its own human review. Rejected attempts that fail before
storage appear only in events and do not consume a patch version. Attempts are numbered from 1.
Any remaining tool calls in the same model response after submission receive skipped observations;
the next model response sees the assessment before it can edit again.

Start/detail/list responses expose `repair_attempts_used`, `final_patch_id`,
`final_patch_passed_tests`, and `failure_summary`. The repair count records additional submissions
actually attempted, not merely offered retries. Successful final selection clears the failure
summary while attempt events retain earlier failures. `GET /agent-runs/{run_id}/patch` returns the
selected patch (or latest candidate before selection); `GET /agent-runs/{run_id}/patches` lists
version history. `GET /agent-runs/{run_id}/tests` includes patch IDs and attempt numbers.

The same configuration is accepted by model comparison requests and stored with each run.
Metrics use the selected patch and its post-patch results, excluding earlier candidates and
ad-hoc `run_tests` observations. Tokens, cost, elapsed time, and inspected files still cover the
entire run. Completed and terminal failed repair runs with a selected patch are evaluated.

Apply Alembic revision `20260909_0004` before starting the updated backend. It retains existing
patch IDs, reviews, and logs, and assigns existing patches version 1. Downgrading is refused when
multiple versions exist for a run, because the old schema cannot preserve that history.

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
- `repair_attempt_started`
- `repair_attempt_completed` (candidate ID/version, outcome, and test-result IDs)
- `repair_limit_reached`
- `final_patch_selected`

The existing `model_response` event is also retained for metrics compatibility, and controlled
tools continue to emit `agent_tool_call`. The normalized run config and redacted prompt preview are
stored once in `agent_run_configured`. Logged content is size-limited and common credential keys and
token patterns are redacted. Full tool observations exist only in the in-memory provider
conversation for the active run.

`GET /analytics/tool-usage` turns these events into selection and reliability metrics. Request and
outcome events are authoritative for current loop runs, while `agent_tool_call` is a fallback for
legacy/scripted runs. This avoids double-counting an executed tool while still counting unknown,
malformed, and incomplete requests that never reach a registered tool. See
[aggregate analytics](analytics.md#tool-usage) for metric definitions and filters.

## Mock Provider

`MockModelProvider` supports an explicit ordered response sequence for deterministic service tests.
Without one, it drives a four-turn no-op flow: list files, read a README or first listed file, inspect
the diff, and submit it. This exercises the real loop without making an external API call.

OpenAI uses the common response contract through an opt-in Responses API adapter. Real calls are
disabled by default. Anthropic and local adapters use the same loop and their existing feature
flags. See [model providers](model-providers.md) for configuration.
