import { useCallback, useEffect, useMemo, useState } from "react";
import {
  approvePatch,
  getAgentRunDetails,
  getPatchReview,
  getRunPatch,
  getRunTests,
  rejectPatch,
} from "../api";
import { ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import type {
  AgentRunDetail,
  GeneratedPatch,
  PatchReview,
  TestResult,
} from "../types/api";
import { formatDuration, shortId } from "../utils/format";

interface AgentRunDetailPageProps {
  runId: string;
  onNavigate: (path: string) => void;
}

interface DetailState {
  run: AgentRunDetail;
  patch: GeneratedPatch | null;
  review: PatchReview | null;
  tests: TestResult[];
}

interface TestPhaseGroup {
  phase: string;
  results: TestResult[];
}

const TEST_PHASE_ORDER = ["setup", "baseline", "post_patch", "hidden_eval"];

const TEST_PHASE_LABELS: Record<string, string> = {
  setup: "Setup",
  baseline: "Baseline",
  post_patch: "Post-patch",
  hidden_eval: "Hidden eval",
};

export function AgentRunDetailPage({ runId, onNavigate }: AgentRunDetailPageProps) {
  const [state, setState] = useState<DetailState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reviewerName, setReviewerName] = useState("");
  const [reviewNotes, setReviewNotes] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState(false);

  const loadDetail = useCallback(async (): Promise<DetailState> => {
    const run = await getAgentRunDetails(runId);
    const [patch, tests] = await Promise.all([
      getRunPatch(runId),
      getRunTests(runId),
    ]);
    const review = patch ? await getPatchReview(patch.id) : null;
    return { run, patch, review, tests };
  }, [runId]);

  useEffect(() => {
    let active = true;
    loadDetail()
      .then((detailState) => {
        if (active) {
          setState(detailState);
          setError(null);
        }
      })
      .catch((loadError) => {
        if (active) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load run.");
        }
      });
    return () => {
      active = false;
    };
  }, [loadDetail]);

  const testsByPhase = useMemo(
    () => groupTestsByPhase(state?.tests ?? []),
    [state?.tests],
  );

  async function submitReview(decision: "approve" | "reject") {
    if (!state?.patch) {
      return;
    }

    const reviewStatus = currentReviewStatus(state);
    if (isFinalReviewStatus(reviewStatus)) {
      setValidationError("This patch already has a completed human review.");
      return;
    }

    const reviewer = reviewerName.trim();
    const notes = reviewNotes.trim();
    if (!reviewer) {
      setValidationError("Reviewer name is required.");
      return;
    }
    if (decision === "reject" && !notes) {
      setValidationError("Review notes are required when rejecting a patch.");
      return;
    }

    setActionPending(true);
    setValidationError(null);
    setActionError(null);
    try {
      if (decision === "approve") {
        await approvePatch(state.patch.id, {
          reviewer_name: reviewer,
          review_notes: notes || null,
        });
      } else {
        await rejectPatch(state.patch.id, {
          reviewer_name: reviewer,
          review_notes: notes,
        });
      }
      setState(await loadDetail());
    } catch (reviewError) {
      setActionError(reviewError instanceof Error ? reviewError.message : "Review failed.");
    } finally {
      setActionPending(false);
    }
  }

  if (error) {
    return <ErrorState title="Run unavailable" message={error} />;
  }
  if (!state) {
    return <LoadingState title="Loading agent run" />;
  }

  const reviewStatus = currentReviewStatus(state);
  const reviewRecord = state.review?.review ?? null;
  const reviewLocked = isFinalReviewStatus(reviewStatus);
  const failedRun = state.run.status === "failed";

  return (
    <div className="page-stack">
      <PageHeader
        action={
          <button className="secondary-button" onClick={() => onNavigate("/runs")} type="button">
            Back to runs
          </button>
        }
        eyebrow="Agent Run"
        title={`Run ${shortId(state.run.id)}`}
        description={`#${state.run.benchmark_task.issue_number} in ${state.run.repository.owner}/${state.run.repository.name}`}
      />

      {failedRun ? (
        <section className="run-alert" role="status">
          <strong>
            {state.run.failure_category
              ? formatFailureCategory(state.run.failure_category)
              : "Run failed"}
          </strong>
          <span>
            {state.run.failure_summary ??
              "Patch, tests, and metrics may be incomplete for this execution."}
          </span>
        </section>
      ) : null}

      <section className="detail-grid overview-grid">
        <article className="panel">
          <div className="panel-header">
            <h3>Run Status</h3>
            <StatusBadge value={state.run.status} />
          </div>
          <dl className="definition-list">
            <div>
              <dt>Model</dt>
              <dd>
                {state.run.model_provider}
                <span>{state.run.model_name}</span>
              </dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>{formatTimestamp(state.run.started_at)}</dd>
            </div>
            <div>
              <dt>Completed</dt>
              <dd>{formatTimestamp(state.run.completed_at)}</dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd>{formatDuration(state.run.started_at, state.run.completed_at)}</dd>
            </div>
          </dl>
        </article>

        <article className="panel">
          <div className="panel-header">
            <h3>Issue Context</h3>
            <StatusBadge value={state.run.status} />
          </div>
          <dl className="definition-list">
            <div>
              <dt>Issue</dt>
              <dd>
                #{state.run.benchmark_task.issue_number}
                <span>{state.run.benchmark_task.issue_title}</span>
              </dd>
            </div>
            <div>
              <dt>Repository</dt>
              <dd>
                {state.run.repository.owner}/{state.run.repository.name}
                <span>{state.run.repository.url}</span>
              </dd>
            </div>
            <div>
              <dt>Benchmark Task</dt>
              <dd className="mono">{shortId(state.run.benchmark_task_id)}</dd>
            </div>
          </dl>
        </article>

        <article className="panel">
          <div className="panel-header">
            <h3>Human Review</h3>
            <StatusBadge value={reviewStatus} />
          </div>
          {state.patch ? (
            <div className={`review-state review-state-${reviewStatus}`}>
              <strong>{reviewStatusLabel(reviewStatus)}</strong>
              <span>{reviewStatusDescription(reviewStatus, state.review?.export_eligible)}</span>
              {reviewRecord ? (
                <dl className="compact-definition-list">
                  <div>
                    <dt>Reviewer</dt>
                    <dd>{reviewRecord.reviewer_name ?? "Unknown"}</dd>
                  </div>
                  <div>
                    <dt>Reviewed</dt>
                    <dd>{formatTimestamp(reviewRecord.reviewed_at)}</dd>
                  </div>
                  {reviewRecord.review_notes ? (
                    <div>
                      <dt>Notes</dt>
                      <dd>{reviewRecord.review_notes}</dd>
                    </div>
                  ) : null}
                </dl>
              ) : null}
            </div>
          ) : (
            <p className="muted">No generated patch is available for review.</p>
          )}
        </article>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Generated Patch</h3>
          {state.patch ? <span>{formatTimestamp(state.patch.created_at)}</span> : null}
        </div>
        {state.patch ? (
          <>
            <div className="patch-meta">
              <span>{state.patch.changed_files.length} files changed</span>
              <span>{state.patch.stats.additions} additions</span>
              <span>{state.patch.stats.deletions} deletions</span>
              <span>{state.patch.stats.size_bytes} bytes</span>
            </div>
            {state.patch.changed_files.length > 0 ? (
              <ul className="changed-file-list" aria-label="Changed files">
                {state.patch.changed_files.map((filePath) => (
                  <li className="file-pill mono" key={filePath}>
                    {filePath}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">No changed files were reported for this patch.</p>
            )}
            <DiffViewer patchText={state.patch.patch_text} />
          </>
        ) : (
          <p className="muted">No patch has been submitted for this run yet.</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Patch Approval</h3>
          <StatusBadge value={reviewStatus} />
        </div>
        {!state.patch ? (
          <p className="muted">Approval controls appear after a generated patch is stored.</p>
        ) : reviewLocked ? (
          <p className="muted">
            This patch is already {reviewStatus}. Create an explicit review update from the API if
            the decision must change.
          </p>
        ) : (
          <div className="review-form">
            <div className="form-grid">
              <label>
                <span>Reviewer Name</span>
                <input
                  aria-label="Reviewer name"
                  onChange={(event) => {
                    setReviewerName(event.target.value);
                    setValidationError(null);
                  }}
                  placeholder="Reviewer name"
                  value={reviewerName}
                />
              </label>
              <label>
                <span>Review Notes</span>
                <textarea
                  aria-label="Review notes"
                  onChange={(event) => {
                    setReviewNotes(event.target.value);
                    setValidationError(null);
                  }}
                  placeholder="Required for rejection"
                  rows={3}
                  value={reviewNotes}
                />
              </label>
            </div>
            {validationError || actionError ? (
              <p className="form-error">{validationError ?? actionError}</p>
            ) : null}
            <div className="button-row">
              <button
                disabled={actionPending}
                onClick={() => void submitReview("approve")}
                type="button"
              >
                Approve Patch
              </button>
              <button
                className="danger-button"
                disabled={actionPending}
                onClick={() => void submitReview("reject")}
                type="button"
              >
                Reject Patch
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Test Results</h3>
          <span>{state.tests.length} commands</span>
        </div>
        {testsByPhase.length === 0 ? (
          <p className="muted">No setup, baseline, or post-patch test logs are available yet.</p>
        ) : (
          <div className="test-phase-list">
            {testsByPhase.map((group) => (
              <section className="test-phase" key={group.phase}>
                <div className="test-phase-header">
                  <h4>{phaseLabel(group.phase)}</h4>
                  <span>{phaseSummary(group.results)}</span>
                </div>
                <div className="test-result-list">
                  {group.results.map((test) => (
                    <details className="test-result" key={test.id} open={!test.passed}>
                      <summary>
                        <span className="mono test-command">{test.command}</span>
                        <span className={test.passed ? "result-pass" : "result-fail"}>
                          {test.passed ? "Passed" : "Failed"} | exit {test.exit_code} |{" "}
                          {formatSeconds(test.duration_seconds)}
                        </span>
                      </summary>
                      <div className="log-grid">
                        <div>
                          <strong>stdout</strong>
                          <pre className="log-block">{test.stdout?.trim() ? test.stdout : "No stdout captured."}</pre>
                        </div>
                        <div>
                          <strong>stderr</strong>
                          <pre className="log-block">{test.stderr?.trim() ? test.stderr : "No stderr captured."}</pre>
                        </div>
                      </div>
                    </details>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h3>Evaluation Metrics</h3>
          <span>{state.run.metric_summary ? "Available" : "Not evaluated"}</span>
        </div>
        {state.run.metric_summary ? (
          <div className="metric-grid">
            <MetricItem
              detail={issueConfidenceDetail(state.run.metric_summary.hidden_tests_run_count)}
              label="Issue Resolved"
              tone={state.run.metric_summary.issue_resolved ? "positive" : "negative"}
              value={state.run.metric_summary.issue_resolved ? "Yes" : "No"}
            />
            <MetricItem
              label="Issue Score"
              value={formatPercent(state.run.metric_summary.issue_specific_score)}
            />
            <MetricItem
              detail="Before patch"
              label="Baseline Tests"
              value={testStatus(state.run.metric_summary.baseline_tests_passed)}
            />
            <MetricItem
              detail="After patch"
              label="Visible Tests"
              value={testStatus(state.run.metric_summary.post_patch_tests_passed)}
            />
            <MetricItem
              detail={
                state.run.metric_summary.hidden_tests_run_count > 0
                  ? `${state.run.metric_summary.hidden_tests_run_count} commands run`
                  : "Lower confidence without hidden evaluation"
              }
              label="Hidden Eval"
              value={hiddenTestStatus(
                state.run.metric_summary.hidden_tests_passed,
                state.run.metric_summary.hidden_tests_run_count,
              )}
            />
            <MetricItem
              label="Regression"
              tone={state.run.metric_summary.regression_detected ? "negative" : "neutral"}
              value={state.run.metric_summary.regression_detected ? "Detected" : "None"}
            />
            <MetricItem
              label="File Localization"
              value={formatPercent(state.run.metric_summary.file_localization_score)}
            />
            <MetricItem
              label="Patch Applied"
              value={state.run.metric_summary.patch_applied ? "Yes" : "No"}
            />
            <MetricItem
              label="Modified Files"
              value={state.run.metric_summary.modified_files_count.toString()}
            />
            <MetricItem
              label="Unrelated Files"
              value={state.run.metric_summary.unrelated_files_count.toString()}
            />
            <MetricItem
              label="Tokens Used"
              value={formatNumber(state.run.metric_summary.tokens_used)}
            />
            <MetricItem
              label="Estimated Cost"
              value={formatCost(state.run.metric_summary.estimated_cost)}
            />
            <MetricItem
              label="Execution Time"
              value={formatSeconds(state.run.metric_summary.execution_time_seconds)}
            />
          </div>
        ) : (
          <p className="muted">Metrics have not been calculated for this run yet.</p>
        )}
      </section>
    </div>
  );
}

function DiffViewer({ patchText }: { patchText: string }) {
  if (!patchText.trim()) {
    return <pre className="diff-viewer">No patch changes.</pre>;
  }

  return (
    <pre className="diff-viewer" aria-label="Generated patch diff">
      {patchText.split("\n").map((line, index) => (
        <span className={diffLineClass(line)} key={`${index}-${line.slice(0, 20)}`}>
          {line || " "}
        </span>
      ))}
    </pre>
  );
}

function MetricItem({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: "neutral" | "positive" | "negative";
}) {
  return (
    <div className="metric-item" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function testStatus(passed: boolean): string {
  return passed ? "Passed" : "Failed";
}

function hiddenTestStatus(passed: boolean | null, runCount: number): string {
  if (runCount === 0) {
    return "Not run";
  }
  return passed ? "Passed" : "Failed";
}

function issueConfidenceDetail(hiddenRunCount: number): string {
  return hiddenRunCount > 0
    ? "High confidence: hidden evaluation ran"
    : "Lower confidence: no hidden evaluation";
}

function currentReviewStatus(state: DetailState): string {
  return (
    state.review?.review_status ?? state.patch?.review_status ?? state.run.review_status ?? "none"
  );
}

function isFinalReviewStatus(status: string): boolean {
  return status === "approved" || status === "rejected";
}

function groupTestsByPhase(tests: TestResult[]): TestPhaseGroup[] {
  const groups = new Map<string, TestResult[]>();
  for (const test of tests) {
    const existing = groups.get(test.phase) ?? [];
    existing.push(test);
    groups.set(test.phase, existing);
  }

  const ordered: TestPhaseGroup[] = [];
  for (const phase of TEST_PHASE_ORDER) {
    const results = groups.get(phase);
    if (results) {
      ordered.push({ phase, results });
      groups.delete(phase);
    }
  }

  for (const phase of Array.from(groups.keys()).sort()) {
    ordered.push({ phase, results: groups.get(phase) ?? [] });
  }
  return ordered;
}

function phaseLabel(phase: string): string {
  return TEST_PHASE_LABELS[phase] ?? phase;
}

function phaseSummary(results: TestResult[]): string {
  const passed = results.filter((result) => result.passed).length;
  return `${passed}/${results.length} passed`;
}

function diffLineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) {
    return "diff-line diff-add";
  }
  if (line.startsWith("-") && !line.startsWith("---")) {
    return "diff-line diff-remove";
  }
  if (line.startsWith("@@")) {
    return "diff-line diff-hunk";
  }
  if (line.startsWith("diff --git") || line.startsWith("index ")) {
    return "diff-line diff-meta";
  }
  return "diff-line";
}

function reviewStatusLabel(status: string): string {
  if (status === "approved") {
    return "Approved";
  }
  if (status === "rejected") {
    return "Rejected";
  }
  return "Awaiting review";
}

function reviewStatusDescription(status: string, exportEligible?: boolean): string {
  if (status === "approved") {
    return exportEligible ? "Eligible for future export." : "Approved, but export is unavailable.";
  }
  if (status === "rejected") {
    return "Blocked from export or publishing.";
  }
  return "A human decision is required before export or publication.";
}

function formatFailureCategory(category: string): string {
  return category
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "Not completed";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "N/A";
  }
  return date.toLocaleString();
}

function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${value.toFixed(2)}s`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${Math.round(value * 100)}%`;
}

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "0";
  }
  return new Intl.NumberFormat().format(value);
}

function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "$0.0000";
  }
  return `$${value.toFixed(4)}`;
}
