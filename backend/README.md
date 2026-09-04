# Backend

FastAPI service for the benchmark platform.

The current implementation exposes a health endpoint, SQLAlchemy models, and basic list/create endpoints for repositories, benchmark tasks, and agent runs.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts/apply_migrations.py
uvicorn app.main:app --reload
```

Health check:

```text
GET /health
```

Apply database migrations:

```powershell
python scripts/apply_migrations.py
```

Create and inspect migrations:

```powershell
python scripts/create_migration.py "describe schema change"
python scripts/migration_status.py
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
POST /agent-runs/{benchmark_task_id}/start
GET  /agent-runs/{run_id}
GET  /agent-runs/{run_id}/diff
GET  /agent-runs/{run_id}/patch
POST /agent-runs/{run_id}/patch/apply
POST /agent-runs/{run_id}/tests/baseline
POST /agent-runs/{run_id}/tests/post-patch
GET  /agent-runs/{run_id}/tests
POST /agent-runs/{run_id}/evaluate
GET  /agent-runs/{run_id}/metrics
POST /patches/{patch_id}/approve
POST /patches/{patch_id}/reject
GET  /patches/{patch_id}/review
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

The agent run start endpoint executes an iterative model/tool loop through controlled workspace
tools. The mock provider drives deterministic local tests without an external model call. See
`docs/agent-runs.md` and `docs/agent-loop.md` from the repository root.

The patch endpoints inspect active agent-run workspaces, apply safe unified diffs, and store generated patches separately from hidden gold solutions. See `docs/patch-management.md` from the repository root.

The test execution endpoints run configured setup, baseline, and post-patch commands for an active agent run and store each command result. See `docs/test-execution.md` from the repository root.

The evaluation endpoints calculate and return idempotent benchmark metrics for completed agent runs. See `docs/evaluation-metrics.md` from the repository root.

The patch review endpoints store human approve/reject decisions for generated patches and expose review status in patch/run responses. See `docs/human-approval.md` from the repository root.

Database schema changes are managed with Alembic migrations. See `docs/database-migrations.md` from the repository root.
