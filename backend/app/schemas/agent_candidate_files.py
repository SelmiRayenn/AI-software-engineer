from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

CandidatePath = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
CandidateReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]
CandidateConfidence = Literal["low", "medium", "high"]


class CandidateFileRank(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: CandidatePath
    reason: CandidateReason
    confidence: CandidateConfidence


class CandidateFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ranked_files: list[CandidateFileRank] = Field(min_length=1, max_length=50)


class CandidateFilesSubmission(CandidateFilesInput):
    model_config = ConfigDict(extra="ignore")

    created_at: datetime
