from dataclasses import dataclass

from app.model_providers import ModelProvider as BaseModelProvider

type ModelProvider = BaseModelProvider


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
