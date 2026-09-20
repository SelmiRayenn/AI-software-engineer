# Benchmark Packs

Benchmark packs are versioned definitions of a named benchmark selection. A pack records a
human-readable name, stable slug, description, version and source. Its task memberships carry an
explicit order plus optional difficulty and tags, so clients can rerun the same ordered selection
and compare model results consistently.

Changing source tasks after adding them changes what the pack runs. For strict published-dataset
reproducibility, create a new pack slug/version after task definitions are verified and avoid
editing those tasks. Pack versioning is curator-managed; the API does not infer semantic versions
or create snapshots of repository contents.

## Create and List Packs

```text
POST /benchmark-packs
GET  /benchmark-packs
GET  /benchmark-packs/{pack_id}
```

Create request:

```json
{
  "name": "Python Core Repairs",
  "slug": "python-core-2026",
  "description": "Verified Python repair tasks for the 2026 baseline.",
  "version": "2026.1",
  "source": "internal-curation"
}
```

`name`, `slug`, and `version` are required. Slugs are globally unique, lowercase words separated
by single hyphens. A duplicate slug returns 409. `description` and `source` are optional. Creating
a new release currently requires a new slug, for example `python-core-2026-2`, because slugs are
the unique pack identity rather than a reusable family name.

The list endpoint supports `skip` and `limit` query parameters. A pack detail response includes
the same summary plus its task memberships ordered by `order_index`.

## Memberships

```text
POST   /benchmark-packs/{pack_id}/tasks
DELETE /benchmark-packs/{pack_id}/tasks/{task_id}
```

Add request:

```json
{
  "benchmark_task_id": "11111111-1111-4111-8111-111111111111",
  "order_index": 10,
  "difficulty": "medium",
  "tags": ["python", "parser"]
}
```

Order indexes are non-negative integers. They do not need to be contiguous, which allows a
curator to insert tasks between existing positions. A task can appear only once in a pack and an
order index can be used only once in that pack; either conflict returns 409. Difficulty is optional.
Tags and difficulty are trimmed and normalized to lowercase; duplicate tags in one membership are
removed while preserving their first occurrence. Removing a task leaves other order indexes intact.
Missing packs, tasks, or memberships return 404.

Pack responses contain agent-visible task metadata only: task UUID, issue number/title, status and
repository name. They do not expose gold patches, hidden test definitions, trusted import metadata,
setup commands or test commands.

## Summary Fields

Every create, list, and detail response includes:

- `task_count`: all task memberships.
- `ready_task_count`: memberships whose current task status is `ready`.
- `repositories_represented`: sorted unique `owner/name` repository identifiers.
- `difficulty_distribution`: counts by normalized difficulty; missing values use `unspecified`.
- `tags`: sorted unique tags across all memberships.

Summary fields are calculated from current task and membership records on every read. A task status
transition can therefore change `ready_task_count` without modifying the pack.

## Import Into a Pack

Create the pack first, then supply its UUID while importing a JSON/JSONL dataset:

```sh
curl -X POST "http://localhost:8000/benchmark-imports?pack_id=PACK_UUID" \
  -H "X-Operator-Token: $TRUSTED_OPERATOR_TOKEN" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @tasks.jsonl

python ../scripts/import_benchmark_tasks.py tasks.jsonl --pack-id PACK_UUID
```

New tasks append after the pack's highest existing order index. If the pack is empty, ordering
starts at 0. Successfully imported rows keep source order. Invalid and duplicate rows do not create
memberships or consume order positions. Imported memberships have no difficulty and no tags; add
curated metadata later by constructing the pack through the membership API before publication.

Task creation, trusted data storage and pack membership are committed as one row-level transaction.
A failed row leaves neither a task nor membership. An unknown pack returns a document-level
`pack_not_found` import error and imports no rows. Concurrent imports into the same pack can collide
on order indexes; those rows report `storage_conflict` and can be retried after the other import
finishes.

See [benchmark imports](benchmark-imports.md) for format validation, trusted fields and complete
summary behavior.

## Database Migration

Apply migrations before using packs:

```sh
cd backend
alembic upgrade head
```

Revision `20260920_0010` adds `benchmark_packs` and `benchmark_pack_tasks`, including database
constraints for unique slugs, task membership, and order positions.

Revision `20260920_0011` adds `benchmark_pack_runs` and `benchmark_pack_run_tasks` for saved
rosters, effective run configuration, per-task outcomes, and aggregate metrics.

## Run a Pack

```http
POST /benchmark-packs/{pack_id}/runs
Content-Type: application/json

{
  "model_provider": "mock",
  "model_name": "mock-model",
  "max_steps": 4,
  "max_tool_errors": 3,
  "command_timeout_seconds": 120,
  "include_issue_comments": false,
  "enable_test_tool": true,
  "run_mode": "tool_loop",
  "max_repair_attempts": 0,
  "run_tests_after_patch": true,
  "stop_on_first_passing_patch": true,
  "include_test_failure_feedback": true,
  "include_hidden_tests": false,
  "stop_on_task_failure": false
}
```

Options are flat, with the same validation and defaults as [agent runs](agent-runs.md). Provider
defaults to `mock`; omitting `model_name` resolves and stores the configured provider default before
execution. Unknown fields and invalid limits return 422. An unknown pack returns 404; a pack with
no ready tasks returns 409 without creating any runs.

The POST waits for execution and returns 201 with the saved result, including when individual tasks
fail. Only tasks currently `ready` are selected, in increasing pack `order_index`. The complete
roster and one queued AgentRun per selected task are committed before the first task starts. Each
task receives a new provider instance, conversation, workspace, patch history, tests and metrics.
Existing workspace cleanup/retention settings apply. The source task remains ready for reruns.

