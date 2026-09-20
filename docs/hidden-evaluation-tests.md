# Hidden Evaluation Tests

Hidden tests are operator-managed suites stored in `hidden_eval_tests`, separately from the
agent-visible task. A suite has a name, a list of commands, optional `files_payload` (relative
paths mapped to UTF-8 text), an enabled flag, and a creation timestamp. Only enabled suites for
the current task can run.

## Setup and Access

Apply migrations from `backend/` with `alembic upgrade head`. Revision `20260911_0007` adds
the suite table and three evaluation metric columns. Docker Compose applies migrations on start.

Set `TRUSTED_OPERATOR_TOKEN` to a private random token in the backend environment. Supply it
as the `X-Operator-Token` header for every trusted request. An unset token disables these routes
(503); a missing or incorrect header returns 403. Keep the token out of frontend environment
variables, repository workspaces, model messages, and shared request logs. Use HTTPS outside local
development. This is a shared operator credential, not a user account or role management system.

Trusted endpoints:

```text
POST   /benchmark-tasks/{task_id}/hidden-tests
GET    /benchmark-tasks/{task_id}/hidden-tests
DELETE /hidden-tests/{hidden_test_id}
POST   /agent-runs/{task_id}/start-trusted
GET    /agent-runs/{run_id}/tests/hidden-eval
POST   /github/preview-pr/trusted
POST   /benchmark-imports
GET    /evaluation/benchmark-tasks/{task_id}/gold-patch
```

Missing tasks, suites, or runs return 404. Delete and recreate a suite to revise its definition;
previous command results remain stored. Listing suites includes disabled entries.

## Historical PR Test Extraction

Historical task ingestion classifies every changed PR file and stores likely test paths in
`GoldPatch.test_files`. The opt-in `create_hidden_tests_from_pr_tests=true` request flag fetches
candidate content at the PR head and creates runnable suites when a safe command can be inferred.
The flag requires `X-Operator-Token`; it is false by default.

Use `POST /github/preview-pr/trusted` to inspect `detected_test_files` and
`hidden_test_candidates` before task creation. The ordinary PR preview does not fetch or return
candidate contents. Extraction skips removed, binary, non-UTF-8, unavailable, and files over
250,000 bytes. Python candidates use pytest. JavaScript/TypeScript and Ruby commands are inferred
only when configured commands identify Vitest, Jest, or RSpec. Unsupported candidates can be added
manually through the trusted suite endpoint.

## Example

For a prepared Python repository with a `calculator.py` module, create a suite using the trusted
POST endpoint and this JSON body:

```json
{
  "name": "addition boundary cases",
  "commands": ["python -m pytest -q .benchmark-hidden-eval/test_addition.py"],
  "files_payload": {
    "test_addition.py": "from calculator import add\n\ndef test_negative_input():\n    assert add(-2, 3) == 1\n"
  },
  "enabled": true
}
```

File paths are relative to `.benchmark-hidden-eval/` in the private evaluation copy. Commands
execute from that copy's repository root, so they can import and test the selected agent code.
Commands without a files payload can use existing repository tests. The evaluator supports text
file payloads, not patch execution. Benchmark imports can store disabled patch suites with
trusted test identifier metadata; see [benchmark imports](benchmark-imports.md).
Limits are 50 commands, 16,384 characters per
command, 50 files, and 1,000,000 payload bytes per suite. Absolute paths, traversal, backslashes,
Windows drives/streams, and colliding file paths are rejected.

Then POST to `/agent-runs/{task_id}/start-trusted`, with the operator header and:

```json
{
  "model_provider": "mock",
  "run_mode": "tool_loop",
  "max_steps": 4,
  "run_hidden_tests": true
}
```

`run_hidden_tests` defaults to false, including on trusted starts. The normal `/start` endpoint
rejects true even when an operator header is supplied. Public model comparison requests cannot
enable hidden evaluation. The effective run configuration is saved in `agent_run_configured`.
Explicit backend code may pass `run_hidden_tests=True` to the orchestrator request or to
`TestExecutionService.run_hidden_evaluation`; it must stop the model loop first and select a patch.

## Execution and Isolation

The repair loop finishes, chooses a final patch, and restores that candidate before hidden
evaluation begins. Each enabled suite gets its own disposable copy under `SANDBOX_WORKSPACE_ROOT`.
The evaluator verifies that the workspace diff matches the selected patch. Hidden files are never
written into the agent's workspace, included in its index or diff, or fed back to the model. No
model call or repair attempt follows hidden evaluation. A valid selected patch can be evaluated
even when its normal tests failed; invalid patches and runs without a selected patch are skipped.

Copies exclude Git metadata, gold/protected paths, environment files, and test caches. Links,
junctions, and hard-linked files are rejected. Test changes in one suite do not alter another
suite or the retained agent workspace. Evaluation copies are cleaned in a `finally` block,
including after errors and timeouts, regardless of `SANDBOX_RETAIN_WORKSPACES`. Abrupt process
termination can leave a private copy for operator cleanup; it is never assigned to an AgentRun.

Each command produces a `TestResult` with phase `hidden_eval` and the final generated patch ID.
The existing command timeout and output capture limits apply. Command failures are stored and
remaining suites continue. Infrastructure/payload errors stop evaluation with a generic public
error; hidden commands, filenames and exception contents are not copied into public failure text.

## Results and Visibility

`GET /agent-runs/{run_id}/tests/hidden-eval` requires operator authentication and returns private
command logs. The normal `/tests` endpoint excludes hidden results, including their stdout/stderr.
Task reads, prompts, prompt previews, generated patches, and normal frontend task views never
include suite definitions. The reserved `.benchmark-hidden-eval` path is also blocked by controlled
file tools, patch handling, and indexing. Phase audit events expose only counts and patch IDs.

Public metric responses expose aggregate outcomes, not commands or test contents:

- `hidden_tests_passed`: null when nothing ran, true only when all commands passed and the phase
  completed, otherwise false (including interrupted evaluation with partial results).
- `hidden_tests_run_count`: number of stored hidden command results for the selected patch.
- `hidden_tests_failed_count`: number of those commands that failed or timed out.

These fields remain separate from `tests_passed` and `final_patch_passed_tests`, which describe
ordinary post-patch tests. A completed run can fail hidden evaluation. Existing comparison awards
continue to use ordinary post-patch tests. See [evaluation metrics](evaluation-metrics.md).

## Current Limits

Hidden execution reuses the project's current subprocess test executor. Private workspace copies
and API access controls prevent normal agent-tool/API disclosure; they are not an OS security
boundary against hostile repository code or background processes. Use trusted development
repositories until the executor is fully isolated in Docker. Copies omit Git metadata, and
repositories whose setup relies on symlinks or absolute workspace paths need an adapted evaluator.
Output is truncated before storage; subprocess capture itself is not yet a streaming memory limit.
No real model calls are required for the tests or the example mock run.
