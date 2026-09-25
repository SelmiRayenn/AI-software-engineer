import type { AgentPlan } from "../types/api";
import { StatusBadge } from "./StatusBadge";

export function AgentPlanSection({ plan }: { plan: AgentPlan | null }) {
  const content = plan?.plan;
  return (
    <section className="agent-plan-section" id="agent-plan" aria-labelledby="agent-plan-heading">
      <div className="panel-header">
        <h3 id="agent-plan-heading">Agent Plan</h3>
        {plan ? (
          <div className="plan-status">
            <span>Revision {plan.revision}</span>
            <StatusBadge value={plan.status} />
          </div>
        ) : <span className="muted">Not submitted</span>}
      </div>
      {plan?.reason ? <p className="plan-rejection" role="status">{plan.reason}</p> : null}
      {content ? (
        <dl className="agent-plan-grid">
          <div><dt>Issue summary</dt><dd>{content.issue_summary}</dd></div>
          <div><dt>Suspected root cause</dt><dd>{content.suspected_root_cause}</dd></div>
          <div>
            <dt>Files inspected</dt>
            <dd>{content.files_inspected.length ? (
              <ul>{content.files_inspected.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
            ) : "None"}</dd>
          </div>
          <div>
            <dt>Files likely to modify</dt>
            <dd>{content.files_likely_to_modify.length ? (
              <ul>{content.files_likely_to_modify.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
            ) : "No changes proposed"}</dd>
          </div>
          <div><dt>Test strategy</dt><dd>{content.test_strategy}</dd></div>
          <div><dt>Risk / rollback</dt><dd>{content.risk_rollback_notes}</dd></div>
        </dl>
      ) : <p className="muted">No validated plan content available.</p>}
    </section>
  );
}
