import { useEffect, useState } from "react";
import { listAgentRuns } from "../api";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import type { AgentRun } from "../types/api";
import { formatDuration, shortId } from "../utils/format";

interface AgentRunsPageProps {
  onNavigate: (path: string) => void;
}

export function AgentRunsPage({ onNavigate }: AgentRunsPageProps) {
  const [runs, setRuns] = useState<AgentRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listAgentRuns()
      .then((records) => {
        if (active) {
          setRuns(records);
          setError(null);
        }
      })
      .catch((loadError) => {
        if (active) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load runs.");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return <ErrorState title="Runs unavailable" message={error} />;
  }
  if (!runs) {
    return <LoadingState title="Loading agent runs" />;
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Agent Runs"
        title="Run history"
        description="Execution status, model provider, and human review state for generated patches."
      />
      {runs.length === 0 ? (
        <EmptyState title="No runs found" message="Start an agent run from a ready benchmark task." />
      ) : (
        <section className="panel table-panel">
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Model</th>
                <th>Status</th>
                <th>Patch Review</th>
                <th>Duration</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td className="mono">{shortId(run.id)}</td>
                  <td>
                    {run.model_provider}
                    <span>{run.model_name}</span>
                  </td>
                  <td>
                    <StatusBadge value={run.status} />
                  </td>
                  <td>
                    <StatusBadge value={run.patch_review_status ?? "none"} />
                  </td>
                  <td>{formatDuration(run.started_at, run.completed_at)}</td>
                  <td className="align-right">
                    <button
                      className="text-button"
                      onClick={() => onNavigate(`/runs/${run.id}`)}
                      type="button"
                    >
                      View
                    </button>
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
