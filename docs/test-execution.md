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
then runs post-patch tests after `submit_patch`. Setup failure fails the run immediately after logs
are stored. Post-patch test failure also marks the run failed.

## Current Limits

- Execution is still synchronous.
- The active workspace must still exist.
- The current implementation runs configured commands in the prepared workspace process context.
  Deeper Docker lifecycle integration is still planned.
