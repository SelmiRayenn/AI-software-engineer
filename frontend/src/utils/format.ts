export function shortId(value: string): string {
  return value.slice(0, 8);
}

export function shortSha(value: string): string {
  return value.slice(0, 12);
}

export function formatDuration(startedAt: string, completedAt: string | null): string {
  if (!completedAt) {
    return "Running";
  }
  const started = new Date(startedAt).getTime();
  const completed = new Date(completedAt).getTime();
  if (Number.isNaN(started) || Number.isNaN(completed)) {
    return "N/A";
  }
  const seconds = Math.max(0, Math.round((completed - started) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}m ${remainder}s`;
}
