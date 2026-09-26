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
GET /agent-runs/{run_id}/test-failure-analysis
POST /agent-runs/{run_id}/tests/select-targeted
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

## Targeted Test Selection

Targeted post-patch testing is disabled by default. Run configuration accepts:

```json
{
  "enable_targeted_tests": false,
  "targeted_tests_max_commands": 3,
  "targeted_tests_trusted_gold_files": false
}
```

When enabled, the selector considers changed files, ranked candidate files, successful read and
retrieval events, repository index paths, repository language, and recognized test-runner commands.
Changed test files are preferred. Source files can map to indexed tests by normalized filename, for
example `src/calculator.py` to `tests/test_calculator.py`.

The selector can safely narrow existing commands or append validated repository-relative test paths
to recognized pytest, Jest, or Vitest commands. It does not derive commands from shell pipelines,
redirections, malformed commands, or unknown runners. When no safe subset exists, all configured
test commands are returned unchanged. `targeted_tests_max_commands` limits only targeted commands;
it never truncates the full-suite fallback.

`POST /agent-runs/{run_id}/tests/select-targeted` accepts an optional command limit and returns
`selected_commands`, `selection_reason`, `confidence`, and `fallback_to_full_suite`. Every selection
is recorded as a `targeted_tests_selected` AgentEvent. Baseline and hidden-evaluation phases are
unchanged; targeting applies only to visible post-patch execution.

GoldPatch test-file hints are ignored by default. Setting
`targeted_tests_trusted_gold_files=true` requires the trusted operator start/API path. Gold hints may
only select a command already present in `BenchmarkTask.test_commands`; they never generate a new
command containing a gold-only path. Normal agent runs, prompts, and public selection requests do
not load gold files.

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

## Structured Failure Analysis

Every failed setup, baseline, post-patch, or hidden-evaluation result produces an idempotent
`test_failure_analysis` AgentEvent. The analysis records the failed command, phase, exit code,
concise summary, likely test names, bounded assertion/error excerpts, bounded stack snippets,
affected paths, timeout/command-failure signals, and whether the source output was truncated.

`GET /agent-runs/{run_id}/test-failure-analysis` returns the ordered analyses for a run. Common
credential patterns are redacted before persistence, fields and excerpts have strict size/count
limits, and repeated reads update the existing event instead of creating duplicates.

Hidden evaluation failures are always collapsed into one public aggregate. The response exposes
only phase, failed/total command counts, timeout/truncation signals, and a restricted summary. It
does not expose hidden commands, test names, file paths, assertions, stacks, or test payloads.

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

Failure feedback sent to the model is rendered from the same structured analyses, then separately
bounded by `max_failure_feedback_chars` (default 4096, range 512-16384) and 4,096 UTF-8 bytes.
Redaction precedes truncation; truncated feedback includes a marker. Disabling detailed failure feedback includes only counts, phase,
exit code, and timeout/truncation signals; stored bounded stdout/stderr remain available for human
inspection. Neither hidden evaluation details nor GoldPatch data enter repair feedback.

For retries after failed visible tests, `require_hypothesis_update_after_failure=true` requires
the agent to update or confirm its hypothesis before submitting the next patch.
`require_plan_update_after_failure=false` and `require_candidate_update_after_failure=false`
can be enabled to require a new accepted plan before editing/submitting and a new evidence-backed
candidate ranking before editing. Otherwise feedback recommends revising them when strategy or
implicated files change. Candidate rankings reopen for this repair window and freeze at the next
successful write. The initial localization ranking is preserved for analytics.

Each completed repair attempt links its structured failure analysis event IDs, the reasoning
revisions used, attempted patch version, and a `test_phase_result` summary. The next attempt links
the source analyses through `repair_source_analysis_event_ids`; no raw output is added to these
metadata fields. They are inspectable in the run trace. Rejected gates spend tool-error/step budget,
not patch attempts. Repeated failures require repeated reasoning updates. Disabled repairs retain
their one-submission behavior, and setup, baseline, hidden-evaluation and standalone test endpoints
are unaffected. Final passing selection clears the run failure state without erasing earlier logs.

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

## Baseline Flakiness Checks

```text
POST /benchmark-tasks/{task_id}/flakiness-check
GET  /benchmark-tasks/{task_id}/flakiness-checks
```

The POST body accepts `repetitions` (default 3, range 2-20), `stop_on_first_failure`, and an
optional `command_timeout_seconds`. It does not accept command text. The service clones the task's
repository at `base_commit`, runs configured setup commands once, then executes the configured test
commands for each repetition. It uses the Docker sandbox's memory, CPU, privilege, timeout, output
capture, workspace-root, and retention controls.

Each check stores aggregate pass/fail counts, inconsistency, average repetition duration, setup
results, workspace retention state, and a row for each completed repetition. `stable` means every
completed repetition had the same outcome, including consistently failing tests; use the pass/fail
counts to distinguish a healthy baseline from a consistently broken one. `flaky` means both passing
and failing repetitions were observed. Clone, checkout, Docker, or empty-result failures are
`inconclusive`, as is an early stop before enough consistent repetitions complete. Setup failure is
`failed_setup`.
