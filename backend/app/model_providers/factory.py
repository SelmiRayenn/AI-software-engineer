from __future__ import annotations

from app.core.config import Settings, settings
from app.model_providers.base import ModelProvider, ModelProviderConfigError
from app.model_providers.providers import (
    AnthropicProvider,
    LocalModelProvider,
    MockModelProvider,
    OpenAIProvider,
)


class ModelProviderFactory:
    def __init__(self, app_settings: Settings = settings) -> None:
        self._settings = app_settings

    def create(
        self,
        provider_name: str,
        *,
        model_name: str | None = None,
    ) -> ModelProvider:
        normalized_name = provider_name.strip().lower()
        if normalized_name == "mock":
            return MockModelProvider(model_name=model_name or "mock-model")
        if normalized_name == "openai":
            return OpenAIProvider(
                model_name=model_name or self._settings.openai_default_model,
                app_settings=self._settings,
            )
        if normalized_name == "anthropic":
            return AnthropicProvider(
                model_name=model_name or "claude-3-5-haiku-latest",
                app_settings=self._settings,
            )
        if normalized_name == "local":
            return LocalModelProvider(
                model_name=model_name or "local-model",
                app_settings=self._settings,
            )
        raise ModelProviderConfigError(f"Unknown model provider: {provider_name}")


def create_model_provider(
    provider_name: str,
    *,
    model_name: str | None = None,
    app_settings: Settings = settings,
) -> ModelProvider:
    return ModelProviderFactory(app_settings=app_settings).create(
        provider_name,
        model_name=model_name,
    )
