from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Software Engineering Agent Benchmark Platform"
    app_version: str = "0.1.0"
    app_env: str = Field(default="development", alias="APP_ENV")
    database_url: str = Field(
        default="postgresql+psycopg://benchmark:benchmark@localhost:5432/agent_benchmark",
        alias="DATABASE_URL",
    )
    database_auto_create_tables: bool = Field(default=False, alias="DATABASE_AUTO_CREATE_TABLES")
    backend_cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="BACKEND_CORS_ORIGINS",
    )
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    sandbox_image: str = Field(default="python:3.12-slim", alias="SANDBOX_IMAGE")
    sandbox_workspace_root: str | None = Field(default=None, alias="SANDBOX_WORKSPACE_ROOT")
    sandbox_retain_workspaces: bool = Field(default=False, alias="SANDBOX_RETAIN_WORKSPACES")
    sandbox_memory_limit: str = Field(default="1g", alias="SANDBOX_MEMORY_LIMIT")
    sandbox_cpu_limit: float = Field(
        default=1.0,
        gt=0,
        validation_alias=AliasChoices("SANDBOX_CPU_LIMIT", "SANDBOX_CPUS"),
    )
    sandbox_pids_limit: int = Field(default=256, ge=16, alias="SANDBOX_PIDS_LIMIT")
    sandbox_command_timeout_seconds: int = Field(
        default=120,
        ge=1,
        alias="SANDBOX_COMMAND_TIMEOUT_SECONDS",
    )
    sandbox_clone_timeout_seconds: int = Field(
        default=120,
        ge=1,
        alias="SANDBOX_CLONE_TIMEOUT_SECONDS",
    )
    sandbox_max_command_timeout_seconds: int = Field(
        default=600,
        ge=1,
        alias="SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS",
    )
    sandbox_network_enabled: bool = Field(default=False, alias="SANDBOX_NETWORK_ENABLED")
    sandbox_pull_image: bool = Field(default=True, alias="SANDBOX_PULL_IMAGE")
    sandbox_max_output_bytes: int = Field(
        default=200_000,
        ge=1024,
        validation_alias=AliasChoices("SANDBOX_MAX_OUTPUT_BYTES", "SANDBOX_MAX_LOG_BYTES"),
    )
    patch_max_bytes: int = Field(default=1_000_000, ge=1024, alias="PATCH_MAX_BYTES")
    patch_max_changed_files: int = Field(default=100, ge=1, alias="PATCH_MAX_CHANGED_FILES")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_default_model: str = Field(default="gpt-4o-mini", alias="OPENAI_DEFAULT_MODEL")
    enable_real_model_calls: bool = Field(default=False, alias="ENABLE_REAL_MODEL_CALLS")
    enable_real_embeddings: bool = Field(default=False, alias="ENABLE_REAL_EMBEDDINGS")
    embeddings_provider: Literal["mock", "openai", "local"] = Field(
        default="mock", alias="EMBEDDINGS_PROVIDER"
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small", min_length=1, alias="OPENAI_EMBEDDING_MODEL"
    )
    embedding_batch_size: int = Field(default=32, ge=1, le=128, alias="EMBEDDING_BATCH_SIZE")
    embedding_vector_dimensions: int | None = Field(
        default=None, ge=1, le=4096, alias="EMBEDDING_VECTOR_DIMENSIONS"
    )
    embedding_chunk_size_chars: int = Field(
        default=1000, ge=128, le=1500, alias="EMBEDDING_CHUNK_SIZE_CHARS"
    )
    embedding_chunk_overlap_chars: int = Field(
        default=100, ge=0, le=500, alias="EMBEDDING_CHUNK_OVERLAP_CHARS"
    )
    embedding_auto_build: bool = Field(default=False, alias="EMBEDDING_AUTO_BUILD")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    local_model_endpoint: str | None = Field(default=None, alias="LOCAL_MODEL_ENDPOINT")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("embedding_vector_dimensions", mode="before")
    @classmethod
    def optional_embedding_dimensions(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_embedding_overlap(self):
        if self.embedding_chunk_overlap_chars >= self.embedding_chunk_size_chars:
            raise ValueError("Embedding chunk overlap must be smaller than chunk size.")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]

    @property
    def sandbox_cpus(self) -> float:
        return self.sandbox_cpu_limit

    @property
    def sandbox_max_log_bytes(self) -> int:
        return self.sandbox_max_output_bytes


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
