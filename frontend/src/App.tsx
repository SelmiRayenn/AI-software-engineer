import { useEffect, useMemo, useState } from "react";
import "./styles.css";

type HealthState = "checking" | "ok" | "offline";

const stages = [
  {
    label: "Issue Intake",
    value: "Queued",
    detail: "Historical GitHub issues become reproducible benchmark cases.",
  },
  {
    label: "Sandbox Runs",
    value: "Docker",
    detail: "Each run executes in an isolated repository and dependency context.",
  },
  {
    label: "Patch Review",
    value: "Human Gate",
    detail: "Candidate patches wait for approval before benchmark publication.",
  },
  {
    label: "Metrics",
    value: "Pending",
    detail: "Pass rate, latency, cost, retries, and regression signals land here.",
  },
];

function App() {
  const [health, setHealth] = useState<HealthState>("checking");
  const apiBaseUrl = useMemo(
    () => import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000",
    [],
  );

  useEffect(() => {
    const controller = new AbortController();

    fetch(`${apiBaseUrl}/health`, { signal: controller.signal })
      .then((response) => {
        setHealth(response.ok ? "ok" : "offline");
      })
      .catch(() => {
        setHealth("offline");
      });

    return () => controller.abort();
  }, [apiBaseUrl]);

  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="Primary">
        <div className="brand">
          <span className="brand-mark">AB</span>
          <div>
            <p className="eyebrow">Benchmark Platform</p>
            <h1>AgentBench</h1>
          </div>
        </div>
        <nav className="nav-links">
          <a aria-current="page" href="#overview">
            Overview
          </a>
          <a href="#runs">Runs</a>
          <a href="#reviews">Reviews</a>
          <a href="#metrics">Metrics</a>
        </nav>
      </aside>

      <section className="dashboard" id="overview">
        <header className="dashboard-header">
          <div>
            <p className="eyebrow">Flagship Evaluation System</p>
            <h2>AI software engineering agent benchmark</h2>
          </div>
          <div className={`status-pill status-${health}`}>
            <span aria-hidden="true" />
            Backend {health}
          </div>
        </header>

        <section className="summary-grid" aria-label="Benchmark summary">
          <article>
            <p>Benchmark Cases</p>
            <strong>0</strong>
            <span>Ready for ingestion</span>
          </article>
          <article>
            <p>Sandbox Jobs</p>
            <strong>0</strong>
            <span>Docker queue pending</span>
          </article>
          <article>
            <p>Patch Reviews</p>
            <strong>0</strong>
            <span>Human approval queue</span>
          </article>
          <article>
            <p>Published Runs</p>
            <strong>0</strong>
            <span>Metrics not recorded yet</span>
          </article>
        </section>

        <section className="pipeline" aria-label="Evaluation pipeline">
          <div className="section-heading">
            <h3>Evaluation Pipeline</h3>
            <span>Initial scaffold</span>
          </div>
          <div className="stage-grid">
            {stages.map((stage) => (
              <article className="stage-card" key={stage.label}>
                <div className="stage-topline">
                  <span>{stage.label}</span>
                  <strong>{stage.value}</strong>
                </div>
                <p>{stage.detail}</p>
              </article>
            ))}
          </div>
        </section>
      </section>
    </main>
  );
}

export default App;
