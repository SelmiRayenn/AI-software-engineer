from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

HypothesisText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
EvidenceText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]
HypothesisPath = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
HypothesisStatus = Literal["active", "revised", "rejected", "confirmed"]
HypothesisConfidence = Literal["low", "medium", "high"]


class AgentHypothesisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: HypothesisText
    suspected_files: list[HypothesisPath] = Field(default_factory=list, max_length=50)
    supporting_evidence: list[EvidenceText] = Field(min_length=1, max_length=50)
    confidence: HypothesisConfidence
    status: HypothesisStatus = "active"


class AgentHypothesisRead(AgentHypothesisInput):
    model_config = ConfigDict(extra="ignore")

    revision: int = Field(ge=1)
    superseded_by_revision: int | None = Field(default=None, ge=1)
    created_at: datetime
