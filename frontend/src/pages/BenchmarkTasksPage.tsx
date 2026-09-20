import { useEffect, useState } from "react";
import { listBenchmarkTasks } from "../api";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import type { BenchmarkTask } from "../types/api";
import { shortSha } from "../utils/format";

export function BenchmarkTasksPage() {
  const [tasks, setTasks] = useState<BenchmarkTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listBenchmarkTasks()
      .then((records) => {
        if (active) {
          setTasks(records);
          setError(null);
        }
      })
      .catch((loadError) => {
        if (active) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load tasks.");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return <ErrorState title="Tasks unavailable" message={error} />;
  }
  if (!tasks) {
    return <LoadingState title="Loading benchmark tasks" />;
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Benchmark Tasks"
        title="Historical issue tasks"
        description="Agent-visible benchmark tasks with repository and base commit context."
      />
      {tasks.length === 0 ? (
        <EmptyState title="No tasks found" message="Load manual benchmarks or ingest GitHub tasks." />
      ) : (
        <section className="panel table-panel">
          <table>
            <thead>
              <tr>
                <th>Issue</th>
                <th>Repository</th>
                <th>Status</th>
                <th>Base Commit</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr key={task.id}>
                  <td>
                    <strong>{task.issue_number ? `#${task.issue_number}` : "No linked issue"}</strong>
                    <span>{task.issue_title}</span>
                  </td>
                  <td>
                    {task.repository.owner}/{task.repository.name}
                    <span>{task.repository.language ?? "Unknown"}</span>
                  </td>
                  <td>
                    <StatusBadge value={task.status} />
                  </td>
                  <td className="mono">{shortSha(task.base_commit)}</td>
                  <td>{new Date(task.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
