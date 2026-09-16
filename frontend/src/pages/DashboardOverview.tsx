import { useEffect, useMemo, useState } from "react";
import { getRunMetrics, listAgentRuns, listBenchmarkTasks, listRepositories } from "../api";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import { StatCard } from "../components/StatCard";
import type { AgentRun, BenchmarkTask, EvaluationMetric, Repository } from "../types/api";

interface OverviewState {
  repositories: Repository[];
  tasks: BenchmarkTask[];
  runs: AgentRun[];
  metrics: EvaluationMetric[];
}

export function DashboardOverview() {
  const [state, setState] = useState<OverviewState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function load() {
      try {
        setError(null);
        const [repositories, tasks, runs] = await Promise.all([
          listRepositories(),
          listBenchmarkTasks(),
          listAgentRuns(),
        ]);
        const metricResults = await Promise.allSettled(
          runs
            .filter((run) => run.status === "completed")
            .map((run) => getRunMetrics(run.id)),
        );
        const metrics = metricResults
          .filter((result): result is PromiseFulfilledResult<EvaluationMetric | null> => {
            return result.status === "fulfilled";
          })
          .map((result) => result.value)
          .filter((metric): metric is EvaluationMetric => metric !== null);

        if (active) {
          setState({ repositories, tasks, runs, metrics });
        }
      } catch (loadError) {
        if (active) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load dashboard.");
        }
      }
    }

    void load();
    return () => {
      active = false;
    };
  }, []);

  const summary = useMemo(() => {
    const runs = state?.runs ?? [];
    const metrics = state?.metrics ?? [];
    const completedRuns = runs.filter((run) => run.status === "completed").length;
    const failedRuns = runs.filter((run) => run.status === "failed").length;
    const passRate =
      metrics.length === 0
        ? "N/A"
        : `${Math.round(
            (metrics.filter((metric) => metric.post_patch_tests_passed).length / metrics.length) *
              100,
          )}%`;

    return {
      totalTasks: state?.tasks.length ?? 0,
      totalRuns: runs.length,
      completedRuns,
      failedRuns,
      passRate,
      metricCount: metrics.length,
    };
  }, [state]);

  if (error) {
    return <ErrorState title="Dashboard unavailable" message={error} />;
  }
  if (!state) {
    return <LoadingState title="Loading dashboard" />;
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Overview"
        title="Evaluation dashboard"
        description="Current benchmark task and agent run activity from the backend."
      />
      <section className="summary-grid" aria-label="Dashboard summary">
        <StatCard label="Total Tasks" value={summary.totalTasks} detail="Benchmark tasks" />
        <StatCard label="Total Runs" value={summary.totalRuns} detail="Agent executions" />
        <StatCard label="Completed Runs" value={summary.completedRuns} detail="Finished runs" />
        <StatCard label="Failed Runs" value={summary.failedRuns} detail="Needs attention" />
        <StatCard
          label="Test Pass Rate"
          value={summary.passRate}
          detail={`${summary.metricCount} evaluated runs`}
        />
      </section>
      {state.tasks.length === 0 && state.runs.length === 0 ? (
        <EmptyState
          title="No benchmark records yet"
          message="Create or seed benchmark tasks to populate this dashboard."
        />
      ) : (
        <section className="panel">
          <div className="panel-header">
            <h3>System Snapshot</h3>
            <span>{state.repositories.length} repositories tracked</span>
          </div>
          <div className="activity-list">
            {state.runs.slice(0, 6).map((run) => (
              <div className="activity-row" key={run.id}>
                <div>
                  <strong>{run.model_provider}</strong>
                  <span>{run.model_name}</span>
                </div>
                <span>{run.status}</span>
              </div>
            ))}
            {state.runs.length === 0 ? <p className="muted">No runs started yet.</p> : null}
          </div>
        </section>
      )}
    </div>
  );
}
