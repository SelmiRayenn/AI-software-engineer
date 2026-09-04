import json
from types import SimpleNamespace

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


class FakeOpenAIResponses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.requests.append(request)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: object) -> None:
        self.responses = FakeOpenAIResponses(response)


def openai_settings(**overrides: object) -> Settings:
    values = {
        "ENABLE_REAL_MODEL_CALLS": True,
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_DEFAULT_MODEL": "gpt-4o-mini",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_provider_interface_consistency() -> None:
    providers = [
        MockModelProvider(),
        OpenAIProvider(app_settings=openai_settings()),
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


def test_real_openai_calls_disabled_produces_clear_configuration_error() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_REAL_MODEL_CALLS=False,
        OPENAI_API_KEY="openai-key",
    )

    with pytest.raises(ModelProviderConfigError, match="ENABLE_REAL_MODEL_CALLS=true"):
        OpenAIProvider(app_settings=app_settings)


def test_missing_api_key_produces_clear_configuration_error() -> None:
    with pytest.raises(ModelProviderConfigError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key="", app_settings=openai_settings(OPENAI_API_KEY=""))

    with pytest.raises(ModelProviderConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key="")

    with pytest.raises(ModelProviderConfigError, match="LOCAL_MODEL_ENDPOINT"):
        LocalModelProvider(endpoint="")


def test_usage_cost_fields_are_standardized() -> None:
    provider = OpenAIProvider(
        model_name="gpt-4o-mini",
        app_settings=openai_settings(),
    )

    cost = provider.estimate_cost(input_tokens=1000, output_tokens=2000)

    assert cost == 0.00135


def test_mocked_openai_response_normalizes_content_tools_and_usage() -> None:
    sdk_response = SimpleNamespace(
        id="resp-safe-id",
        model="gpt-4o-mini",
        status="completed",
        output_text="I will inspect the file.",
        output=[
            SimpleNamespace(
                type="function_call",
                id="fc_123",
                call_id="call_123",
                name="read_file",
                arguments='{"file_path":"src/app.py"}',
            )
        ],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )
    client = FakeOpenAIClient(sdk_response)
    provider = OpenAIProvider(
        app_settings=openai_settings(),
        client=client,
    )
    messages = [
        ModelMessage(role="system", content="You are a coding agent."),
        ModelMessage(role="user", content="Inspect the app."),
        ModelMessage(
            role="assistant",
            content=json.dumps(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_previous",
                            "name": "list_files",
                            "arguments": {},
                        }
                    ],
                }
            ),
        ),
        ModelMessage(
            role="tool",
            content=json.dumps(
                {
                    "tool_call_id": "call_previous",
                    "tool_name": "list_files",
                    "success": True,
                    "result": {"files": ["src/app.py"]},
                }
            ),
        ),
    ]
    tools = [
        ToolDefinition(
            name="read_file",
            description="Read a workspace file.",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
                "additionalProperties": False,
            },
        )
    ]

    response = provider.generate_response(messages, tools=tools)

    assert response.content == "I will inspect the file."
    assert response.tool_calls == [
        ModelToolCall(
            id="call_123",
            name="read_file",
            arguments={"file_path": "src/app.py"},
        )
    ]
    assert response.input_tokens == 120
    assert response.output_tokens == 30
    assert response.estimated_cost == 0.000036
    assert response.latency_seconds >= 0
    assert response.raw_response == {
        "id": "resp-safe-id",
        "model": "gpt-4o-mini",
        "status": "completed",
        "output_types": ["function_call"],
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }

    request = client.responses.requests[0]
    assert request["model"] == "gpt-4o-mini"
    assert request["store"] is False
    assert request["tool_choice"] == "auto"
    assert request["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a workspace file.",
            "parameters": tools[0].input_schema,
            "strict": False,
        }
    ]
    assert request["input"][2] == {
        "type": "function_call",
        "call_id": "call_previous",
        "name": "list_files",
        "arguments": "{}",
    }
    assert request["input"][3]["type"] == "function_call_output"
    assert request["input"][3]["call_id"] == "call_previous"


def test_provider_factory_selects_correct_provider() -> None:
    app_settings = openai_settings(
        OPENAI_DEFAULT_MODEL="configured-openai-model",
        ANTHROPIC_API_KEY="anthropic-key",
        LOCAL_MODEL_ENDPOINT="http://localhost:11434",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    openai_provider = factory.create("openai")
    assert isinstance(openai_provider, OpenAIProvider)
    assert openai_provider.model_name == "configured-openai-model"
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


def test_provider_factory_rejects_openai_when_real_calls_are_disabled() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_REAL_MODEL_CALLS=False,
        OPENAI_API_KEY="openai-key",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    with pytest.raises(ModelProviderConfigError, match="disabled"):
        factory.create("openai")


def test_provider_factory_rejects_unknown_provider() -> None:
    factory = ModelProviderFactory(app_settings=Settings(_env_file=None))

    with pytest.raises(ModelProviderConfigError, match="Unknown model provider"):
        factory.create("missing-provider")
