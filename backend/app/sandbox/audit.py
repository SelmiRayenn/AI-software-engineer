from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AgentEvent
from app.schemas.sandbox import SandboxCommandResult


def record_network_decision(db: Session, run_id: UUID, result: SandboxCommandResult) -> None:
    policy = result.network_policy
    db.add(
        AgentEvent(
            agent_run_id=run_id,
            event_type="sandbox_network_policy",
            payload_json={
                **(
                    policy.model_dump()
                    if policy
                    else {
                        "phase": result.phase,
                        "effective_network_mode": "none",
                        "network_exception": False,
                        "reason": "execution_blocked",
                    }
                ),
                "error_code": result.error_code,
            },
        )
    )
    db.commit()
