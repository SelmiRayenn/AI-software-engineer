from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
PlanPath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class AgentPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_summary: PlanText
    suspected_root_cause: PlanText
    files_inspected: list[PlanPath] = Field(max_length=50)
    files_likely_to_modify: list[PlanPath] = Field(max_length=50)
    test_strategy: PlanText
    risk_rollback_notes: PlanText


class AgentPlanRead(BaseModel):
    revision: int = Field(ge=1)
    status: Literal["accepted", "rejected"]
    accepted: bool
    reason: str | None = None
    plan: AgentPlanInput | None = None
    created_at: datetime
