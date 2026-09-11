# Test Execution

Test execution records whether a benchmark repository passes its configured tests before and after
an agent patch.

The API does not accept arbitrary command text. Setup and test commands come from the
`BenchmarkTask` attached to the `AgentRun`.

## Phases

Supported phases:

```text
setup
baseline
post_patch
hidden_eval
```

`hidden_eval` is reserved for later evaluation-only checks.

## API

```text
POST /agent-runs/{run_id}/tests/baseline
POST /agent-runs/{run_id}/tests/post-patch
GET /agent-runs/{run_id}/tests
```

Both POST endpoints accept an optional timeout override:

```json
{
  "command_timeout_seconds": 120
}
```

The timeout is capped by `SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS`.

## Baseline Tests

`POST /agent-runs/{run_id}/tests/baseline` runs:

1. `BenchmarkTask.setup_commands` with phase `setup`.
2. `BenchmarkTask.test_commands` with phase `baseline`.

If setup fails, the run is marked `failed` and baseline commands are skipped. All setup logs remain
stored.

## Post-Patch Tests

`POST /agent-runs/{run_id}/tests/post-patch`:

1. Loads the run's `GeneratedPatch`, if one exists.
2. Applies it to the active workspace when it is not already applied.
3. Runs `BenchmarkTask.test_commands` with phase `post_patch`.
4. Marks the run `completed` when all post-patch tests pass, or `failed` when any fail.

No-op generated patches are allowed and recorded as `patch_status: "empty"`.

## Stored Results

Each command creates a `TestResult` row with:

- phase
- command
- passed
- exit code
- stdout
- stderr
- duration in seconds
- generated patch ID (nullable for baseline/setup and diagnostic tool runs)
- attempt number (1 for first submission, 2 for first repair; null for standalone/legacy results)

Output is capped by `SANDBOX_MAX_OUTPUT_BYTES`. When output exceeds the limit, the stored value keeps
the last bytes with a truncation prefix.

## Safety

- Commands are read from the benchmark task, not from the frontend.
- Test command execution rejects commands that are not configured on the benchmark task.
- Commands run inside the active agent-run workspace.
- Patch application reuses patch-management safety checks.
- Test execution is blocked for immutable run states: `completed`, `failed`, and `cancelled`.

## Orchestrator Integration

The agent-run orchestrator runs setup and baseline tests before the model tool loop,
then runs post-patch tests after each `submit_patch` when `run_tests_after_patch=true` (the default).
Setup failure fails the run immediately after logs are stored. During managed repairs, intermediate
post-patch failure leaves the run `running`, records every command result with its candidate ID and
attempt number, and returns safe feedback to the same model conversation. The public standalone
post-patch endpoint still finalizes its run after one phase.

`max_repair_attempts` defaults to 0 (range 0-5). `stop_on_first_passing_patch` and
`include_test_failure_feedback` default to true. All attempts share the original `max_steps`,
`max_tool_errors`, command timeout, output limit, and configured-command allowlist. Disabling the
agent's `run_tests` tool does not disable the orchestrated test phases. See
[agent repair configuration](agent-loop.md#repair-attempts) for requests and selection rules.

Failure feedback sent to the model is separately redacted and bounded to 4,096 UTF-8 bytes.
Disabling failure output includes only counts; stored bounded stdout/stderr remain available for
human inspection. Neither hidden evaluation results nor GoldPatch data enter repair feedback.

Tests that mutate tracked or unignored workspace files invalidate the candidate, which must then be
inspected and resubmitted. Configure repository ignore rules for ordinary build/test artifacts.
Patch validation/application failure produces an attempt event without invented command results.

The run's final candidate is exposed through `final_patch_id`, and `final_patch_passed_tests` is
true, false, or null (untested). Evaluation reads only the selected candidate's post-patch results;
earlier failed candidates and diagnostic `run_tests` results cannot overturn a passing final patch.
No post-patch commands means untested, not passing. Test history stays available across all attempts.

## Current Limits

- Execution is still synchronous.
- The active workspace must still exist.
- The current implementation runs configured commands in the prepared workspace process context.
  Deeper Docker lifecycle integration is still planned.
