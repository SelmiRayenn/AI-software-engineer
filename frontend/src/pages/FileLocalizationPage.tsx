import { useEffect, useState } from "react";
import {
  getFileLocalization,
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
  BenchmarkPack,
  FileLocalizationAnalytics,
  LocalizationByModel,
  LocalizationByRepository,
  MissedGoldFile,
  Repository,
} from "../types/api";
import { formatMetricNumber, formatPercent } from "../utils/format";

interface FilterOptions {
  repositories: Repository[];
  packs: BenchmarkPack[];
  providers: string[];
}

export function FileLocalizationPage() {
  const [filters, setFilters] = useState<AnalyticsFilterValue>(emptyAnalyticsFilters);
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [data, setData] = useState<FileLocalizationAnalytics | null>(null);
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
    getFileLocalization({
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

  if (error) return <ErrorState title="Localization analytics unavailable" message={error} />;
  if (!options || !data) return <LoadingState title="Loading localization analytics" />;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Analytics"
        title="File localization"
        description="How reliably agents inspect and edit issue-relevant files."
      />
      <AnalyticsFilters
        value={filters}
        providers={options.providers}
        repositories={options.repositories}
        packs={options.packs}
        onChange={setFilters}
      />
      <section className="analytics-metric-grid" aria-label="File localization metrics">
        <Metric label="Runs with gold files" value={String(data.total_runs_with_gold_files)} />
        <Metric
          label="Average localization"
          value={formatPercent(data.average_file_localization_score)}
        />
        <Metric label="Top 1" value={formatPercent(data.top1_accuracy)} />
        <Metric label="Top 3" value={formatPercent(data.top3_accuracy)} />
        <Metric label="Top 5" value={formatPercent(data.top5_accuracy)} />
        <Metric label="Edit precision" value={formatPercent(data.edited_file_precision)} />
        <Metric label="Edit recall" value={formatPercent(data.edited_file_recall)} />
        <Metric
          label="Average files read"
          value={formatMetricNumber(data.average_files_read, 2)}
        />
        <Metric
          label="Average files edited"
          value={formatMetricNumber(data.average_files_edited, 2)}
        />
      </section>

      {data.total_runs_with_gold_files === 0 ? (
        <EmptyState
          title="No localization evidence"
          message="No matching runs are associated with a non-empty gold file list."
        />
      ) : (
        <div className="localization-panels">
          <ModelTable rows={data.localization_by_model} />
          <RepositoryTable rows={data.localization_by_repository} />
          <MissedFilesTable rows={data.most_common_missed_gold_files} />
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <article className="analytics-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function ModelTable({ rows }: { rows: LocalizationByModel[] }) {
  return (
    <section className="panel table-panel localization-table-panel">
      <div className="panel-header">
        <h3>Localization by model</h3>
        <span>{rows.length} configurations</span>
      </div>
      <table>
        <thead><ComparisonHeader first="Provider / model" /></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.model_provider}:${row.model_name}`}>
              <td><strong>{row.model_name}</strong><span>{row.model_provider}</span></td>
              <ComparisonCells row={row} />
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function RepositoryTable({ rows }: { rows: LocalizationByRepository[] }) {
  return (
    <section className="panel table-panel localization-table-panel">
      <div className="panel-header">
        <h3>Localization by repository</h3>
        <span>{rows.length} repositories</span>
      </div>
      <table>
        <thead><ComparisonHeader first="Repository" /></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.repository_id}>
              <td><strong>{row.repository_owner}/{row.repository_name}</strong></td>
              <ComparisonCells row={row} />
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

interface ComparisonRow {
  total_runs_with_gold_files: number;
  average_file_localization_score: number;
  top1_accuracy: number;
  top3_accuracy: number;
  top5_accuracy: number;
  edited_file_precision: number;
  edited_file_recall: number;
  average_files_read: number;
  average_files_edited: number;
}

function ComparisonHeader({ first }: { first: string }) {
  return (
    <tr>
      <th>{first}</th><th>Runs</th><th>Average</th><th>Top 1</th><th>Top 3</th>
      <th>Top 5</th><th>Edit precision</th><th>Edit recall</th><th>Read</th><th>Edited</th>
    </tr>
  );
}

function ComparisonCells({ row }: { row: ComparisonRow }) {
  return (
    <>
      <td>{row.total_runs_with_gold_files}</td>
      <td>{formatPercent(row.average_file_localization_score)}</td>
      <td>{formatPercent(row.top1_accuracy)}</td>
      <td>{formatPercent(row.top3_accuracy)}</td>
      <td>{formatPercent(row.top5_accuracy)}</td>
      <td>{formatPercent(row.edited_file_precision)}</td>
      <td>{formatPercent(row.edited_file_recall)}</td>
      <td>{formatMetricNumber(row.average_files_read, 2)}</td>
      <td>{formatMetricNumber(row.average_files_edited, 2)}</td>
    </>
  );
}

function MissedFilesTable({ rows }: { rows: MissedGoldFile[] }) {
  return (
    <section className="panel table-panel">
      <div className="panel-header">
        <h3>Most commonly missed gold files</h3>
        <span>{rows.length} missed paths</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted">Every gold file was inspected before editing.</p>
      ) : (
        <table>
          <thead><tr><th>Repository</th><th>Gold file</th><th>Missed runs</th><th>Gold runs</th><th>Miss rate</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.repository_owner}/${row.repository_name}:${row.file_path}`}>
                <td>{row.repository_owner}/{row.repository_name}</td>
                <td><code>{row.file_path}</code></td>
                <td>{row.missed_run_count}</td>
                <td>{row.gold_run_count}</td>
                <td>{formatPercent(row.miss_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load localization analytics.";
}
