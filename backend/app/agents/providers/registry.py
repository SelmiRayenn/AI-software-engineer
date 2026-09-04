from app.model_providers import ModelProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        self._providers[provider.provider_name] = provider

    def get(self, name: str) -> ModelProvider:
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)
