# Backend

FastAPI service for the benchmark platform.

The current implementation exposes a health endpoint, SQLAlchemy models, and basic list/create endpoints for repositories, benchmark tasks, and agent runs.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Health check:

```text
GET /health
```

Create database tables:

```powershell
python scripts/create_tables.py
```

Seed development data:

```powershell
python scripts/seed_dev.py
```

Initial API endpoints:

```text
GET  /api/v1/repositories
POST /api/v1/repositories
GET  /api/v1/benchmark-tasks
POST /api/v1/benchmark-tasks
GET  /api/v1/agent-runs
POST /api/v1/agent-runs
POST /sandbox/run
POST /github/preview-issue
POST /github/preview-pr
POST /benchmark-tasks/from-github
POST /benchmark-tasks/{task_id}/validate
POST /benchmark-tasks/{task_id}/mark-ready
GET  /evaluation/benchmark-tasks/{task_id}/gold-patch
```

The sandbox endpoint is a proof of concept for cloning a repository, checking out a commit, and running setup/test commands inside an isolated Docker container. See `docs/sandbox.md` from the repository root.

The GitHub preview endpoints fetch public repository, issue, pull request, file, and commit metadata through the GitHub REST API. Set `GITHUB_TOKEN` for higher rate limits. See `docs/github-ingestion.md` from the repository root.

The GitHub benchmark creation endpoint turns a real issue plus merged fix PR into a benchmark task while hiding the gold patch from agent-visible responses. See `docs/benchmark-tasks.md` from the repository root.

Benchmark tasks must pass validation before they can be marked ready for agent use. The validation endpoint checks repository metadata, issue and PR numbers, base commit, setup/test commands, hidden gold data, changed files, and task lifecycle status.
