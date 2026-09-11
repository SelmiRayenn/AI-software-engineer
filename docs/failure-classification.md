# Failure Classification

Each terminal failed or cancelled `AgentRun` has one queryable `AgentRunFailure` record. The
record is updated in place if a more precise terminal cause becomes available, so a run never
accumulates conflicting classifications.

## API

Run details include:

```json
{
  "status": "failed",
  "failure_category": "model_provider_error",
  "failure_summary": "Model provider call failed: service unavailable."
}
```

The complete stored classification is available at:

```text
GET /agent-runs/{run_id}/failure
```

The response includes `id`, `agent_run_id`, `category`, `human_readable_summary`, optional
`source_event_id`, and `created_at`. A missing run returns 404. Successful or still-active runs
without a failure return 404 because there is no failure to report.

Older failed/cancelled runs without a row are classified lazily from their event trace and stored
when the detail or failure endpoint is read. Summaries are redacted and limited to 2,000
characters before persistence.

## Categories

| Category | Meaning |
| --- | --- |
| `task_not_ready` | A prepared run could not start because its task was not ready. A rejected start request that creates no run has no failure row. |
| `repository_checkout_failed` | Clone, checkout, or workspace preparation failed. |
| `docker_unavailable` | Docker Desktop/daemon/socket was unavailable. |
| `setup_failed` | A configured setup command failed. |
| `baseline_tests_failed` | Baseline execution terminated the run. An expected failing historical baseline that does not terminate execution remains test evidence, not a run failure. |
| `model_provider_error` | Provider construction or model generation failed. |
| `malformed_tool_call` | A terminal model tool call did not match the controlled contract. |
| `unknown_tool` | A terminal model request named an unregistered tool. |
| `tool_error_limit_reached` | Generic controlled-tool failures exhausted the configured limit. |
| `patch_generation_failed` | No valid candidate patch was produced. |
| `patch_apply_failed` | A candidate did not apply cleanly. |
| `patch_quality_blocked` | Patch quality hard limits or blocked file policy rejected the candidate. |
| `post_patch_tests_failed` | The final candidate failed configured post-patch tests without repair exhaustion. |
| `max_steps_reached` | The shared model/tool step limit was reached. |
| `max_repair_attempts_reached` | Configured repair attempts were exhausted. |
| `timeout` | A workspace, setup, test, or other bounded operation timed out. |
| `cancelled` | The run was cancelled. |
| `unknown` | The run failed without enough evidence for a more specific category. |

Specific causes take precedence over generic limits where useful. For example, one unknown tool
that exhausts `max_tool_errors=1` is classified as `unknown_tool`; otherwise generic repeated tool
failures use `tool_error_limit_reached`.

## Integration

The orchestrator persists terminal loop, workspace, provider, setup, repair, patch, and timeout
outcomes. Direct setup/post-patch execution also classifies runs when it marks them failed. Patch
application and quality failures remain recoverable during the repair loop and are persisted only
if the run ultimately fails.

Model comparisons return `failure_category` and `failure_summary` for each failed model. One
classified model failure does not stop the remaining comparison runs.

`source_event_id` points to the event that finalized or best explains the classification when one
is available. Event and failure rows are deleted with their run; deleting a source event sets the
reference to null without deleting the failure record.
