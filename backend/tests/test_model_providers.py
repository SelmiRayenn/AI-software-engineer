import pytest

from app.core.config import Settings
from app.model_providers import (
    AnthropicProvider,
    LocalModelProvider,
    MockModelProvider,
    ModelMessage,
    ModelProviderConfigError,
    ModelProviderFactory,
    ModelProviderResponse,
    ModelToolCall,
    OpenAIProvider,
    ToolDefinition,
    create_model_provider,
)


def test_provider_interface_consistency() -> None:
    providers = [
        MockModelProvider(),
        OpenAIProvider(api_key="openai-key"),
        AnthropicProvider(api_key="anthropic-key"),
        LocalModelProvider(endpoint="http://localhost:11434"),
    ]

    for provider in providers:
        assert isinstance(provider.provider_name, str)
        assert isinstance(provider.model_name, str)
        assert callable(provider.generate_response)
        assert callable(provider.estimate_cost)


def test_mock_provider_response_captures_usage_and_tool_calls() -> None:
    tool_call = ModelToolCall(
        id="call-1",
        name="read_file",
        arguments={"file_path": "src/app.py"},
    )
    provider = MockModelProvider(content="Use read_file.", tool_calls=[tool_call])

    response = provider.generate_response(
        messages=[
            ModelMessage(role="system", content="You are a coding agent."),
            ModelMessage(role="user", content="Inspect the app."),
        ],
        tools=[
            ToolDefinition(
                name="read_file",
                description="Read a workspace file.",
                input_schema={"type": "object"},
            )
        ],
    )

    assert isinstance(response, ModelProviderResponse)
    assert response.content == "Use read_file."
    assert response.tool_calls == [tool_call]
    assert response.input_tokens is not None
    assert response.input_tokens > 0
    assert response.output_tokens is not None
    assert response.output_tokens > 0
    assert response.estimated_cost == 0.0
    assert response.latency_seconds >= 0
    assert response.raw_response["tools_available"] == ["read_file"]


def test_missing_api_key_produces_clear_configuration_error() -> None:
    with pytest.raises(ModelProviderConfigError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key="")

    with pytest.raises(ModelProviderConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key="")

    with pytest.raises(ModelProviderConfigError, match="LOCAL_MODEL_ENDPOINT"):
        LocalModelProvider(endpoint="")


def test_usage_cost_fields_are_standardized() -> None:
    provider = OpenAIProvider(model_name="gpt-4o-mini", api_key="openai-key")

    cost = provider.estimate_cost(input_tokens=1000, output_tokens=2000)

    assert cost == 0.00135


def test_provider_factory_selects_correct_provider() -> None:
    app_settings = Settings(
        _env_file=None,
        OPENAI_API_KEY="openai-key",
        ANTHROPIC_API_KEY="anthropic-key",
        LOCAL_MODEL_ENDPOINT="http://localhost:11434",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    assert isinstance(factory.create("openai"), OpenAIProvider)
    assert isinstance(factory.create("anthropic"), AnthropicProvider)
    assert isinstance(factory.create("local"), LocalModelProvider)
    assert isinstance(
        create_model_provider(
            "mock",
            model_name="mock-dev",
            app_settings=app_settings,
        ),
        MockModelProvider,
    )


def test_provider_factory_rejects_unknown_provider() -> None:
    factory = ModelProviderFactory(app_settings=Settings(_env_file=None))

    with pytest.raises(ModelProviderConfigError, match="Unknown model provider"):
        factory.create("missing-provider")
