import { useEffect, useMemo, useState } from "react";
import {
  getAnalyticsByPack,
  getAnalyticsByRepository,
  getAnalyticsSummary,
  getModelLeaderboard,
  listBenchmarkPacks,
  listRepositories,
} from "../api";
import {
  AnalyticsFilters,
  emptyAnalyticsFilters,
  type AnalyticsFilterValue,
} from "../components/AnalyticsFilters";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import type {
  AnalyticsSummary,
  BenchmarkPack,
  PackAnalytics,
  Repository,
  RepositoryAnalytics,
} from "../types/api";
import {
  formatMoney,
  formatPercent,
  formatMetricNumber,
  formatSeconds,
} from "../utils/format";

interface AnalyticsData {
  summary: AnalyticsSummary;
  repositories: RepositoryAnalytics[];
  packs: PackAnalytics[];
}

interface FilterOptions {
  repositories: Repository[];
  packs: BenchmarkPack[];
  providers: string[];
}

export function AnalyticsOverviewPage() {
  const [filters, setFilters] = useState<AnalyticsFilterValue>(emptyAnalyticsFilters);
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([listRepositories(), listBenchmarkPacks(), getModelLeaderboard()])
      .then(([repositories, packs, leaderboard]) => {
        if (!active) return;
        setOptions({
          repositories,
          packs,
          providers: [...new Set(leaderboard.map((row) => row.model_provider))].sort(),
        });
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError, "Failed to load analytics filters."));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    const requestFilters = {
      modelProvider: filters.modelProvider || undefined,
      repositoryId: filters.repositoryId || undefined,
      benchmarkPackId: filters.benchmarkPackId || undefined,
    };
    Promise.all([
      getAnalyticsSummary(requestFilters),
      getAnalyticsByRepository(requestFilters),
      getAnalyticsByPack(requestFilters),
    ])
      .then(([summary, repositories, packs]) => {
        if (active) setData({ summary, repositories, packs });
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError, "Failed to load analytics."));
      });
    return () => {
      active = false;
    };
  }, [filters]);

  const evaluatedDetail = useMemo(() => {
    if (!data) return "";
    return `${data.summary.completed_runs} completed / ${data.summary.failed_runs} failed`;
  }, [data]);

  if (error) return <ErrorState title="Analytics unavailable" message={error} />;
  if (!options || !data) return <LoadingState title="Loading analytics" />;

  const { summary } = data;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Analytics"
        title="Benchmark performance"
        description="Aggregate execution, test, issue-resolution, cost, and localization metrics."
      />
      <AnalyticsFilters
        value={filters}
        providers={options.providers}
        repositories={options.repositories}
        packs={options.packs}
        onChange={setFilters}
      />
      <section className="analytics-metric-grid" aria-label="Aggregate metrics">
        <Metric label="Total runs" value={String(summary.total_runs)} detail={evaluatedDetail} />
        <Metric label="Completed" value={String(summary.completed_runs)} />
        <Metric label="Failed" value={String(summary.failed_runs)} tone="negative" />
        <Metric label="Patch apply" value={formatPercent(summary.patch_apply_rate)} />
        <Metric label="Visible tests" value={formatPercent(summary.visible_test_pass_rate)} />
        <Metric
          label="Hidden tests"
          value={formatPercent(summary.hidden_test_pass_rate)}
          detail={summary.hidden_test_pass_rate === null ? "Not evaluated" : undefined}
        />
        <Metric
          label="Issue resolved"
          value={formatPercent(summary.issue_resolved_rate)}
          tone="positive"
        />
        <Metric
          label="Regression rate"
          value={formatPercent(summary.regression_rate)}
          tone={summary.regression_rate > 0 ? "negative" : undefined}
        />
        <Metric
          label="Avg. localization"
          value={formatMetricNumber(summary.average_file_localization_score, 3)}
        />
        <Metric
          label="Avg. issue score"
          value={formatMetricNumber(summary.average_issue_specific_score, 3)}
        />
        <Metric
          label="Total cost"
          value={formatMoney(summary.total_cost)}
          detail={`${formatMoney(summary.average_cost_per_run)} / run`}
        />
        <Metric
          label="Avg. execution"
          value={formatSeconds(summary.average_execution_time_seconds)}
        />
      </section>

      {summary.total_runs === 0 ? (
        <EmptyState
          title="No matching runs"
          message="Adjust the filters or execute benchmark tasks to populate analytics."
        />
      ) : (
        <div className="analytics-group-grid">
          <GroupedRepositoryTable rows={data.repositories} />
          <GroupedPackTable rows={data.packs} />
        </div>
      )}
    </div>
  );
}

interface MetricProps {
  label: string;
  value: string;
  detail?: string;
  tone?: "positive" | "negative";
}

function Metric({ label, value, detail, tone }: MetricProps) {
  return (
    <article className="analytics-metric" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </article>
  );
}

function GroupedRepositoryTable({ rows }: { rows: RepositoryAnalytics[] }) {
  return (
    <section className="panel table-panel">
      <div className="panel-header">
        <h3>By repository</h3>
        <span>{rows.length} represented</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted">No repository groups match these filters.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Repository</th>
              <th>Runs</th>
              <th>Resolved</th>
              <th>Visible</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.repository_id}>
                <td>
                  <strong>{row.repository_owner}/{row.repository_name}</strong>
                </td>
                <td>{row.total_runs}</td>
                <td>{formatPercent(row.issue_resolved_rate)}</td>
                <td>{formatPercent(row.visible_test_pass_rate)}</td>
                <td>{formatMoney(row.total_cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function GroupedPackTable({ rows }: { rows: PackAnalytics[] }) {
  return (
    <section className="panel table-panel">
      <div className="panel-header">
        <h3>By benchmark pack</h3>
        <span>{rows.length} represented</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted">No pack-run groups match these filters.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Pack</th>
              <th>Runs</th>
              <th>Resolved</th>
              <th>Hidden</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.benchmark_pack_id}>
                <td>
                  <strong>{row.pack_name}</strong>
                  <span>{row.pack_version}</span>
                </td>
                <td>{row.total_runs}</td>
                <td>{formatPercent(row.issue_resolved_rate)}</td>
                <td>{formatPercent(row.hidden_test_pass_rate)}</td>
                <td>{formatMoney(row.total_cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