Tasks run sequentially. A failure does not prevent later tasks from running unless
`stop_on_task_failure=true`. With that flag, remaining roster entries are `skipped`, their queued
AgentRuns are cancelled with a classified explanation, and no workspace or model call is started
for them. Skipped tasks remain in the aggregate denominator. Baseline test failures are diagnostic
and do not automatically stop a task; repair failures follow the existing agent-run rules.

Overall status is `running`, `completed`, `partial_failure`, or `failed`. A completed pack means
all AgentRuns completed, not that every issue was resolved. In particular, a failed hidden test
affects issue-success metrics without changing the existing AgentRun completion semantics;
`stop_on_task_failure` responds to failed/cancelled AgentRuns, not hidden-test verdicts alone.

## Hidden Evaluation and Safety

`include_hidden_tests` defaults to false. To enable it, configure `TRUSTED_OPERATOR_TOKEN` and
send a valid `X-Operator-Token` header on the POST. A missing/incorrect header returns 403; trusted
execution is unavailable (503) when the operator token is not configured. The inherited
`run_hidden_tests=true` option is rejected at this API boundary; use `include_hidden_tests` instead.
Explicit backend callers must also pass `trusted_operator=True` to the pack-run service.

Hidden suites execute only after final patch selection, using the existing private evaluation
workflow. Tasks without enabled suites report no hidden coverage. Gold patches, hidden test
payloads/commands, and trusted import metadata never enter pack-run responses or model context.
Only scalar evaluation outcomes are exposed. Normal setup/test commands are taken from the task,
not from the pack-run request. Existing controlled tools, patch limits, repair limits and provider
feature flags remain in force. Disabled providers produce classified `model_provider_error` runs
without creating workspaces. Nothing approves or publishes generated patches automatically.

## Read Saved Results

```http
GET /benchmark-pack-runs/{pack_run_id}
```

The response includes pack name/slug/version, effective model configuration, timestamps, overall
status, `aggregates`, and ordered `tasks`. Each task entry contains its AgentRun ID, original order
index, agent-visible task snapshot, status, metric summary, timestamps and classified failure if
present. Use the existing `/agent-runs/{run_id}` endpoints to inspect generated patches and visible
test results. Missing pack runs return 404.

Snapshots make a completed result independent of later pack membership, task-title, or metric
changes. Each queued task also has an internal SHA-256 fingerprint of its agent-visible definition,
configured setup/tests, and dependency-change permissions. If that definition changes before the
task starts, the entry fails as `task_not_ready` instead of silently running a different task.
Gold data and hidden payloads are not included in this public snapshot or fingerprint. Curators must
still freeze task definitions, gold/hidden suites, environments and versions for comparable reruns;
this is not an immutable dataset or environment archive.

Events named `benchmark_pack_task_requested`, `benchmark_pack_task_started`, and
`benchmark_pack_task_finished` link each AgentRun to the pack-run UUID and order. Requested events
store effective configuration. Early-stop tasks record `benchmark_pack_task_skipped`; unexpected
execution/evaluation failures record audit events and preserve existing root-cause classifications.

## Aggregate Definitions

- `total_tasks`: the saved ready-task roster, excluding non-ready pack members.
- `completed_tasks`, `failed_tasks`, `skipped_tasks`: execution outcome counts.
- `issue_resolved_count`: tasks whose saved metric reports `issue_resolved=true`.
- `issue_resolved_rate`: resolved count divided by total selected tasks.
- `visible_test_pass_rate`: tasks passing all visible post-patch tests divided by total tasks.
- `hidden_test_pass_rate`: tasks passing hidden evaluation divided by tasks with at least one
  hidden result; null when none ran. `hidden_tested_tasks` reports that coverage denominator.
  This is a per-task rate, not a ratio of individual hidden commands.
- `average_file_localization_score`, `average_issue_specific_score`: sum of saved scores divided
  by total selected tasks. Missing/skipped scores contribute zero.
- `total_tokens`, `total_cost`: summed usage, including failed attempts. Cost is estimated USD,
  rounded to 8 decimals; mock usage is zero. If evaluation is unavailable, recorded model usage
  is still counted without inventing successful test outcomes.
- `total_execution_time`: sum of attempted AgentRun durations in seconds, excluding queue wait.
- `average_execution_time`: total execution time divided by completed plus failed tasks, excluding
  skipped tasks. Times are rounded to 4 decimals; pack timestamps also show wall-clock duration.

Aggregates are saved after each task and read without rerunning evaluations. Per-task tests and
scores use the existing [evaluation formulas](evaluation-metrics.md).

## Execution Limits

- This is synchronous, sequential orchestration, not a durable job queue. Configure HTTP/proxy
  timeouts for the whole pack. A process/database failure can leave queued/running records; there
  is no recovery, resume, cancellation API or idempotency key yet. Repeating POST starts a new run.
- Task-level failures are isolated; database outages may prevent result/audit persistence.
- Historical run/task/AgentRun links use restrictive foreign keys. Removing a pack membership does
  not erase an existing run roster. Do not delete the underlying tasks or runs used by saved results.
- The existing test executor still runs configured commands via backend subprocesses. Separate
  working directories are not a security boundary for untrusted code. Use trusted development
  tasks until test execution is fully isolated in Docker; this endpoint does not fix that limitation.
- Tests use mock providers and temporary local Git repositories. No real provider calls are required.
