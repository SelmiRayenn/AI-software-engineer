# Architecture

The platform evaluates AI software engineering agents against real historical GitHub issues. Each benchmark case should be reproducible from a repository URL, issue metadata, base commit, dependency setup, and expected test command.

## System Components

### Frontend

The React dashboard is the operator surface for benchmark suites, sandbox runs, patch review, and benchmark metrics. The scaffold currently shows the intended workflow and backend health status.

### Backend API

The FastAPI service owns platform orchestration. Its future responsibilities include:

- Ingesting GitHub issue metadata and repository references.
- Creating benchmark run records.
- Scheduling sandbox execution jobs.
- Calling model-provider adapters.
- Storing patch, test, approval, and metric results.
- Serving dashboard APIs.

The current GitHub ingestion boundary is read-only. It previews public repository, issue, pull request, file, and commit metadata through the GitHub REST API, with optional `GITHUB_TOKEN` support for higher rate limits.

Benchmark task creation uses a separate service layer above GitHub ingestion. It stores agent-visible task context on `benchmark_tasks` and stores hidden solution data on `gold_patches`, which is exposed only through evaluation/admin routes.

### PostgreSQL

PostgreSQL is the source of truth for benchmark cases, gold patches, agent runs, generated patches, agent events, test outcomes, approval decisions, and evaluation metrics.
Schema changes are managed with Alembic migrations under `backend/alembic`.

The initial schema includes:

- `repositories`
- `benchmark_tasks`
- `gold_patches`
- `agent_runs`
- `agent_events`
- `generated_patches`
- `test_results`
- `evaluation_metrics`
- `human_reviews`

### Docker Sandbox

The sandbox layer will isolate each benchmark run. A run should clone or mount the target repository, check out the historical base commit, apply the generated patch, execute the declared test command, and export logs plus artifacts.

### Controlled Agent Tools

Agents do not get unrestricted shell access. The backend exposes an internal tool layer for repository inspection and edits inside a prepared sandbox workspace: file listing, code search, file reads/writes, allowed test commands, diff inspection, and patch submission. Each tool call enforces workspace path boundaries and writes an `agent_tool_call` event to `agent_events`.

### Patch Management

Generated patch handling is separate from gold solution storage. The patch service reads current
workspace diffs, validates and applies unified diffs, rejects traversal/gold/binary patch inputs,
enforces patch size limits, and stores generated agent patches on `generated_patches`.

### Test Execution

The test execution layer runs benchmark-defined setup and test commands for each agent run. It
records setup, baseline, and post-patch results in `test_results`, captures stdout/stderr with size
limits, and rejects commands that are not configured on the benchmark task.

### Evaluation Metrics

The evaluation layer summarizes completed runs into `evaluation_metrics`. It compares inspected
files against hidden gold changed files, checks generated patch scope, reads post-patch test
outcomes, aggregates model token/cost events, and records runtime from run timestamps.

### Human Approval

Generated patches are not publishable by default. A human reviewer must approve or reject each
`GeneratedPatch`, creating a `human_reviews` record. Rejected patches are blocked from future
export or pull request creation, while approved patches become eligible for those later workflows.
Approval endpoints are outside the controlled agent tool layer.

### Agent Provider Abstraction

The backend contains a provider interface boundary under `app.model_providers`. Mock, OpenAI, Anthropic, and local adapters share the same `ModelProvider` contract and return normalized content, tool calls, token usage, cost estimates, latency, and optional raw provider responses.

## Evaluation Flow

1. A benchmark case references a real GitHub issue, repository, base commit, and test command.
2. The backend creates a run and provisions an isolated Docker sandbox.
3. The selected model provider receives the issue context and repository state instructions.
4. The provider returns a candidate patch.
5. The platform records baseline test results before applying the generated patch.
6. The sandbox applies the patch and executes post-patch tests.
7. The platform stores the patch, logs, pass/fail status, cost, latency, and token usage.
8. A human reviewer approves, rejects, or annotates the run.
9. Approved runs contribute to benchmark metrics and leaderboard-style reporting.

## First Working Version

The first working version should keep implementation depth narrow:

- Add a sandbox job stub that can execute a fixed command in a controlled container.
- Add one provider adapter behind the `ModelProvider` interface.
- Store raw patches and test logs before building richer analytics.
- Keep human approval as a simple status transition before adding collaboration features.
- Keep schema changes behind reviewed Alembic migrations.

## Boundaries

The scaffold does not implement the full coding agent, benchmark scheduler, Docker execution engine, or metrics pipeline. It establishes the repository structure, database foundation, and extension points needed for those features.
