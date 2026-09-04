# Dashboard

The React dashboard is the human-facing review surface for benchmark tasks and agent runs.

## Local Usage

Start the backend API, then run the frontend with:

```bash
cd frontend
npm install
npm run dev
```

Set `VITE_API_BASE_URL` when the backend is not running at the default origin.

```bash
VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

## Pages

- Dashboard overview: task and run totals, completion counts, failed runs, and available test pass rate.
- Benchmark tasks: historical issue tasks with repository, lifecycle status, and base commit context.
- Agent runs: run history with model, execution status, duration, and patch review state.
- Agent run detail: issue context, generated patch diff, changed files, grouped test logs, evaluation metrics, and human approval controls.

## Agent Run Review Flow

1. Open an agent run from the run history.
2. Confirm the linked issue title, repository, model, timestamps, and run status.
3. Inspect the generated patch and changed file list.
4. Review setup, baseline, and post-patch test results by phase. Command logs are collapsible and scrollable.
5. Review evaluation metrics when available.
6. Approve the patch with reviewer name and optional notes, or reject it with reviewer name and required notes.

Approved patches become eligible for a future export or pull request creation flow. Rejected patches are blocked from export.

## Empty States

The detail page explicitly handles runs with no patch, no tests, no metrics, failed execution status, and already approved or rejected patches.

## Screenshot Placeholder

Add verified screenshots here after the first seeded run is available:

- `docs/assets/dashboard-run-detail-desktop.png`
- `docs/assets/dashboard-run-detail-mobile.png`
