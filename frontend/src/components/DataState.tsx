import type { ReactNode } from "react";

interface DataStateProps {
  title: string;
  message?: string;
  children?: ReactNode;
}

export function LoadingState({ title = "Loading data" }: Partial<DataStateProps>) {
  return (
    <div className="state-panel">
      <div className="spinner" aria-hidden="true" />
      <h2>{title}</h2>
      <p>Fetching current backend records.</p>
    </div>
  );
}

export function EmptyState({ title, message, children }: DataStateProps) {
  return (
    <div className="state-panel">
      <h2>{title}</h2>
      <p>{message ?? "No records are available yet."}</p>
      {children}
    </div>
  );
}

export function ErrorState({ title, message, children }: DataStateProps) {
  return (
    <div className="state-panel state-error">
      <h2>{title}</h2>
      <p>{message ?? "The request failed."}</p>
      {children}
    </div>
  );
}
