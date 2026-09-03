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

### PostgreSQL

PostgreSQL is the source of truth for benchmark cases, gold patches, agent runs, generated patches, agent events, test outcomes, approval decisions, and evaluation metrics.

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

### Agent Provider Abstraction

The backend contains an initial provider interface boundary. OpenAI, Anthropic, and local model adapters can later implement the same `ModelProvider` contract without changing benchmark orchestration code.

## Evaluation Flow

1. A benchmark case references a real GitHub issue, repository, base commit, and test command.
2. The backend creates a run and provisions an isolated Docker sandbox.
3. The selected model provider receives the issue context and repository state instructions.
4. The provider returns a candidate patch.
5. The sandbox applies the patch and executes tests.
6. The platform stores the patch, logs, pass/fail status, cost, latency, and token usage.
7. A human reviewer approves, rejects, or annotates the run.
8. Approved runs contribute to benchmark metrics and leaderboard-style reporting.

## First Working Version

The first working version should keep implementation depth narrow:

- Add a sandbox job stub that can execute a fixed command in a controlled container.
- Add one provider adapter behind the `ModelProvider` interface.
- Store raw patches and test logs before building richer analytics.
- Keep human approval as a simple status transition before adding collaboration features.
- Add migrations once the initial schema stabilizes beyond the scaffold.

## Boundaries

The scaffold does not implement the full coding agent, benchmark scheduler, Docker execution engine, or metrics pipeline. It establishes the repository structure, database foundation, and extension points needed for those features.
