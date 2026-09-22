import type { ReactNode } from "react";
import type { RouteState } from "../router";

interface AppLayoutProps {
  route: RouteState;
  children: ReactNode;
  onNavigate: (path: string) => void;
}

const navItems = [
  { label: "Overview", path: "/" },
  { label: "Analytics", path: "/analytics" },
  { label: "Leaderboard", path: "/leaderboard" },
  { label: "Tool Usage", path: "/tool-usage" },
  { label: "Localization", path: "/file-localization" },
  { label: "Benchmark Tasks", path: "/tasks" },
  { label: "Agent Runs", path: "/runs" },
];

export function AppLayout({ route, children, onNavigate }: AppLayoutProps) {
  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand">
          <span className="brand-mark">AB</span>
          <div>
            <p className="eyebrow">Benchmark Platform</p>
            <h1>AgentBench</h1>
          </div>
        </div>
        <nav className="nav-links">
          {navItems.map((item) => (
            <a
              aria-current={isActive(route, item.path) ? "page" : undefined}
              href={item.path}
              key={item.path}
              onClick={(event) => {
                event.preventDefault();
                onNavigate(item.path);
              }}
            >
              {item.label}
            </a>
          ))}
        </nav>
      </aside>
      <section className="workspace">{children}</section>
    </main>
  );
}

function isActive(route: RouteState, path: string): boolean {
  if (path === "/") {
    return route.name === "overview";
  }
  if (path === "/tasks") {
    return route.name === "tasks";
  }
  if (path === "/analytics") {
    return route.name === "analytics";
  }
  if (path === "/leaderboard") {
    return route.name === "leaderboard";
  }
  if (path === "/tool-usage") {
    return route.name === "tool-usage";
  }
  if (path === "/file-localization") {
    return route.name === "file-localization";
  }
  if (path === "/runs") {
    return route.name === "runs" || route.name === "run-detail";
  }
  return false;
}
