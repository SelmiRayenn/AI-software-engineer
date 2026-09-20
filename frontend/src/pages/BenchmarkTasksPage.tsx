import { useEffect, useState } from "react";
import { listBenchmarkTasks, listBenchmarkTaskTags } from "../api";
import { EmptyState, ErrorState, LoadingState } from "../components/DataState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import type { BenchmarkTask } from "../types/api";
import { shortSha } from "../utils/format";

export function BenchmarkTasksPage() {
  const [tasks, setTasks] = useState<BenchmarkTask[] | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [difficulty, setDifficulty] = useState("");
  const [tag, setTag] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([listBenchmarkTasks({ difficulty: difficulty || undefined, tag: tag || undefined }), listBenchmarkTaskTags()])
      .then(([records, availableTags]) => {
        if (active) {
          setTasks(records);
          setTags(availableTags);
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
  }, [difficulty, tag]);

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
      <section className="task-filters" aria-label="Task filters">
        <label>
          <span>Difficulty</span>
          <select value={difficulty} onChange={(event) => setDifficulty(event.target.value)}>
            <option value="">All difficulties</option>
            <option value="easy">Easy</option>
            <option value="medium">Medium</option>
            <option value="hard">Hard</option>
            <option value="expert">Expert</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
        <label>
          <span>Tag</span>
          <select value={tag} onChange={(event) => setTag(event.target.value)}>
            <option value="">All tags</option>
            {tags.map((availableTag) => <option key={availableTag} value={availableTag}>{availableTag}</option>)}
          </select>
        </label>
      </section>
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
                <th>Difficulty</th>
                <th>Tags</th>
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
                  <td><span className={`difficulty-label difficulty-${task.difficulty}`}>{task.difficulty}</span></td>
                  <td>
                    {task.tags.length ? (
                      <div className="tag-list">{task.tags.map((taskTag) => <span className="tag-label" key={taskTag}>{taskTag}</span>)}</div>
                    ) : <span className="muted">No tags</span>}
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
