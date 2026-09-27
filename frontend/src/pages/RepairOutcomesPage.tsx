import { useEffect, useState, type ReactNode } from "react";
import {
  getModelLeaderboard,
  getRepairOutcomes,
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
  BenchmarkPack,
  RepairFailureCount,
  RepairOutcomeAnalytics,
  RepairOutcomeMetricSet,
  RepairSuccessByModel,
  RepairSuccessByRepository,
  Repository,
} from "../types/api";
import { formatMetricNumber, formatMoney, formatPercent, formatSeconds } from "../utils/format";

interface FilterOptions {
  repositories: Repository[];
  packs: BenchmarkPack[];
  providers: string[];
}

export function RepairOutcomesPage() {
  const [filters, setFilters] = useState<AnalyticsFilterValue>(emptyAnalyticsFilters);
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [data, setData] = useState<RepairOutcomeAnalytics | null>(null);
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
        if (active) setError(errorMessage(loadError));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    getRepairOutcomes({
      modelProvider: filters.modelProvider || undefined,
      repositoryId: filters.repositoryId || undefined,
      benchmarkPackId: filters.benchmarkPackId || undefined,
    })
      .then((result) => {
        if (active) setData(result);
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError));
      });
    return () => {
      active = false;
    };
  }, [filters]);

  if (error) return <ErrorState title="Repair analytics unavailable" message={error} />;
  if (!options || !data) return <LoadingState title="Loading repair analytics" />;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Analytics"
        title="Repair outcomes"
        description="Whether bounded retries convert failed first patches into passing final patches."
      />
      <AnalyticsFilters
        value={filters}
        providers={options.providers}
        repositories={options.repositories}
        packs={options.packs}
        onChange={setFilters}
      />
      <section className="analytics-metric-grid" aria-label="Repair outcome metrics">
        <Metric label="Runs with repairs" value={String(data.total_runs_with_repairs)} />
        <Metric
          label="Average repairs"
          value={formatMetricNumber(data.average_repair_attempts, 2)}
        />
        <Metric label="Repair success" value={formatPercent(data.repair_success_rate)} tone="positive" />
        <Metric label="First patch pass" value={formatPercent(data.first_patch_pass_rate)} />
        <Metric
          label="Repaired patch pass"
          value={formatPercent(data.repaired_patch_pass_rate)}
          tone="positive"
        />
        <Metric
          label="Attempts exhausted"
          value={formatPercent(data.attempts_exhausted_rate)}
          tone="negative"
        />
        <Metric label="Average repair cost" value={formatMoney(data.average_cost_with_repairs)} />
        <Metric label="Average repair time" value={formatSeconds(data.average_time_with_repairs)} />
      </section>

      {data.total_runs_with_repairs === 0 ? (
        <EmptyState
          title="No repair attempts"
          message="No agent runs with additional patch attempts match the current filters."
        />
      ) : (
        <div className="repair-outcome-panels">
          <ModelTable rows={data.repair_success_by_model} />
          <RepositoryTable rows={data.repair_success_by_repository} />
          <div className="repair-failure-grid">
            <FailureTable
              title="Initial patch failures"
              rows={data.most_common_initial_failure_categories}
            />
            <FailureTable
              title="Repair attempt failures"
              rows={data.most_common_repair_failure_categories}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "positive" | "negative";
}) {
  return (
    <article className="analytics-metric" data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function ModelTable({ rows }: { rows: RepairSuccessByModel[] }) {
  return (
    <BreakdownTable title="Repair success by model" count={`${rows.length} configurations`}>
      {rows.map((row) => (
        <tr key={`${row.model_provider}:${row.model_name}`}>
          <td><strong>{row.model_name}</strong><span>{row.model_provider}</span></td>
          <OutcomeCells row={row} />
        </tr>
      ))}
    </BreakdownTable>
  );
}

function RepositoryTable({ rows }: { rows: RepairSuccessByRepository[] }) {
  return (
    <BreakdownTable title="Repair success by repository" count={`${rows.length} repositories`}>
      {rows.map((row) => (
        <tr key={row.repository_id}>
          <td><strong>{row.repository_owner}/{row.repository_name}</strong></td>
          <OutcomeCells row={row} />
        </tr>
      ))}
    </BreakdownTable>
  );
}

function BreakdownTable({
  title,
  count,
  children,
}: {
  title: string;
  count: string;
  children: ReactNode;
}) {
  return (
    <section className="panel table-panel repair-breakdown-panel">
      <div className="panel-header"><h3>{title}</h3><span>{count}</span></div>
      <table>
        <thead>
          <tr><th>Group</th><th>Runs</th><th>Avg attempts</th><th>Success</th><th>Final pass</th><th>Exhausted</th><th>Cost</th><th>Time</th></tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </section>
  );
}

function OutcomeCells({ row }: { row: RepairOutcomeMetricSet }) {
  return (
    <>
      <td>{row.total_runs_with_repairs}</td>
      <td>{formatMetricNumber(row.average_repair_attempts, 2)}</td>
      <td>{formatPercent(row.repair_success_rate)}</td>
      <td>{formatPercent(row.repaired_patch_pass_rate)}</td>
      <td>{formatPercent(row.attempts_exhausted_rate)}</td>
      <td>{formatMoney(row.average_cost_with_repairs)}</td>
      <td>{formatSeconds(row.average_time_with_repairs)}</td>
    </>
  );
}

function FailureTable({ title, rows }: { title: string; rows: RepairFailureCount[] }) {
  return (
    <section className="panel table-panel">
      <div className="panel-header"><h3>{title}</h3><span>{rows.length} categories</span></div>
      <table>
        <thead><tr><th>Category</th><th>Occurrences</th></tr></thead>
        <tbody>
          {rows.length ? rows.map((row) => (
            <tr key={row.category}><td><code>{row.category}</code></td><td>{row.count}</td></tr>
          )) : <tr><td className="muted" colSpan={2}>No failures recorded.</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load repair analytics.";
}
