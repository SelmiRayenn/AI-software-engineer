import { useEffect, useState, type ReactNode } from "react";
import {
  getModelLeaderboard,
  getToolUsage,
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
  Repository,
  ToolErrorsByModel,
  ToolFailureCount,
  ToolUsageAnalytics,
  ToolUsageCount,
} from "../types/api";
import { formatMetricNumber, formatPercent } from "../utils/format";

interface FilterOptions {
  repositories: Repository[];
  packs: BenchmarkPack[];
  providers: string[];
}

export function ToolUsagePage() {
  const [filters, setFilters] = useState<AnalyticsFilterValue>(emptyAnalyticsFilters);
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [data, setData] = useState<ToolUsageAnalytics | null>(null);
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
    getToolUsage({
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

  if (error) return <ErrorState title="Tool analytics unavailable" message={error} />;
  if (!options || !data) return <LoadingState title="Loading tool analytics" />;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Analytics"
        title="Tool selection"
        description="Controlled-tool volume, failures, and invalid selections across agent runs."
      />
      <AnalyticsFilters
        value={filters}
        providers={options.providers}
        repositories={options.repositories}
        packs={options.packs}
        onChange={setFilters}
      />
      <section className="analytics-metric-grid" aria-label="Tool usage metrics">
        <ToolMetric label="Total calls" value={String(data.total_tool_calls)} />
        <ToolMetric label="Successful" value={String(data.successful_tool_calls)} tone="positive" />
        <ToolMetric label="Failed" value={String(data.failed_tool_calls)} tone="negative" />
        <ToolMetric label="Error rate" value={formatPercent(data.tool_error_rate)} />
        <ToolMetric label="Calls / run" value={formatMetricNumber(data.average_tool_calls_per_run, 2)} />
        <ToolMetric label="Runs with errors" value={String(data.runs_with_tool_errors)} />
        <ToolMetric label="Unknown tools" value={String(data.unknown_tool_calls)} tone="negative" />
        <ToolMetric label="Malformed calls" value={String(data.malformed_tool_calls)} tone="negative" />
      </section>

      {data.total_tool_calls === 0 ? (
        <EmptyState
          title="No tool calls"
          message="No controlled-tool events match the current filters."
        />
      ) : (
        <div className="tool-usage-panels">
          <UsageTable rows={data.most_used_tools} />
          <FailureTable rows={data.most_failed_tools} />
          <ErrorTypeTable counts={data.tool_error_counts_by_type} />
          <ModelErrorTable rows={data.tool_errors_by_model} />
        </div>
      )}
    </div>
  );
}

function ToolMetric({
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

function UsageTable({ rows }: { rows: ToolUsageCount[] }) {
  return (
    <ToolTable title="Most used tools" countLabel="Calls">
      {rows.map((row) => (
        <tr key={row.tool_name}>
          <td><code>{row.tool_name}</code></td>
          <td>{row.call_count}</td>
        </tr>
      ))}
    </ToolTable>
  );
}

function FailureTable({ rows }: { rows: ToolFailureCount[] }) {
  return (
    <ToolTable title="Most failed tools" countLabel="Failures">
      {rows.length ? rows.map((row) => (
        <tr key={row.tool_name}>
          <td><code>{row.tool_name}</code></td>
          <td>{row.failed_count}</td>
        </tr>
      )) : <EmptyTableRow message="No tool failures recorded." />}
    </ToolTable>
  );
}

function ErrorTypeTable({ counts }: { counts: Record<string, number> }) {
  const rows = Object.entries(counts).sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  return (
    <ToolTable title="Errors by type" countLabel="Errors">
      {rows.length ? rows.map(([errorType, count]) => (
        <tr key={errorType}>
          <td><code>{errorType}</code></td>
          <td>{count}</td>
        </tr>
      )) : <EmptyTableRow message="No error categories recorded." />}
    </ToolTable>
  );
}

function ModelErrorTable({ rows }: { rows: ToolErrorsByModel[] }) {
  return (
    <section className="panel table-panel tool-model-panel">
      <div className="panel-header">
        <h3>Tool errors by model</h3>
        <span>{rows.length} configurations</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>Provider / model</th>
            <th>Calls</th>
            <th>Failed</th>
            <th>Error rate</th>
            <th>Unknown</th>
            <th>Malformed</th>
            <th>Runs affected</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.model_provider}:${row.model_name}`}>
              <td><strong>{row.model_name}</strong><span>{row.model_provider}</span></td>
              <td>{row.total_tool_calls}</td>
              <td>{row.failed_tool_calls}</td>
              <td>{formatPercent(row.tool_error_rate)}</td>
              <td>{row.unknown_tool_calls}</td>
              <td>{row.malformed_tool_calls}</td>
              <td>{row.runs_with_tool_errors}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function ToolTable({
  title,
  countLabel,
  children,
}: {
  title: string;
  countLabel: string;
  children: ReactNode;
}) {
  return (
    <section className="panel table-panel">
      <div className="panel-header"><h3>{title}</h3></div>
      <table>
        <thead><tr><th>Tool</th><th>{countLabel}</th></tr></thead>
        <tbody>{children}</tbody>
      </table>
    </section>
  );
}

function EmptyTableRow({ message }: { message: string }) {
  return <tr><td className="muted" colSpan={2}>{message}</td></tr>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load tool analytics.";
}
