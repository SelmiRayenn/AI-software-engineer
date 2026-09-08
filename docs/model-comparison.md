# Model Comparison

Compare 2-10 distinct provider/model pairs against the same ready benchmark task. Each comparison
gets a UUID and each model gets its own AgentRun, workspace, tool trace, generated patch, test
results, and EvaluationMetric. Models run sequentially in request order.

## Start a Comparison

```http
POST /benchmark-tasks/{task_id}/compare-models
Content-Type: application/json

{
  "models": [
    {"model_provider": "mock", "model_name": "mock-alpha"},
    {"model_provider": "mock", "model_name": "mock-beta"}
  ],
  "max_steps": 4,
  "max_tool_errors": 3,
  "command_timeout_seconds": 120,
  "include_issue_comments": false,
  "enable_test_tool": true,
  "run_mode": "tool_loop"
}
```

Both model fields are required. Aliases such as `mock-alpha` use the existing deterministic mock
flow; changing a mock model's name does not change its behavior. They can complete with empty
patches, which are ineligible for passing-model awards.

Shared options use the same limits as individual agent runs: 1-50 steps, 1-20 tool errors, and
1-600 seconds per command. `scripted` mode accepts mock providers only. Duplicate provider/model
pairs, blank names, invalid limits, and unknown request fields return 422 before runs are created.
Missing tasks return 404; non-ready tasks return 409.

The POST waits for all runs and returns 200 with individual outcomes, even if some or all models
fail. A disabled or unconfigured provider still gets a failed AgentRun and an audit trail. It
does not prepare a workspace or stop the remaining models.

## Read Results

```http
GET /benchmark-tasks/{task_id}/model-comparison
GET /benchmark-tasks/{task_id}/model-comparison?comparison_id={comparison_id}
```

The first form returns the latest comparison requested for that task. Supply `comparison_id` to
read an earlier comparison. Ordinary single-model runs are excluded. Unknown comparisons, or a
comparison belonging to another task, return 404. GET reads stored runs/events/metrics and never
starts models or recalculates metrics.

The response contains `comparison_id`, `benchmark_task_id`, `created_at`, `status`, `runs`, and
`aggregates`. Overall status is `queued`, `running`, `completed`, `partial_failure`, or `failed`.
Overall `completed` means all individual runs completed; it does not imply that every model
produced a passing, non-empty patch.

Each entry in `runs`, ordered as requested, contains:

- `run_id`, `provider`, `model`, and `status`.
- `metric_id`, `patch_applied`, `tests_passed`, and `file_localization_score`.
- `modified_files_count`, `unrelated_files_count`, and `tokens_used`.
- `estimated_cost` in USD and `execution_time_seconds`.
- `failure_reason`, redacted and bounded, or null on success.

Patch text, changed-file lists, prompts, issue bodies, and gold solution data are not part of the
comparison read model. Existing run detail, patch, and test endpoints can inspect the generated
output using `run_id`. Human approval is still required before any future publication step.

## Ranking and Totals

A passing candidate must have `status=completed`, `patch_applied=true`, and `tests_passed=true`.
This excludes failed runs, untested runs, empty/no-op patches, and unapplied patches.

`aggregates` contains:

- `best_passing_model`: highest localization score, then fewest unrelated files, fewest modified
  files, lowest estimated cost, and shortest execution time.
- `lowest_cost_passing_model`: lowest known cost among passing candidates.
- `fastest_passing_model`: shortest known execution time among passing candidates.
- `highest_localization_score`: highest stored localization score across all comparison runs,
  including failed ones, or null if none is available.
- `total_cost`: sum of stored estimated costs, including failed attempts, rounded to 8 decimals.
- `total_execution_time`: sum of stored run durations in seconds, including failed attempts,
  rounded to 4 decimals. Waiting for another model in the comparison is excluded.

Each model award is `{ "run_id": "...", "provider": "...", "model": "..." }`, or null when
there is no eligible candidate. Cost/speed ties use the best-model ordering; remaining ties sort
by provider, model name, and run UUID. Missing costs/durations never win cost/speed awards and
contribute zero to totals. Inspect per-run nullable fields to recognize incomplete totals.

Metrics use the existing [evaluation formulas](evaluation-metrics.md). Completed runs are evaluated
by the orchestrator. The comparison service also evaluates terminal failed runs to capture their
inspected files, patch evidence, test outcomes, consumed tokens/cost, and duration. Missing
post-patch results mean `tests_passed=false`. Mock providers report zero tokens and zero cost.
The standalone evaluation POST retains its completed-only rule.

## Audit and Provider Flags

Before the first model runs, all queued AgentRuns and `model_comparison_run_requested` events are
committed together. Each event stores the comparison UUID, model position, and full effective run
configuration. This uses existing tables, so no additional migration is needed.

Each attempted model records `model_comparison_run_started` and `model_comparison_run_finished`,
plus the normal workspace, model, tool, test, patch, and evaluation events. Evaluation failures
record `model_comparison_evaluation_failed`. Comparison membership is kept in backend events;
neither that membership nor another model's results are injected into model prompts.

Provider construction uses the existing ModelProviderFactory and gates:

- OpenAI and Anthropic require `ENABLE_REAL_MODEL_CALLS=true` and their API key.
- Local models require `ENABLE_LOCAL_MODEL_CALLS=true` and the configured local endpoint.
- Mock models work without credentials or enabled network calls.

The model loop receives only the existing agent-visible prompts and controlled tools. GoldPatch
is read exclusively by backend evaluation. See [model providers](model-providers.md) for setup.

## Current Limits

- Execution is synchronous and sequential. Configure client/proxy timeouts for the full batch.
  There is no background queue, resume, cancellation, or retry/idempotency key; another POST starts
  a new comparison. A process interruption can leave unfinished queued/running records.
- Comparison history depends on keeping its AgentEvents. Deleting runs/events deletes that history.
- An unavailable database can prevent progress or audit writes; provider failure isolation does
  not provide database outage recovery. Missing gold data/evaluation failures are reported rather
  than inventing metric values. Totals use the existing engine's estimates, not billing records.
- This reuses the current test executor: configured setup/test commands execute via subprocess in
  the backend process. Moving that executor fully into Docker is separate work. Use trusted
  development tasks with the current executor.
- Tests use mock model aliases, temporary Git repositories, and small local test commands. They
  exercise the real loop, tools, patches, and evaluator; no model APIs or GitHub calls are made.
