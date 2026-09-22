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
The orchestrator also uses this opt-in for failed repair runs with a selected candidate.
It does not change their failed status or make them eligible for passing-model awards. Queued,
running, and cancelled runs remain ineligible; the standalone POST retains its completed-only rule.

## Metrics

For repair runs, patch/test metrics describe the final selected version and its associated
post-patch tests. Failed earlier versions and diagnostic tool tests are excluded. An invalid final
candidate cannot claim patch application or passing tests. Localization, tokens, cost, and elapsed
time include the entire run. All candidate/test history remains available for inspection.

### File Localization Score

The score compares files inspected by the agent against hidden gold changed files:

```text
matched_gold_files / total_gold_changed_files
```

Only files recorded in `agent_tool_call` event `files_read` payloads are counted as inspected. A
score of `1.0` means the agent inspected every file changed by the gold patch. A score of `0.0`
means it inspected none of them.

The operator analytics endpoint `GET /analytics/file-localization` adds ranking and edit-quality
context. It orders unique paths from successful retrieval/search/read events, stops at the first
edit, and reports top-1/top-3/top-5 accuracy plus generated-patch precision and recall. This
pre-edit analytics definition is intentionally stricter than the persisted compatibility metric,
which continues to count every `agent_tool_call.files_read` entry over the complete run. See
[aggregate analytics](analytics.md#file-localization) for formulas and filtering.

### Patch Applied

`patch_applied` is `true` only when a `GeneratedPatch` exists, the patch is not empty, and the run
has evidence that the patch applied cleanly. Evidence currently comes from:

- a `patch_applied` event from the patch service
- a post-patch test phase event with `patch_status` of `applied` or `already_applied`

### Visible And Baseline Tests

- `baseline_tests_passed` is true only when baseline results exist and every baseline command passed.
- `post_patch_tests_passed` is true only when results exist for the final selected patch and every
  visible post-patch command passed.
- `tests_passed` remains a compatibility alias for `post_patch_tests_passed`.
- `regression_detected` is true when baseline tests passed but post-patch tests failed.

### Hidden Evaluation

Hidden tests have separate metrics and do not change `tests_passed`:

- `hidden_tests_passed`: null if no hidden commands ran for the selected patch; true only if all
  passed and the hidden phase completed, otherwise false.
- `hidden_tests_run_count`: stored hidden command result count for the final selected patch.
- `hidden_tests_failed_count`: failed/timed-out commands within that count.

Partial execution cannot report hidden success even if the commands that finished passed.
Re-evaluation updates these fields on the existing metric row. Earlier candidate results are
excluded. A completed run can pass ordinary tests but fail hidden evaluation; the issue-specific
outcome is `hidden_tests_passed`. Run details and comparisons expose aggregates only, with private
logs available through the trusted route described in [hidden evaluation tests](hidden-evaluation-tests.md).

### Issue-Specific Success

`issue_resolved` combines visible and hidden evidence:

- With hidden results, visible post-patch and hidden tests must both pass.
- Without hidden results, visible post-patch tests determine resolution, but confidence is lower.

`issue_specific_score` is deliberately discrete and explainable:

| Visible post-patch | Hidden evaluation | Score |
| --- | --- | ---: |
| Pass | Pass | `1.0` |
| Pass | Not run | `0.75` |
| Fail | Pass | `0.5` |
| Any other outcome | Fail or incomplete | `0.0` |

Hidden evaluation is considered run when at least one hidden command result exists. A partial
hidden phase cannot report success. The API returns `hidden_tests_passed: null` when no hidden
commands ran, which lets clients distinguish lower-confidence visible-only success from hidden
evaluation failure.

### Modified And Unrelated Files

`modified_files_count` is the number of files listed on the `GeneratedPatch`.

`unrelated_files_count` is the count of generated changed files that are not present in the hidden
gold patch changed-file list.

## Patch Quality

Patch minimality is stored separately in `PatchQuality` so each immutable patch version retains
its own report, while `EvaluationMetric` continues to summarize the final selected patch for a run.
`GET /patches/{patch_id}/quality` reports file and line counts, file-kind categories,
whitespace-only changes, dependency/lockfile touches, generated-output violations, and warnings.

The quality service and evaluation engine use the same hidden gold association for unrelated-file
analysis. Public quality responses expose only the unrelated count and never reveal gold paths or
gold patch text.

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
- Hidden execution currently uses the existing subprocess executor; see the hidden evaluation
  documentation for isolation limits.
- The current confidence indicator is derived from whether hidden tests ran; it is not a
  probabilistic confidence estimate.
