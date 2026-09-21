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

export function formatPercent(value: number | null, digits = 1): string {
  return value === null ? "N/A" : `${(value * 100).toFixed(digits)}%`;
}

export function formatMoney(value: number): string {
  return `$${value.toFixed(value < 0.01 ? 6 : 4)}`;
}

export function formatMetricNumber(value: number, digits = 2): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatSeconds(value: number): string {
  if (value < 60) return `${value.toFixed(1)}s`;
  const minutes = Math.floor(value / 60);
  return `${minutes}m ${(value % 60).toFixed(0)}s`;
}
