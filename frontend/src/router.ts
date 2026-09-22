export type RouteState =
  | { name: "overview"; path: string }
  | { name: "analytics"; path: string }
  | { name: "leaderboard"; path: string }
  | { name: "tool-usage"; path: string }
  | { name: "file-localization"; path: string }
  | { name: "tasks"; path: string }
  | { name: "runs"; path: string }
  | { name: "run-detail"; path: string; runId: string };

export function parseRoute(pathname: string): RouteState {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length === 0) {
    return { name: "overview", path: "/" };
  }
  if (parts[0] === "analytics") {
    return { name: "analytics", path: "/analytics" };
  }
  if (parts[0] === "leaderboard") {
    return { name: "leaderboard", path: "/leaderboard" };
  }
  if (parts[0] === "tool-usage") {
    return { name: "tool-usage", path: "/tool-usage" };
  }
  if (parts[0] === "file-localization") {
    return { name: "file-localization", path: "/file-localization" };
  }
  if (parts[0] === "tasks") {
    return { name: "tasks", path: "/tasks" };
  }
  if (parts[0] === "runs" && parts[1]) {
    return { name: "run-detail", path: `/runs/${parts[1]}`, runId: parts[1] };
  }
  if (parts[0] === "runs") {
    return { name: "runs", path: "/runs" };
  }
  return { name: "overview", path: "/" };
}
