# GitHub Ingestion

The backend can preview public GitHub issue and pull request data before creating benchmark tasks. This is a read-only ingestion step that uses the GitHub REST API.

## Setup

Public repositories work without credentials. For higher API rate limits, set an optional token:

```powershell
$env:GITHUB_TOKEN = "<your_github_token>"
```

Or add it to `.env`:

```text
GITHUB_TOKEN=<your_github_token>
```

The token only needs read access for public repository metadata. Do not commit `.env`.

## Preview An Issue

```powershell
$body = @{
  repository_url = "https://github.com/psf/requests"
  issue_number = 1
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/github/preview-issue" `
  -ContentType "application/json" `
  -Body $body
```

The response includes:

- Repository metadata.
- Issue title, body, labels, author, and timestamps.
- Issue comments.
- Best-effort linked pull requests.
- A `benchmark_task_hint` object with fields that can later seed a benchmark task.

## Preview A Pull Request

```powershell
$body = @{
  repository_url = "https://github.com/psf/requests"
  pull_request_number = 1
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/github/preview-pr" `
  -ContentType "application/json" `
  -Body $body
```

The response includes:

- Repository metadata.
- Pull request metadata.
- Changed files and per-file patches where GitHub returns them.
- Pull request commits.
- A `benchmark_task_hint` with base commit, fix commit, linked PR URL, changed files, and detected test files.

Each changed PR file is classified as `source`, `test`, `docs`, `config`, or `other`.
Test detection recognizes conventional `test`/`tests`/`spec` directories, Python
`test_*.py` and `*_test.py` files, JavaScript/TypeScript `.test.*` and `.spec.*` files,
and several common framework naming patterns. Detected paths are persisted in
`GoldPatch.test_files` when a historical task is created.

## Trusted Hidden-Test Candidate Preview

The normal preview does not fetch or return full hidden-test candidate contents. Operators can
inspect candidates through the trusted endpoint:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/github/preview-pr/trusted" `
  -Headers @{ "X-Operator-Token" = $env:TRUSTED_OPERATOR_TOKEN } `
  -ContentType "application/json" `
  -Body $body
```

The response adds `detected_test_files` and `hidden_test_candidates`. Candidate UTF-8 content is
fetched from the PR head commit where GitHub can provide it. Content is capped at 250,000 bytes;
removed, binary, non-UTF-8, and unavailable files are reported without content.

## Create Hidden Tests From A PR

`POST /benchmark-tasks/from-github` accepts `create_hidden_tests_from_pr_tests`, which defaults to
`false`. Setting it to `true` requires the trusted operator header. Runnable candidates are stored
as enabled `HiddenEvalTest` suites; all detected paths remain recorded in `GoldPatch.test_files`.

```json
{
  "repository_url": "https://github.com/example/project",
  "issue_number": 12,
  "pull_request_number": 34,
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "setup_commands": [],
  "test_commands": ["python -m pytest -q"],
  "create_hidden_tests_from_pr_tests": true
}
```

Generated files are staged only in the private hidden-evaluation copy. Task responses, normal
task reads, run prompts, and prompt previews do not expose candidate contents or hidden commands.

## Linked Pull Request Detection

Issue previews detect linked pull requests in two ways:

- Full GitHub pull request URLs in the issue body or comments.
- GitHub issue timeline cross-reference events when the API exposes them.

This is best effort. Some older or unusual GitHub workflows may not expose a linked pull request through the public API.

## Current Limits

- GitHub Enterprise URLs are not supported yet.
- Private repositories are not supported yet.
- The preview endpoints do not create database records.
- The preview endpoints do not clone repositories or run tests.
- Hidden-test command inference is conservative. Unsupported frameworks remain trusted candidates
  and can be configured through the hidden-test management API.
