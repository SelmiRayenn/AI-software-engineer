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
- up to eleven registered controlled tools, including `submit_candidate_files`, `submit_plan`, and
  `submit_hypothesis`
- a maximum model/tool step count
- a maximum cumulative tool error count

Each model response may request structured tool calls. The loop validates every call against the
registered tool name and its argument contract, executes workspace actions through
`AgentWorkspaceTools` and plans through the run-local planning service, and adds
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
  "include_test_failure_feedback": true,
  "require_plan_before_edit": true,
  "max_plan_revisions": 2,
  "plan_min_evidence_files": 1,
  "require_hypothesis_before_patch": true,
  "require_candidate_files_before_edit": true,
  "max_candidate_files": 10
}
```

`max_steps` is capped at 50 and `max_tool_errors` is capped at 20 by request validation. Command
timeouts remain bounded by the sandbox configuration.

Set `run_mode` to `scripted` with the mock provider for a deterministic flow. Set
`enable_test_tool` to `false` to remove `run_tests` from both the advertised definitions and the
executable tool registry. Setup and baseline remain enabled; `run_tests_after_patch` independently
controls automatic post-patch tests.

## Evidence-Grounded Planning

Planning is required by default, in both scripted and tool-loop modes. Before the first
`write_file` or `submit_patch` (including a no-op patch), call `submit_plan`:

```json
{
  "issue_summary": "Addition returns the wrong result.",
  "suspected_root_cause": "The inspected implementation subtracts its operands.",
  "files_inspected": ["src/calculator.py"],
  "files_likely_to_modify": ["src/calculator.py", "tests/test_calculator.py"],
  "test_strategy": "Run the configured calculator tests and regression suite.",
  "risk_rollback_notes": "Restrict the fix to addition; revert the generated diff on regression."
}
```

All six fields are required. Text fields must be non-empty and at most 2,000 characters each.
Each path list accepts at most 50 paths of 300 characters; the entire plan is limited to
16,384 UTF-8 bytes. Paths must remain inside the workspace and cannot refer to protected gold
or hidden-test locations. Proposed new files may not yet exist.

Evidence comes only from successful `read_file`, `search_code` matches, and
`retrieve_relevant_files` results in this run. Listing filenames, failed reads, and the agent's
own claims do not count. Every claimed inspected file must have been observed; duplicate and
equivalent paths count once. `plan_min_evidence_files` defaults to 1 (range 1-50).

`max_plan_revisions` defaults to 2 (range 0-10): one initial submission plus two revisions.
Invalid submissions consume this budget too. Missing plans and rejected revisions produce tool
errors with corrective feedback; they consume the existing step/tool-error budgets, but not a
patch repair attempt. No counter resets. A rejected revision closes the edit gate, even after
an earlier accepted plan. A newly accepted plan reopens it. Once the revision budget is
exhausted, no subsequent plan can unlock editing.

Accepted plans remain valid across patch repairs. `files_likely_to_modify` is an inspectable
intent, not a new file allowlist; existing path and patch-quality guardrails still apply.
Evidence checks verify observation, not the correctness of the proposed root cause.
Set `require_plan_before_edit=false` explicitly only for legacy/comparison experiments.
Model comparisons and pack runs accept the same configuration, stored in each run's configured
event. Plan acceptance is automatic validation, not human patch approval.

Each submission stores a `plan_submitted` event containing revision, accepted/rejected status,
reason, timestamp, and bounded redacted plan data. Invalid schemas and unsafe paths do not retain
raw content. Unvalidated plan arguments are omitted from generic model/tool event logs.
No gold or hidden-test records are loaded by planning. Common credential patterns are redacted;
this is best-effort redaction, not a general secret-detection guarantee.

`GET /agent-runs/{run_id}` includes `latest_plan` (null for older/unplanned runs).
The trace endpoint includes `latest_plan`, `plan_status` (`not_submitted`, `accepted`, `rejected`),
and revision-specific events. Rejected plans have warning severity. Storage uses existing
AgentEvent/config JSON, so this feature needs no database migration.

## Candidate File Ranking

`require_candidate_files_before_edit` defaults to true. Before the first `write_file`, the agent
must call `submit_candidate_files` with an ordered `ranked_files` list:

```json
{
  "ranked_files": [
    {
      "path": "src/calculator.py",
      "reason": "The inspected implementation contains the incorrect operator.",
      "confidence": "high"
    }
  ]
}
```

Every candidate must be an existing, non-protected workspace file previously returned by
`retrieve_relevant_files` or successfully read with `read_file` in the same run. File listings and
search matches alone do not qualify. Paths are normalized, must remain inside the workspace, and
cannot reference gold or hidden-test locations. Duplicate paths are rejected so rank positions stay
unambiguous.

`max_candidate_files` defaults to 10 and accepts 1-50. Each path is capped at 300 characters, each
reason at 1,000 characters, and the complete submission at 16,384 UTF-8 bytes. Confidence is
`low`, `medium`, or `high`. Valid submissions store a bounded, redacted
`candidate_files_submitted` AgentEvent; raw candidate arguments are omitted from generic model and
tool logs. The latest valid ranking appears as `candidate_files` on `GET /agent-runs/{run_id}` and
as a normalized trace event. Existing event/config storage means no migration is required.

Candidate ranking is distinct from planning. The plan explains the intended change; the candidate
list records pre-edit file-localization beliefs in a strict order. A missing ranking blocks only
`write_file` and consumes the existing step/tool-error budgets. It does not expose or consult gold
files. Rankings may be revised during investigation but are frozen after the first successful
write. Set `require_candidate_files_before_edit=false` only for legacy deterministic flows.

## Root-Cause Hypotheses

`require_hypothesis_before_patch` defaults to true. Before `submit_patch`, the run must contain an
`active` or `confirmed` hypothesis submitted through `submit_hypothesis`. This is separate from the
plan gate: plans control editing, while hypotheses make the current bug diagnosis explicit. A
missing hypothesis rejects only the patch submission and returns corrective tool feedback.

Each hypothesis contains a summary, up to 50 suspected workspace files, 1-50 supporting evidence
items, confidence (`low`, `medium`, or `high`), and status (`active`, `revised`, `rejected`, or
`confirmed`). Summary text is capped at 2,000 characters, each evidence item at 1,000 characters,
each path at 300 characters, and the complete payload at 16,384 UTF-8 bytes. Suspected paths must
resolve to existing non-protected workspace files. Hypotheses never query gold or hidden-test data.

Submitting a new `active` or `confirmed` hypothesis changes the previous active/confirmed record to
`revised` and links it to the new revision. `rejected` and explicitly `revised` entries remain in
history but do not satisfy the patch gate. After patch tests fail, the agent may inspect the bounded
feedback, submit a revised hypothesis, edit, and resubmit within the existing repair, step, and tool
error limits. Hypothesis revisions do not reset any budget.

Every valid submission records a redacted `hypothesis_submitted` AgentEvent with its revision,
status, confidence, suspected files, evidence, and timestamp. Generic model/tool event logs omit raw
hypothesis arguments. `GET /agent-runs/{run_id}` returns the ordered `hypotheses` list and the
current `active_hypothesis`; the run trace includes each hypothesis event with a normalized summary.
This uses AgentEvent JSON storage and requires no migration. Credential redaction is best effort.

The deterministic mock flow reads a file, submits a plan, hypothesis, and candidate ranking in one
model response, then submits a no-op patch. Real sequential editing runs normally need at least six
tool steps: inspect, plan, hypothesize, rank candidates, edit, and submit. The default step budget is
therefore six.

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
- `submit_candidate_files`
- `submit_plan`
- `submit_hypothesis`
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
- `plan_submitted`
- `candidate_files_submitted`
- `hypothesis_submitted`
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
Without one, it reads a README or first source file, submits the required plan, hypothesis, and
candidate ranking, then submits a no-op patch. This exercises the real loop without making an
external API call.

OpenAI uses the common response contract through an opt-in Responses API adapter. Real calls are
disabled by default. Anthropic and local adapters use the same loop and their existing feature
flags. See [model providers](model-providers.md) for configuration.
