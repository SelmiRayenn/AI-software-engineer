from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentRequest:
    repository_url: str
    issue_url: str
    base_commit: str
    prompt: str


@dataclass(frozen=True)
class AgentPatch:
    diff: str
    summary: str


class ModelProvider(Protocol):
    name: str

    async def generate_patch(self, request: AgentRequest) -> AgentPatch:
        """Generate a candidate patch for a benchmark issue."""
