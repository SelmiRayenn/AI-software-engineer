# Evaluation Metrics

The first metrics engine creates one `EvaluationMetric` row per completed `AgentRun`. Re-running
evaluation updates that row instead of creating duplicates.

## API

```text
POST /agent-runs/{run_id}/evaluate
GET /agent-runs/{run_id}/metrics
```

`POST /evaluate` calculates and stores metrics. `GET /metrics` reads the stored result. Evaluation
is rejected until the agent run is `completed`.

The [model comparison service](model-comparison.md) additionally evaluates terminal `failed` runs
through an internal opt-in so failed attempts contribute usage, cost, and time to comparisons.
It does not change their failed status or make them eligible for passing-model awards. Queued,
running, and cancelled runs remain ineligible; the standalone POST retains its completed-only rule.

## Metrics

### File Localization Score

The score compares files inspected by the agent against hidden gold changed files:

```text
matched_gold_files / total_gold_changed_files
```

Only files recorded in `agent_tool_call` event `files_read` payloads are counted as inspected. A
score of `1.0` means the agent inspected every file changed by the gold patch. A score of `0.0`
means it inspected none of them.

### Patch Applied

`patch_applied` is `true` only when a `GeneratedPatch` exists, the patch is not empty, and the run
has evidence that the patch applied cleanly. Evidence currently comes from:

- a `patch_applied` event from the patch service
- a post-patch test phase event with `patch_status` of `applied` or `already_applied`

### Test Pass

`tests_passed` is `true` only when post-patch test results exist and all `post_patch` results passed.

### Modified And Unrelated Files

`modified_files_count` is the number of files listed on the `GeneratedPatch`.

`unrelated_files_count` is the count of generated changed files that are not present in the hidden
gold patch changed-file list.

### Tokens And Cost

For non-mock providers, token usage is aggregated from `model_response` events:

```text
input_tokens + output_tokens
```

Estimated cost is the sum of each event's `estimated_cost`. Mock provider runs always report zero
tokens and zero cost.

### Execution Time

Execution time is:

```text
AgentRun.completed_at - AgentRun.started_at
```

If either timestamp is unavailable, the value is `null`.

## Current Limits

- The formulas are intentionally simple and explainable.
- Gold solution data is read by the evaluation service only; normal agent-facing task/run responses
  still do not expose `GoldPatch.patch_text`.
- Human review and leaderboard aggregation are still future layers.
