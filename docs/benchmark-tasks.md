# Benchmark Tasks

Benchmark tasks are historical GitHub issues paired with a known merged fix pull request. The benchmark stores two separate views of the same task:

- Agent-visible task data: repository metadata, issue title/body/comments, and the base commit.
- Hidden gold solution data: fix commit, linked PR URL, changed files, test files, and patch text.

The separation is intentional. Agents should solve from the issue context and base repository state, not from the known solution.

## Create From GitHub

```text
POST /benchmark-tasks/from-github
```

Request:

```json
{
  "repository_url": "https://github.com/example/project",
  "issue_number": 123,
  "pull_request_number": 124,
  "base_commit": "abc1234",
  "fix_commit": null,
  "setup_commands": ["python -m pip install -e ."],
  "test_commands": ["pytest"],
  "notes": "Optional benchmark curation note."
}
```

The backend fetches repository metadata, issue metadata, issue comments, pull request metadata, changed files, commits, and PR diff data from GitHub. It then reuses or creates the `Repository`, creates the `BenchmarkTask`, and stores the hidden `GoldPatch`.

If `fix_commit` is omitted, the backend derives it from the PR merge commit first, then the PR head commit.

## Agent-Visible Response

The create endpoint returns only agent-visible fields:

```json
{
  "id": "...",
  "repository_id": "...",
  "repository": {
    "id": "...",
    "name": "project",
    "owner": "example",
    "url": "https://github.com/example/project.git",
    "default_branch": "main",
    "language": "Python",
    "created_at": "..."
  },
  "issue_number": 123,
  "issue_title": "...",
  "issue_body": "...",
  "issue_comments": [],
  "base_commit": "abc1234",
  "status": "ready",
  "created_at": "..."
}
```

It does not include:

- `GoldPatch.patch_text`
- gold changed files
- test files
- `fix_commit`
- linked PR URL

## Evaluation Access

Gold solution data is available through the evaluation namespace:

```text
GET /evaluation/benchmark-tasks/{task_id}/gold-patch
```

This route returns the stored gold patch, including patch text and changed files. The project does not implement authentication yet, so keep these routes restricted to trusted local/operator environments until admin auth exists.

## Validation and Lifecycle

```text
POST /benchmark-tasks/{task_id}/validate
POST /benchmark-tasks/{task_id}/mark-ready
```

Validation checks:

- repository URL parses as a GitHub repository URL
- issue number is present
- pull request number is present
- base commit is present
- setup commands are a list
- test commands are a non-empty list
- a gold patch exists
- the PR changed files list is present on the gold patch
- task status is one of the known lifecycle statuses

Task statuses:

```text
draft -> ready -> running -> completed
                -> failed
```

Tasks can be archived from draft, ready, completed, or failed states. A failed task can move back to ready after validation passes. Archived tasks are terminal.

The `mark-ready` endpoint runs validation first. If validation fails, the endpoint returns the validation result and leaves the task unchanged.

## Manual Draft Dataset

The repository includes `benchmarks/manual_tasks.json` with small Python repository candidates. They intentionally load as `draft` because exact historical issue, PR, base commit, and gold patch details still need verification.

Load them from the repository root:

```powershell
python scripts/load_manual_benchmarks.py
```

## Current Limits

- Public GitHub repositories only.
- The PR must be merged.
- Creating tasks does not run the sandbox or tests.
- PostgreSQL schema changes are applied with Alembic migrations. See `docs/database-migrations.md`.
