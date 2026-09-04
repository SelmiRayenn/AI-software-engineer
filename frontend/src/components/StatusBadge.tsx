interface StatusBadgeProps {
  value: string | null | undefined;
}

export function StatusBadge({ value }: StatusBadgeProps) {
  const normalized = (value ?? "none").toLowerCase();
  return <span className={`status-badge status-${normalized}`}>{value ?? "none"}</span>;
}
