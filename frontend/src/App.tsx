import { useEffect, useState } from "react";
import { AppLayout } from "./components/AppLayout";
import { AgentRunDetailPage } from "./pages/AgentRunDetailPage";
import { AgentRunsPage } from "./pages/AgentRunsPage";
import { BenchmarkTasksPage } from "./pages/BenchmarkTasksPage";
import { DashboardOverview } from "./pages/DashboardOverview";
import { parseRoute, type RouteState } from "./router";
import "./styles/main.css";

function App() {
  const [route, setRoute] = useState<RouteState>(() => parseRoute(window.location.pathname));

  useEffect(() => {
    const onPopState = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function navigate(path: string) {
    window.history.pushState({}, "", path);
    setRoute(parseRoute(path));
  }

  return (
    <AppLayout onNavigate={navigate} route={route}>
      {route.name === "overview" ? <DashboardOverview /> : null}
      {route.name === "tasks" ? <BenchmarkTasksPage /> : null}
      {route.name === "runs" ? <AgentRunsPage onNavigate={navigate} /> : null}
      {route.name === "run-detail" ? (
        <AgentRunDetailPage onNavigate={navigate} runId={route.runId} />
      ) : null}
    </AppLayout>
  );
}

export default App;
