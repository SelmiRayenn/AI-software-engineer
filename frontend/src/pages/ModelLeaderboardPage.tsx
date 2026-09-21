import { useEffect, useMemo, useState } from "react";
import { getModelLeaderboard, listBenchmarkPacks, listRepositories } from "../api";
import {
  AnalyticsFilters,
  emptyAnalyticsFilters,
  type AnalyticsFilterValue,
} from "../components/AnalyticsFilters";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import type { BenchmarkPack, ModelLeaderboardRow, Repository } from "../types/api";
import {
  formatMetricNumber,
  formatMoney,
  formatPercent,
  formatSeconds,
} from "../utils/format";

interface FilterOptions {
  repositories: Repository[];
  packs: BenchmarkPack[];
  providers: string[];
}

export function ModelLeaderboardPage() {
  const [filters, setFilters] = useState<AnalyticsFilterValue>(emptyAnalyticsFilters);
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [rows, setRows] = useState<ModelLeaderboardRow[] | null>(null);
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
    setRows(null);
    setError(null);
    getModelLeaderboard({
      repositoryId: filters.repositoryId || undefined,
      benchmarkPackId: filters.benchmarkPackId || undefined,
    })
      .then((leaderboard) => {
        if (active) setRows(leaderboard);
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError));
      });
    return () => {
      active = false;
    };
  }, [filters.repositoryId, filters.benchmarkPackId]);

  const visibleRows = useMemo(() => {
    if (!rows) return null;
    return filters.modelProvider
      ? rows.filter((row) => row.model_provider === filters.modelProvider)
      : rows;
  }, [filters.modelProvider, rows]);

  if (error) return <ErrorState title="Leaderboard unavailable" message={error} />;
  if (!options || !visibleRows) return <LoadingState title="Loading model leaderboard" />;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Analytics"
        title="Model leaderboard"
        description="Model configurations ranked by issue resolution, tests, localization, cost, and execution quality."
      />
      <AnalyticsFilters
        value={filters}
        providers={options.providers}
        repositories={options.repositories}
        packs={options.packs}
        onChange={setFilters}
      />
      {visibleRows.length === 0 ? (
        <EmptyState
          title="No ranked models"
          message="No model has enough matching runs for the current filters."
        />
      ) : (
        <section className="panel table-panel leaderboard-panel">
          <div className="panel-header">
            <h3>Ranked configurations</h3>
            <span>{visibleRows.length} models</span>
          </div>
          <table className="leaderboard-table">
            <thead>
              <tr>
                <th>Provider / model</th>
                <th>Runs</th>
                <th>Resolved</th>
                <th>Visible</th>
                <th>Hidden</th>
                <th>Localization</th>
                <th>Cost / run</th>
                <th>Tokens / run</th>
                <th>Speed</th>
                <th>Unrelated</th>
                <th>Composite</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((row) => (
                <tr key={`${row.model_provider}:${row.model_name}`}>
                  <td className="leaderboard-model">
                    <strong>{row.model_name}</strong>
                    <span>{row.model_provider}</span>
                    <div className="rank-list" aria-label="Metric ranks">
                      <RankBadge label="Resolved" rank={row.rank_by_issue_resolved} />
                      <RankBadge label="Cost" rank={row.rank_by_cost} />
                      <RankBadge label="Speed" rank={row.rank_by_speed} />
                      <RankBadge label="Localize" rank={row.rank_by_localization} />
                    </div>
                  </td>
                  <td>
                    <strong>{row.total_runs}</strong>
                    <span>{row.completed_runs} done / {row.failed_runs} failed</span>
                  </td>
                  <td>{formatPercent(row.issue_resolved_rate)}</td>
                  <td>{formatPercent(row.visible_test_pass_rate)}</td>
                  <td>{formatPercent(row.hidden_test_pass_rate)}</td>
                  <td>{formatMetricNumber(row.average_file_localization_score, 3)}</td>
                  <td>{formatMoney(row.average_cost_per_run)}</td>
                  <td>{formatMetricNumber(row.average_tokens_per_run, 0)}</td>
                  <td>{formatSeconds(row.average_execution_time_seconds)}</td>
                  <td>{formatMetricNumber(row.average_unrelated_files_count, 2)}</td>
                  <td>
                    <strong className="composite-score">
                      {formatMetricNumber(row.composite_score, 4)}
                    </strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

function RankBadge({ label, rank }: { label: string; rank: number }) {
  return (
    <span className="rank-badge" title={`${label} rank ${rank}`}>
      {label} #{rank}
    </span>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Failed to load model leaderboard.";
}
