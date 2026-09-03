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
```

The sandbox endpoint is a proof of concept for cloning a repository, checking out a commit, and running setup/test commands inside an isolated Docker container. See `docs/sandbox.md` from the repository root.
