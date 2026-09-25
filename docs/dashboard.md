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
- Analytics: aggregate run counts, patch/test/issue rates, localization, cost, runtime, and
  repository/benchmark-pack breakdowns.
- Model leaderboard: provider/model performance with issue and test success, localization,
  efficiency metrics, composite score, and independent rank badges.

## Analytics Views

Open `/analytics` for the aggregate view or `/leaderboard` for model rankings. Both views provide
repository and benchmark-pack filters. The provider filter is sent to the aggregate analytics API
and applied to the loaded leaderboard rows on the leaderboard page.

Hidden-test pass rate displays `N/A` when no hidden evaluation was executed. This is distinct from
a measured 0% pass rate. Repository and pack sections use the backend's pack-run provenance, so a
task's current membership does not attribute unrelated runs to a pack.

The leaderboard is ordered by the backend composite score and displays issue-resolution, cost,
speed, and localization ranks. Exact ties can therefore share rank badges. The score formula and
rate denominators are documented in [aggregate analytics](analytics.md#model-leaderboard).

## Agent Run Review Flow

1. Open an agent run from the run history.
2. Confirm the linked issue title, repository, model, timestamps, and run status.
3. Inspect the latest agent plan, ranked candidate files, and root-cause hypothesis timeline, then
   the generated patch and changed file list.
4. Inspect the run trace to follow model calls, tool requests, observations, patch submissions,
   test phases, failures, and evaluation. Filter by event type, tool, or severity and expand the
   sanitized JSON payload only when deeper debugging context is needed.
5. Use the trace quick links to jump to the plan, candidates, hypotheses, patch, test, and metric
   sections.
6. Review setup, baseline, and post-patch test results by phase. Command logs are collapsible and scrollable.
7. Review baseline, visible post-patch, and hidden evaluation status. The issue outcome shows a
   `1.0`, `0.75`, `0.5`, or `0.0` issue-specific score and labels visible-only outcomes as lower
   confidence when no hidden tests ran.
8. Approve the patch with reviewer name and optional notes, or reject it with reviewer name and required notes.

Approved patches become eligible for a future export or pull request creation flow. Rejected patches are blocked from export.

## Empty States

The **Agent Plan** section on run detail displays the latest revision and accepted/rejected
status, issue summary, suspected root cause, observed evidence files, intended modifications,
test strategy, and risk/rollback notes. Rejected submissions show corrective feedback. Invalid
or unsafe content is not displayed. Older runs without a plan show "Not submitted".

Filter the trace by `plan_submitted` to inspect revision history; expand a payload for its
sanitized fields. Acceptance means the backend validated plan structure and evidence, not that
a human approved the patch or that the diagnosis is correct. The separate human-review controls
remain required for patch approval. See [planning policy](agent-loop.md#evidence-grounded-planning).

The **Candidate Files** section shows the agent's latest pre-edit ranking with rank position,
confidence, and rationale. Older runs without a candidate submission show an explicit empty state.
The file-localization page separately reports candidate top-1/top-3/top-5 accuracy, average ranking
size, and candidate hit rate by model.

The **Root-Cause Hypotheses** section shows every revision in order with status, confidence,
suspected files, supporting evidence, and superseding revision. The current active/confirmed
hypothesis is called out above the timeline. Filter the run trace by `hypothesis_submitted` to
correlate diagnostic changes with failed tests and repair attempts. Hypothesis status is agent
reasoning metadata, not human approval or proof that the diagnosis is correct.

The detail page explicitly handles runs with no patch, no tests, no metrics, failed execution
status, hidden evaluation not run, and already approved or rejected patches. Analytics pages also
handle empty filtered result sets independently from loading and API errors. Hidden commands and
logs remain restricted to trusted backend routes; the dashboard receives only aggregate hidden
status and counts. Trace payloads are redacted and bounded by the backend before rendering.

## Screenshot Placeholder

Add verified screenshots here after the first seeded run is available:

- `docs/assets/dashboard-run-detail-desktop.png`
- `docs/assets/dashboard-run-detail-mobile.png`
