# AI Software Engineering Agent Benchmark Platform

This repository is the starting scaffold for a benchmark platform that evaluates AI coding agents against real historical GitHub issues. The intended system runs each issue in an isolated Docker sandbox, asks an agent to produce a patch, executes tests, routes the result through human approval, and records benchmark metrics.

The current version provides the monorepo foundation, a FastAPI backend with `/health`, a React +
TypeScript dashboard, PostgreSQL migrations, controlled sandbox tools, and an iterative agent loop.

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

The dashboard reads `VITE_API_BASE_URL` and displays live backend data for benchmark tasks, agent
runs, generated patches, test results, metrics, and human patch review actions. Build and type-check
the frontend with:

```powershell
npm run build
```

## Database Foundation

The backend uses SQLAlchemy, PostgreSQL, and Alembic migrations. Development Docker runs apply
`alembic upgrade head` before the FastAPI app starts. For local development, apply migrations and
seed the tables from the backend directory:

```powershell
cd backend
python scripts/apply_migrations.py
python scripts/seed_dev.py
```

Load the small manual draft benchmark dataset from the repository root:

```powershell
python scripts/load_manual_benchmarks.py
```

Create and inspect migrations from the backend directory:

```powershell
python scripts/create_migration.py "describe schema change"
python scripts/migration_status.py
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
- `GET /agent-runs/{run_id}`
- `GET /agent-runs/{run_id}/diff`
- `GET /agent-runs/{run_id}/patch`
- `POST /agent-runs/{run_id}/patch/apply`
- `POST /agent-runs/{run_id}/tests/baseline`
- `POST /agent-runs/{run_id}/tests/post-patch`
- `GET /agent-runs/{run_id}/tests`
- `POST /agent-runs/{run_id}/evaluate`
- `GET /agent-runs/{run_id}/metrics`
- `POST /patches/{patch_id}/approve`
- `POST /patches/{patch_id}/reject`
- `GET /patches/{patch_id}/review`
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
- React + TypeScript dashboard
- Typed frontend API client and operational dashboard pages
- Docker Compose draft for PostgreSQL, backend, and frontend
- Environment template
- High-level architecture documentation
- Initial model-provider interface placeholders
- SQLAlchemy models for repositories, benchmark tasks, patches, runs, events, test results, metrics, and reviews
- Docker sandbox proof of concept for checked-out repositories and command execution
- GitHub issue and pull request preview ingestion for public repositories
- Historical benchmark task creation from GitHub issues and merged fix PRs
- Swappable model provider abstraction for mock, OpenAI, Anthropic, and local providers
- Configurable agent prompts and iterative tool loop with reproducible run settings
- Opt-in OpenAI Responses API integration, disabled by default behind a safety flag
- Patch management for sandbox workspace diffs, safe patch application, and generated patch records
- Baseline and post-patch test execution with stored command logs
- First evaluation metrics engine for localization, patches, tests, cost, and runtime
- Human approval workflow for approving or rejecting generated patches

Not included yet:

- Anthropic and local provider API adapters
- Pull request publishing/export
- Leaderboard-style benchmark analytics

## Patch Management

Generated agent patches are managed separately from hidden gold solutions. The backend can inspect
the current sandbox workspace diff, apply a unified diff after safety checks, store the result as a
`GeneratedPatch`, and return patch size statistics. Patch application is allowed only while a run is
`queued` or `running`; completed runs are immutable.

See `docs/patch-management.md` for endpoint examples and safety limits.

## Test Execution

Agent runs can record setup, baseline, and post-patch command results using only the commands
configured on the benchmark task. The orchestrator runs setup and baseline tests before the
model tool loop, then stores the submitted patch and runs post-patch tests.

See `docs/test-execution.md` for endpoint examples and behavior.

## Agent Loop

The orchestrator sends agent-visible issue context to the selected provider, validates structured
tool calls, returns controlled tool observations to the model, and repeats until patch submission or
a configured safety limit. Unknown and malformed tools are rejected, and common secrets are
redacted from event logs and stored prompt previews. Run configuration controls provider/model,
limits, issue comments, test-tool access, and scripted versus tool-loop execution.

See `docs/agent-loop.md` for the tool contracts, stop conditions, and event trace.

## Evaluation Metrics

Completed agent runs are summarized into a single idempotent `EvaluationMetric` row. The first
engine compares inspected files against hidden gold changed files, counts unrelated generated
changes, checks post-patch test results, aggregates token/cost events, and records execution time.

See `docs/evaluation-metrics.md` for the metric formulas.

## Human Approval

Generated patches must be reviewed by a human before any future export or pull request creation
step. The backend records one `HumanReview` per `GeneratedPatch`, exposes pending/approved/rejected
status in patch and run responses, and blocks rejected patches from export eligibility.

See `docs/human-approval.md` for endpoint examples and review rules.

See `docs/database-migrations.md` for the migration workflow.
