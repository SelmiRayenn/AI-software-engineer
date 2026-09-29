from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.redaction import redact_and_truncate, redact_common_secrets

AuditActorType = Literal["system", "agent", "human", "operator"]
AuditScopeType = Literal["agent_run", "generated_patch"]


class AuditLogEvent(BaseModel):
    sequence_number: int = Field(ge=1)
    timestamp: str
    event_type: str
    actor_type: AuditActorType
    sanitized_summary: str
    related_entity_id: UUID
    previous_event_hash: str | None = Field(default=None, min_length=64, max_length=64)
    event_hash: str = Field(min_length=64, max_length=64)

    @field_validator("timestamp", "event_type", mode="before")
    @classmethod
    def redact_identity_text(cls, value: str) -> str:
        return redact_common_secrets(value)

    @field_validator("sanitized_summary", mode="before")
    @classmethod
    def redact_summary(cls, value: str) -> str:
        return redact_and_truncate(value, max_chars=2_000)


class AuditLogExport(BaseModel):
    schema_version: str = "1.0"
    scope_type: AuditScopeType
    scope_id: UUID
    agent_run_id: UUID
    benchmark_task_id: UUID
    model_provider: str
    model_name: str
    run_configuration_hash: str = Field(min_length=64, max_length=64)
    exported_at: str
    event_count: int = Field(ge=0)
    final_audit_hash: str | None = Field(default=None, min_length=64, max_length=64)
    hash_algorithm: Literal["sha256"] = "sha256"
    integrity_notice: str
    events: list[AuditLogEvent] = Field(default_factory=list)

    @field_validator("model_provider", "model_name", "integrity_notice", mode="before")
    @classmethod
    def redact_export_text(cls, value: str) -> str:
        return redact_common_secrets(value)
