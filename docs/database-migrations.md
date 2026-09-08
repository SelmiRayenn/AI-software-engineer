# Database Migrations

The backend uses Alembic to manage SQLAlchemy schema changes for PostgreSQL.

## Configuration

Alembic is configured under:

- `backend/alembic.ini`
- `backend/alembic/env.py`
- `backend/alembic/versions/`

The migration environment imports the backend SQLAlchemy metadata from `app.db.base.Base` after
loading `app.models`. It reads `DATABASE_URL` through the same backend settings used by the FastAPI
application.

## Apply Migrations

From the backend directory:

```powershell
python scripts/apply_migrations.py
```

Equivalent Alembic command:

```powershell
alembic upgrade head
```

Docker Compose also runs `alembic upgrade head` before starting the backend service.

## Create A Migration

After changing SQLAlchemy models, create a new migration from the backend directory:

```powershell
python scripts/create_migration.py "describe schema change"
```

Equivalent Alembic command:

```powershell
alembic revision --autogenerate -m "describe schema change"
```

Review the generated migration before committing it. Autogeneration should be treated as a draft,
especially for data migrations, backfills, constraint changes, and destructive operations.

## Check Migration Status

From the backend directory:

```powershell
python scripts/migration_status.py
```

Equivalent Alembic command:

```powershell
alembic current
alembic heads
```

## Initial Migration

The first migration, `20260904_0001_initial_schema`, creates the foundation schema:

- `repositories`
- `benchmark_tasks`
- `gold_patches`
- `agent_runs`
- `agent_events`
- `generated_patches`
- `test_results`
- `evaluation_metrics`
- `human_reviews`

It includes the current task, run, patch, review, test, event, metric, repository, and gold solution
tables, plus recently added fields such as `pull_request_number`, `workspace_id`, and
`workspace_path`.

## Repository Index Migration

Migration `20260905_0002` adds the repository indexing tables `repository_indexes`, `indexed_files`,
and `indexed_symbols` without recreating the existing schema. See `docs/repository-indexing.md` for
the indexing API and workspace requirements.

Migration `20260905_0003` adds `indexed_chunks` and `chunk_embeddings` for optional semantic
retrieval. It follows `20260905_0002` and preserves existing indexes. Apply it with the same
`python scripts/apply_migrations.py` command. Chunks and vectors are populated by an explicit
embedding build, not by the schema migration. No PostgreSQL vector extension is required.

## Auto-Create Tables

`DATABASE_AUTO_CREATE_TABLES` remains available for isolated tests or throwaway local experiments,
but it is disabled by default. Normal development and Docker workflows should use Alembic
migrations instead.
