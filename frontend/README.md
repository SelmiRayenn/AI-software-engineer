# Frontend

React and TypeScript dashboard for the AI software engineering agent benchmark platform.

## Local Development

Install dependencies:

```powershell
npm install
```

Run the dashboard:

```powershell
npm run dev
```

Build and type-check:

```powershell
npm run build
```

## Configuration

The dashboard reads the backend URL from:

```text
VITE_API_BASE_URL=http://localhost:8000
```

## Current Pages

- Overview dashboard with task, run, completion, failure, and evaluated test pass-rate summary.
- Benchmark task list.
- Agent run list.
- Agent run detail view with patch, tests, metrics, and human approve/reject actions.

The typed API client lives under `src/api`, and shared backend response types live under
`src/types`.
