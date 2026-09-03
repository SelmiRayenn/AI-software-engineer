from functools import lru_cache

from pydantic import Field
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
    sandbox_memory_limit: str = Field(default="1g", alias="SANDBOX_MEMORY_LIMIT")
    sandbox_cpus: float = Field(default=1.0, gt=0, alias="SANDBOX_CPUS")
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
    sandbox_max_log_bytes: int = Field(default=200_000, ge=1024, alias="SANDBOX_MAX_LOG_BYTES")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
