# AI Software Engineering Agent Benchmark Platform

This repository is the starting scaffold for a benchmark platform that evaluates AI coding agents against real historical GitHub issues. The intended system runs each issue in an isolated Docker sandbox, asks an agent to produce a patch, executes tests, routes the result through human approval, and records benchmark metrics.

The first version is intentionally small: it provides the monorepo shape, a FastAPI backend with `/health`, a React + TypeScript dashboard shell, PostgreSQL wiring, and architecture notes. The full agent loop is not implemented yet.

## Repository Layout

```text
backend/      FastAPI API service and future benchmark orchestration code
frontend/     React + TypeScript dashboard
docker/       Docker support files, including PostgreSQL init hooks
docs/         Architecture and product notes
scripts/      Developer and operations scripts
benchmarks/   Benchmark suite definitions, fixtures, and run outputs
```

## Quick Start

Copy the example environment file first:

```powershell
Copy-Item .env.example .env
```

Run the full stack with Docker Compose:

```powershell
docker compose up --build
```

The backend health check is available at:

```text
http://localhost:8000/health
```

The frontend dashboard is available at:

```text
http://localhost:5173
```

## Backend

Run the backend locally:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The API currently exposes:

- `GET /health` for service health.

## Frontend

Run the frontend locally:

```powershell
cd frontend
npm install
npm run dev
```

The dashboard shell reads `VITE_API_BASE_URL` and displays the backend health status when available.

## Database Foundation

The backend uses SQLAlchemy with PostgreSQL. Development Docker runs set `DATABASE_AUTO_CREATE_TABLES=true`, so the FastAPI app creates the initial tables on startup. For local development, create and seed the tables from the backend directory:

```powershell
cd backend
python scripts/create_tables.py
python scripts/seed_dev.py
```

Load the small manual draft benchmark dataset from the repository root:

```powershell
python scripts/load_manual_benchmarks.py
```

Initial API surfaces:

- `GET /api/v1/repositories`
- `POST /api/v1/repositories`
- `GET /api/v1/benchmark-tasks`
- `POST /api/v1/benchmark-tasks`
- `GET /api/v1/agent-runs`
- `POST /api/v1/agent-runs`
- `POST /sandbox/run`
- `POST /github/preview-issue`
- `POST /github/preview-pr`
- `POST /benchmark-tasks/from-github`
- `POST /benchmark-tasks/{task_id}/validate`
- `POST /benchmark-tasks/{task_id}/mark-ready`
- `POST /agent-runs/{benchmark_task_id}/start`
- `GET /evaluation/benchmark-tasks/{task_id}/gold-patch`

## Benchmark Task Lifecycle

Benchmark tasks move through a small lifecycle:

```text
draft -> ready -> running -> completed
                -> failed
```

Tasks can also be archived from stable end states. A task can only be marked `ready` after validation passes. Validation checks the repository URL, issue and pull request numbers, base commit, setup/test command lists, hidden gold patch, changed files, and known task status.

The manual dataset in `benchmarks/manual_tasks.json` intentionally loads as draft tasks. These are curation starting points for small Python repositories and must be verified against exact historical issue, PR, base commit, and gold patch data before use.

## Current Scope

Included now:

- Monorepo project structure
- FastAPI app factory and health route
- React + TypeScript dashboard shell
- Docker Compose draft for PostgreSQL, backend, and frontend
- Environment template
- High-level architecture documentation
- Initial model-provider interface placeholders
- SQLAlchemy models for repositories, benchmark tasks, patches, runs, events, test results, metrics, and reviews
- Docker sandbox proof of concept for checked-out repositories and command execution
- GitHub issue and pull request preview ingestion for public repositories
- Historical benchmark task creation from GitHub issues and merged fix PRs
- Swappable model provider abstraction for mock, OpenAI, Anthropic, and local providers
- Scripted agent run orchestrator that uses controlled tools and a mock provider

Not included yet:

- Agent patch generation
- Test execution engine
- Human approval workflow
- Full benchmark metrics computation
