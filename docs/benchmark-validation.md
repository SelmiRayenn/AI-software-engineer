# Benchmark Validation Reports

Validation reports make benchmark quality visible without mutating task status or exposing trusted
evaluation data. They answer three separate questions: can the task run, can another operator
reproduce it, and is its evaluation configuration complete enough to trust the result?

## Endpoints

```http
GET /benchmark-tasks/{task_id}/validation-report
GET /benchmark-packs/{pack_id}/validation-report
```

Both endpoints are read-only. Missing resources return 404. Reports are safe for normal dashboard
use: they contain issue codes, counts, messages, and recommendations only. They never include gold
patch text or paths, hidden-test names, commands or payloads, imported hints, or test identifiers.
Repository URL validation is syntactic; generating a report does not contact GitHub, check commit
reachability, execute setup commands, or run tests.

## Response Shape

```json
{
  "subject_type": "benchmark_task",
  "subject_id": "11111111-1111-4111-8111-111111111111",
  "overall_status": "warning",
  "items": [
    {
      "severity": "warning",
      "code": "setup_commands_missing",
      "message": "The task has no configured setup commands.",
      "recommendation": "Record deterministic setup commands, or document that no setup is required."
    }
  ],
  "summary": {
    "total_count": 1,
    "info_count": 0,
    "warning_count": 1,
    "error_count": 0
  },
  "statistics": {
    "pack_membership_count": 0,
    "hidden_test_count": 0,
    "enabled_hidden_test_count": 0,
    "imported": false
  }
}
```

`code` is the stable machine-readable value; messages and recommendations may be refined over
time. Severity has the following meaning:

- `info`: useful context that does not lower readiness.
- `warning`: the benchmark can be structurally usable, but reproducibility or evaluation coverage
  is incomplete.
- `error`: the task or pack is unsafe, inconsistent, or cannot execute as defined.

Overall status is deterministic: any error produces `blocked`; otherwise any warning produces
`warning`; a report with only informational items (or no items) is `ready`. This status is a report
verdict and does not modify `BenchmarkTask.status`. Continue using `POST /benchmark-tasks/{id}/validate`
and `POST /benchmark-tasks/{id}/mark-ready` for the lifecycle workflow.

## Task Checks

Task reports validate the repository record and GitHub URL, base commit, issue/problem statement,
setup and visible test command lists, gold-patch availability and shape, expected hidden-test
coverage, lifecycle consistency, trusted import provenance, and pack membership metadata.

Visible tests are required. Missing setup commands are a warning because some repositories need no
setup, but explicitly documenting that decision remains preferable. A gold patch is required for
ordinary GitHub tasks and any task already marked ready/running/completed/failed. A SWE-bench-style
imported draft may omit it, but receives a warning and cannot become ready under the existing task
validation rules. Gold test-file metadata makes a hidden suite expected; absent or disabled suites
produce warnings rather than exposing their contents.

An invalid ready task gets a `ready_status_inconsistent` error in addition to the underlying errors.
Draft and failed statuses are warnings because pack execution selects only ready tasks. Archived
status is informational.

## Pack Checks

Pack reports validate membership existence, unique non-negative order indexes, unique tasks,
repository availability and URLs, status distribution, setup/test command coverage, gold data for
ready tasks, difficulty and tag coverage, and enabled hidden-test coverage.

The `statistics` object includes total and per-status task counts, represented repositories,
missing command counts, and difficulty/tag/hidden-test coverage ratios. Coverage is measured per
pack task. Hidden coverage counts only enabled suites with valid commands, because disabled or
non-executable imported candidates cannot be run. A pack with no ready tasks is blocked. Draft,
failed, or archived tasks produce a warning even when the pack also contains runnable tasks.

Database constraints normally prevent duplicate order indexes and duplicate task memberships. The
report still checks both conditions so imported, migrated, or manually repaired databases fail
closed if their constraints were bypassed.

## Operational Use

Generate reports during curation and before publishing a pack version. Resolve errors before any
run, decide whether warnings are acceptable for the benchmark's purpose, then preserve the pack and
task definitions used for published results. Reports are computed from current records and are not
historical snapshots; an existing pack-run response remains the reproducible execution snapshot.

## Flakiness Checks

Structural validation does not prove that baseline tests are deterministic. Run
`POST /benchmark-tasks/{task_id}/flakiness-check` before promoting a curated task, then inspect
previous checks with `GET /benchmark-tasks/{task_id}/flakiness-checks`. A check runs setup once and
the configured baseline tests repeatedly in one isolated Docker workspace. Mixed pass/fail outcomes
produce `flaky`; consistent outcomes produce `stable`; setup failure and infrastructure failures
are reported separately. Validation reports may consume this history in a later revision; Prompt 39
stores it without changing task readiness automatically.
