import type { AgentHypothesis } from "../types/api";
import { StatusBadge } from "./StatusBadge";

interface AgentHypothesisSectionProps {
  hypotheses: AgentHypothesis[];
  activeHypothesis: AgentHypothesis | null;
}

export function AgentHypothesisSection({
  hypotheses,
  activeHypothesis,
}: AgentHypothesisSectionProps) {
  return (
    <section
      className="agent-hypothesis-section"
      id="agent-hypotheses"
      aria-labelledby="agent-hypotheses-heading"
    >
      <div className="panel-header">
        <h3 id="agent-hypotheses-heading">Root-Cause Hypotheses</h3>
        <span>{hypotheses.length} revision{hypotheses.length === 1 ? "" : "s"}</span>
      </div>
      {activeHypothesis ? (
        <p className="hypothesis-current">
          Current: revision {activeHypothesis.revision}, {activeHypothesis.confidence} confidence
        </p>
      ) : null}
      {hypotheses.length ? (
        <ol className="hypothesis-timeline">
          {hypotheses.map((hypothesis) => (
            <li key={hypothesis.revision} className="hypothesis-entry">
              <div className="hypothesis-heading">
                <strong>Revision {hypothesis.revision}</strong>
                <StatusBadge value={hypothesis.status} />
                <span className="muted">{hypothesis.confidence} confidence</span>
              </div>
              <p>{hypothesis.summary}</p>
              <dl className="hypothesis-details">
                <div>
                  <dt>Suspected files</dt>
                  <dd>{hypothesis.suspected_files.length ? (
                    <ul>{hypothesis.suspected_files.map((path) => (
                      <li key={path}><code>{path}</code></li>
                    ))}</ul>
                  ) : "None identified"}</dd>
                </div>
                <div>
                  <dt>Supporting evidence</dt>
                  <dd><ul>{hypothesis.supporting_evidence.map((evidence, index) => (
                    <li key={`${hypothesis.revision}-${index}`}>{evidence}</li>
                  ))}</ul></dd>
                </div>
              </dl>
              {hypothesis.superseded_by_revision ? (
                <span className="muted">Superseded by revision {hypothesis.superseded_by_revision}</span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : <p className="muted">No root-cause hypothesis has been submitted.</p>}
    </section>
  );
}
