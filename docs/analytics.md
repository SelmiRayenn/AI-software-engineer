# Aggregate Analytics

The analytics API summarizes persisted agent runs and their evaluation outcomes. It does not
read or return gold patches, hidden test definitions, prompts, source code, or command logs.

## Endpoints

- `GET /analytics/summary` returns one aggregate across the matching runs.
- `GET /analytics/by-repository` returns one aggregate per repository represented by the
  matching runs.
- `GET /analytics/by-pack` returns one aggregate per benchmark pack represented by the matching
  pack-run records. Runs performed outside a pack are not attributed to that pack merely because
  their task is currently a pack member.
- `GET /analytics/model-leaderboard` groups matching runs by provider and model, ranks each model
  configuration, and returns rows in descending composite-score order.

All endpoints accept these optional query parameters:

- `benchmark_pack_id`
- `repository_id`
- `model_provider`
- `model_name`
- `date_from`
- `date_to`

Dates are ISO 8601 timestamps and filter `AgentRun.started_at` inclusively. `date_from` must be
earlier than or equal to `date_to`. Unknown repository or pack IDs produce an empty result rather
than an error.

Example:

```bash
curl "http://localhost:8000/analytics/summary?model_provider=mock&date_from=2026-01-01T00:00:00Z"
```

## Metric semantics

Run counts include every matching run, including queued or running records. Completed and failed
counts use the corresponding run status exactly.

Patch approval counts include every generated patch with a stored human review. This means a run
with reviewed historical patch versions can contribute more than one reviewed patch.

Performance rates and evaluation averages use only runs with an `EvaluationMetric` record:

- `patch_apply_rate` uses `patch_applied`.
- `visible_test_pass_rate` uses `post_patch_tests_passed`.
- `issue_resolved_rate` uses `issue_resolved`.
- `regression_rate` uses `regression_detected`.
- `hidden_test_pass_rate` uses only evaluated runs where `hidden_tests_run_count > 0`.

The hidden test rate is `null` when no matching run executed hidden tests. Other rates and numeric
aggregates are `0` for an empty population. Nullable average inputs are ignored. Total token and
cost fields treat missing values as zero. `average_cost_per_run` is total cost divided by all
matching runs, including runs without an evaluation metric.

The initial implementation loads the filtered run records and their required relationships in a
bounded set of database queries, then computes the aggregate in the service layer. For very large
installations, these calculations can later move to materialized or SQL-native aggregate views
without changing the API contract.

## Model leaderboard

The leaderboard accepts `benchmark_pack_id`, `repository_id`, `date_from`, `date_to`, and
`min_runs`. `min_runs` defaults to 1 and is applied after grouping by the exact
`model_provider`/`model_name` pair. Pack filtering uses actual pack-run records, consistent with
the other analytics endpoints.

Each row includes run counts, issue and test success rates, localization and issue-specific
scores, average cost, tokens, execution time, changed files, and unrelated files. Token average
is total recorded tokens divided by all matching runs for the model. The remaining averages and
rates use the aggregate semantics above.

Four independent competition ranks are included. Higher issue resolution and localization are
better; lower cost and execution time are better. Exact ties share a rank and the next position is
skipped, such as `1, 1, 3`. These ranks are descriptive and do not alter the composite score.

The composite score is bounded to 0.0-1.0 and rounded to six decimals:

```text
performance =
    0.55 * issue_resolved_rate
  + 0.15 * visible_test_pass_rate
  + 0.10 * hidden_test_pass_rate
  + 0.20 * average_file_localization_score

penalties =
    0.04 * min(average_cost_per_run / $1.00, 1)
  + 0.03 * min(average_execution_time_seconds / 600, 1)
  + 0.03 * min(average_unrelated_files_count / 5, 1)

composite_score = clamp(performance - penalties, 0, 1)
```

When no hidden tests ran, the hidden-test component is zero. This intentionally gives fully
evaluated models more confidence than models with no hidden evaluation evidence. Cost, speed, and
unrelated-file penalties are capped so performance remains the primary signal. Equal composite
scores are ordered deterministically by issue-resolution rate, cost, provider, and model name.

## Dashboard

The React dashboard exposes the aggregate APIs through two engineering-focused views:

- `/analytics` shows summary metrics and repository/pack breakdown tables.
- `/leaderboard` shows one row per provider/model configuration with rank badges and the composite
  score.

Both pages include provider, repository, and benchmark-pack filters plus explicit loading, empty,
and error states. The provider selector on the leaderboard filters the returned rows in the
browser because the leaderboard API groups all providers; repository and pack filters are applied
by the backend. Hidden-test rates remain `N/A` when the API returns `null`.
