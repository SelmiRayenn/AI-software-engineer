import type { CandidateFile } from "../types/api";

export function AgentCandidateFilesSection({ candidates }: { candidates: CandidateFile[] }) {
  return (
    <section
      className="agent-candidate-section"
      id="agent-candidate-files"
      aria-labelledby="agent-candidate-files-heading"
    >
      <div className="panel-header">
        <h3 id="agent-candidate-files-heading">Candidate Files</h3>
        <span>{candidates.length} ranked</span>
      </div>
      {candidates.length ? (
        <ol className="candidate-file-list">
          {candidates.map((candidate, index) => (
            <li key={candidate.path}>
              <span className="candidate-rank">{index + 1}</span>
              <div>
                <div className="candidate-file-heading">
                  <code>{candidate.path}</code>
                  <span className={`candidate-confidence confidence-${candidate.confidence}`}>
                    {candidate.confidence}
                  </span>
                </div>
                <p>{candidate.reason}</p>
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">No candidate file ranking was submitted before editing.</p>
      )}
    </section>
  );
}
