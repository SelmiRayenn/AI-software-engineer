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
