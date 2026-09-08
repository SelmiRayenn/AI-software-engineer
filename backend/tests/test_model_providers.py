import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.model_providers import (
    AnthropicProvider,
    LocalModelProvider,
    MockModelProvider,
    ModelMessage,
    ModelProviderConfigError,
    ModelProviderFactory,
    ModelProviderRequestError,
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


class FakeAnthropicMessages:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.requests.append(request)
        return self.response


class FakeAnthropicClient:
    def __init__(self, response: object) -> None:
        self.messages = FakeAnthropicMessages(response)


def openai_settings(**overrides: object) -> Settings:
    values = {
        "ENABLE_REAL_MODEL_CALLS": True,
        "OPENAI_API_KEY": "openai-key",
        "OPENAI_DEFAULT_MODEL": "gpt-4o-mini",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def anthropic_settings(**overrides: object) -> Settings:
    values = {
        "ENABLE_REAL_MODEL_CALLS": True,
        "ANTHROPIC_API_KEY": "anthropic-key",
        "ANTHROPIC_DEFAULT_MODEL": "claude-sonnet-5",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def local_settings(**overrides: object) -> Settings:
    values = {
        "ENABLE_LOCAL_MODEL_CALLS": True,
        "LOCAL_MODEL_ENDPOINT": "http://localhost:11434/v1",
        "LOCAL_MODEL_DEFAULT_MODEL": "local-test-model",
        "LOCAL_MODEL_TIMEOUT_SECONDS": 15,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_provider_interface_consistency() -> None:
    providers = [
        MockModelProvider(),
        OpenAIProvider(app_settings=openai_settings()),
        AnthropicProvider(app_settings=anthropic_settings()),
        LocalModelProvider(app_settings=local_settings()),
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


def test_real_anthropic_calls_disabled_produces_clear_configuration_error() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_REAL_MODEL_CALLS=False,
        ANTHROPIC_API_KEY="anthropic-key",
    )
    client = FakeAnthropicClient(response=object())

    with pytest.raises(ModelProviderConfigError, match="ENABLE_REAL_MODEL_CALLS=true"):
        AnthropicProvider(app_settings=app_settings, client=client)
    assert client.messages.requests == []


def test_missing_api_key_produces_clear_configuration_error() -> None:
    with pytest.raises(ModelProviderConfigError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key="", app_settings=openai_settings(OPENAI_API_KEY=""))

    with pytest.raises(ModelProviderConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(
            api_key="",
            app_settings=anthropic_settings(ANTHROPIC_API_KEY=""),
        )


def test_missing_local_model_endpoint_produces_clear_configuration_error() -> None:
    with pytest.raises(ModelProviderConfigError, match="LOCAL_MODEL_ENDPOINT"):
        LocalModelProvider(
            endpoint="",
            app_settings=local_settings(LOCAL_MODEL_ENDPOINT=""),
        )


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


def test_mocked_anthropic_response_normalizes_content_tools_and_usage() -> None:
    sdk_response = SimpleNamespace(
        id="msg_safe_id",
        model="claude-sonnet-5",
        stop_reason="tool_use",
        stop_sequence=None,
        content=[
            SimpleNamespace(type="text", text="I will inspect the file."),
            SimpleNamespace(
                type="tool_use",
                id="toolu_123",
                name="read_file",
                input={"file_path": "src/app.py"},
            ),
        ],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )
    client = FakeAnthropicClient(sdk_response)
    provider = AnthropicProvider(
        app_settings=anthropic_settings(),
        client=client,
    )
    messages = [
        ModelMessage(role="system", content="You are a coding agent."),
        ModelMessage(role="developer", content="Use controlled tools only."),
        ModelMessage(role="user", content="Inspect the app."),
        ModelMessage(
            role="assistant",
            content=json.dumps(
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_previous",
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
                    "tool_call_id": "toolu_previous",
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
            id="toolu_123",
            name="read_file",
            arguments={"file_path": "src/app.py"},
        )
    ]
    assert response.input_tokens == 120
    assert response.output_tokens == 30
    assert response.estimated_cost == 0.00054
    assert response.latency_seconds >= 0
    assert response.raw_response == {
        "id": "msg_safe_id",
        "model": "claude-sonnet-5",
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "content_types": ["text", "tool_use"],
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }

    request = client.messages.requests[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["max_tokens"] == 4096
    assert request["thinking"] == {"type": "disabled"}
    assert request["system"] == "You are a coding agent.\n\nUse controlled tools only."
    assert request["tool_choice"] == {"type": "auto"}
    assert request["tools"] == [
        {
            "name": "read_file",
            "description": "Read a workspace file.",
            "input_schema": tools[0].input_schema,
        }
    ]
    assert request["messages"][1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_previous",
                "name": "list_files",
                "input": {},
            }
        ],
    }
    tool_result = request["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_previous"
    assert tool_result["is_error"] is False


def test_anthropic_unknown_model_returns_no_cost_estimate() -> None:
    provider = AnthropicProvider(
        model_name="custom-anthropic-model",
        app_settings=anthropic_settings(),
    )

    assert provider.estimate_cost(input_tokens=1000, output_tokens=1000) is None


def test_local_model_calls_disabled_produces_clear_configuration_error() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_LOCAL_MODEL_CALLS=False,
        LOCAL_MODEL_ENDPOINT="http://localhost:11434/v1",
    )

    with pytest.raises(ModelProviderConfigError, match="ENABLE_LOCAL_MODEL_CALLS=true"):
        LocalModelProvider(app_settings=app_settings)


def test_mocked_local_model_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://localhost:11434/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-key"
        request_body = json.loads(request.content)
        assert request_body == {
            "model": "local-test-model",
            "messages": [
                {"role": "system", "content": "You are a coding agent."},
                {"role": "system", "content": "Use controlled tools only."},
                {"role": "user", "content": "Inspect the app."},
            ],
            "stream": False,
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-safe",
                "object": "chat.completion",
                "created": 1_800_000_000,
                "model": "local-test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Inspect README first."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = LocalModelProvider(
            api_key="local-key",
            app_settings=local_settings(),
            http_client=client,
        )
        response = provider.generate_response(
            [
                ModelMessage(role="system", content="You are a coding agent."),
                ModelMessage(role="developer", content="Use controlled tools only."),
                ModelMessage(role="user", content="Inspect the app."),
            ]
        )

    assert response.content == "Inspect README first."
    assert response.tool_calls == []
    assert response.input_tokens == 20
    assert response.output_tokens == 5
    assert response.estimated_cost == 0.0
    assert response.latency_seconds >= 0
    assert response.raw_response == {
        "id": "chatcmpl-safe",
        "model": "local-test-model",
        "object": "chat.completion",
        "created": 1_800_000_000,
        "finish_reason": "stop",
        "tool_call_count": 0,
        "usage": {"input_tokens": 20, "output_tokens": 5},
    }


def test_mocked_local_model_tool_call_response_and_history() -> None:
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

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        request_body = json.loads(request.content)
        assert request_body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a workspace file.",
                    "parameters": tools[0].input_schema,
                },
            }
        ]
        assert request_body["tool_choice"] == "auto"
        assert request_body["messages"][1]["tool_calls"][0] == {
            "id": "call_previous",
            "type": "function",
            "function": {"name": "list_files", "arguments": "{}"},
        }
        assert request_body["messages"][2]["role"] == "tool"
        assert request_body["messages"][2]["tool_call_id"] == "call_previous"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tool-safe",
                "object": "chat.completion",
                "created": 1_800_000_001,
                "model": "local-test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_read",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"file_path":"src/app.py"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"input_tokens": 50, "output_tokens": 12},
            },
        )

    messages = [
        ModelMessage(role="user", content="Inspect the app."),
        ModelMessage(
            role="assistant",
            content=json.dumps(
                {
                    "content": "",
                    "tool_calls": [{"id": "call_previous", "name": "list_files", "arguments": {}}],
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

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = LocalModelProvider(app_settings=local_settings(), http_client=client)
        response = provider.generate_response(messages, tools=tools)

    assert response.content == ""
    assert response.tool_calls == [
        ModelToolCall(
            id="call_read",
            name="read_file",
            arguments={"file_path": "src/app.py"},
        )
    ]
    assert response.input_tokens == 50
    assert response.output_tokens == 12
    assert response.estimated_cost == 0.0
    assert response.raw_response["tool_call_count"] == 1


def test_local_model_timeout_is_reported_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mocked timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = LocalModelProvider(
            timeout_seconds=2.5,
            app_settings=local_settings(),
            http_client=client,
        )
        with pytest.raises(ModelProviderRequestError, match="timed out after 2.5 seconds"):
            provider.generate_response([ModelMessage(role="user", content="Hello")])


def test_local_model_cost_is_always_zero() -> None:
    provider = LocalModelProvider(app_settings=local_settings())

    assert provider.estimate_cost(input_tokens=10_000, output_tokens=20_000) == 0.0


def test_provider_factory_selects_correct_provider() -> None:
    app_settings = openai_settings(
        OPENAI_DEFAULT_MODEL="configured-openai-model",
        ANTHROPIC_API_KEY="anthropic-key",
        LOCAL_MODEL_ENDPOINT="http://localhost:11434",
        ENABLE_LOCAL_MODEL_CALLS=True,
        LOCAL_MODEL_DEFAULT_MODEL="configured-local-model",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    openai_provider = factory.create("openai")
    assert isinstance(openai_provider, OpenAIProvider)
    assert openai_provider.model_name == "configured-openai-model"
    assert isinstance(factory.create("anthropic"), AnthropicProvider)
    assert factory.create("anthropic").model_name == "claude-sonnet-5"
    local_provider = factory.create("local")
    assert isinstance(local_provider, LocalModelProvider)
    assert local_provider.model_name == "configured-local-model"
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


def test_provider_factory_rejects_anthropic_when_real_calls_are_disabled() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_REAL_MODEL_CALLS=False,
        ANTHROPIC_API_KEY="anthropic-key",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    with pytest.raises(ModelProviderConfigError, match="disabled"):
        factory.create("anthropic")


def test_provider_factory_rejects_local_when_local_calls_are_disabled() -> None:
    app_settings = Settings(
        _env_file=None,
        ENABLE_LOCAL_MODEL_CALLS=False,
        LOCAL_MODEL_ENDPOINT="http://localhost:11434/v1",
    )
    factory = ModelProviderFactory(app_settings=app_settings)

    assert isinstance(factory.create("mock"), MockModelProvider)
    with pytest.raises(ModelProviderConfigError, match="disabled"):
        factory.create("local")


def test_provider_factory_rejects_unknown_provider() -> None:
    factory = ModelProviderFactory(app_settings=Settings(_env_file=None))

    with pytest.raises(ModelProviderConfigError, match="Unknown model provider"):
        factory.create("missing-provider")
