from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.embeddings import (
    EmbeddingsConfigError,
    EmbeddingsError,
    EmbeddingsProvider,
    LocalEmbeddingsProvider,
    MockEmbeddingsProvider,
    OpenAIEmbeddingsProvider,
    create_embeddings_provider,
)


@pytest.fixture(autouse=True)
def block_real_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openai.OpenAI", Mock(side_effect=AssertionError("Real API call in test")))


def config(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "ENABLE_REAL_EMBEDDINGS": False,
            "EMBEDDINGS_PROVIDER": "mock",
            "OPENAI_API_KEY": "",
            "EMBEDDING_VECTOR_DIMENSIONS": 3,
            "EMBEDDING_BATCH_SIZE": 32,
            **overrides,
        },
    )


def test_mock_embedding_provider_is_deterministic_and_reports_zero_usage() -> None:
    provider = MockEmbeddingsProvider(app_settings=config())
    assert isinstance(provider, EmbeddingsProvider)
    vectors = provider.embed_texts(["one phrase", "different phrase"])
    assert vectors == provider.embed_texts(["one phrase", "different phrase"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 3
    assert provider.last_usage.input_tokens == 0
    assert provider.last_usage.estimated_cost == 0


def test_real_embeddings_disabled_even_with_client_and_key() -> None:
    client = Mock()
    with pytest.raises(EmbeddingsConfigError, match="ENABLE_REAL_EMBEDDINGS=true"):
        OpenAIEmbeddingsProvider(app_settings=config(OPENAI_API_KEY="test-key"), client=client)
    client.embeddings.create.assert_not_called()


def test_real_embeddings_require_key() -> None:
    with pytest.raises(EmbeddingsConfigError, match="OPENAI_API_KEY"):
        OpenAIEmbeddingsProvider(app_settings=config(ENABLE_REAL_EMBEDDINGS=True))


def test_mock_sdk_normalization_dimensions_and_usage() -> None:
    client = Mock()
    client.embeddings.create.return_value = sdk_response([[1.0, 0, 0], [0, 1.0, 0]], reverse=True)
    provider = OpenAIEmbeddingsProvider(
        app_settings=config(ENABLE_REAL_EMBEDDINGS=True, OPENAI_API_KEY="test-key"),
        client=client,
    )
    vectors = provider.embed_texts(["first", "second"])
    assert vectors == [[1.0, 0, 0], [0, 1.0, 0]]
    client.embeddings.create.assert_called_once_with(
        input=["first", "second"],
        model="text-embedding-3-small",
        dimensions=3,
        encoding_format="float",
    )
    assert provider.last_usage.input_tokens == 100
    assert provider.last_usage.estimated_cost == pytest.approx(0.000002)
    assert provider.last_usage.latency_seconds >= 0


@pytest.mark.parametrize("vectors", [[[1, 2]], [[float("nan"), 0, 1]], [[0, 0, 0]], []])
def test_invalid_sdk_vectors_are_rejected(vectors) -> None:
    client = Mock()
    client.embeddings.create.return_value = sdk_response(vectors)
    provider = OpenAIEmbeddingsProvider(
        app_settings=config(ENABLE_REAL_EMBEDDINGS=True, OPENAI_API_KEY="test-key"),
        client=client,
    )
    with pytest.raises(EmbeddingsError):
        provider.embed_texts(["test"])


@pytest.mark.parametrize("texts", [[], [""], ["x" * 8001], ["x"] * 33, ["x" * 8000] * 17])
def test_embedding_input_limits_apply_before_network(texts) -> None:
    client = Mock()
    provider = OpenAIEmbeddingsProvider(
        app_settings=config(ENABLE_REAL_EMBEDDINGS=True, OPENAI_API_KEY="test-key"),
        client=client,
    )
    with pytest.raises(EmbeddingsError):
        provider.embed_texts(texts)
    client.embeddings.create.assert_not_called()


def test_provider_factory_and_local_skeleton() -> None:
    assert isinstance(create_embeddings_provider(app_settings=config()), MockEmbeddingsProvider)
    with pytest.raises(EmbeddingsConfigError):
        create_embeddings_provider(app_settings=config(EMBEDDINGS_PROVIDER="openai"))
    assert isinstance(
        create_embeddings_provider(
            app_settings=config(
                EMBEDDINGS_PROVIDER="openai",
                ENABLE_REAL_EMBEDDINGS=True,
                OPENAI_API_KEY="test-key",
            )
        ),
        OpenAIEmbeddingsProvider,
    )
    local = create_embeddings_provider(app_settings=config(EMBEDDINGS_PROVIDER="local"))
    assert isinstance(local, LocalEmbeddingsProvider)
    with pytest.raises(EmbeddingsConfigError, match="not implemented"):
        local.embed_texts(["text"])


def test_flag_is_rechecked_and_provider_exception_does_not_leak_inputs() -> None:
    cfg = config(ENABLE_REAL_EMBEDDINGS=True, OPENAI_API_KEY="test-key")
    client = Mock()
    client.embeddings.create.side_effect = RuntimeError("API_KEY=secret and request text")
    provider = OpenAIEmbeddingsProvider(app_settings=cfg, client=client)
    with pytest.raises(EmbeddingsError, match="OpenAI embedding request failed") as error:
        provider.embed_texts(["source code"])
    assert "secret" not in str(error.value)
    cfg.enable_real_embeddings = False
    with pytest.raises(EmbeddingsConfigError):
        provider.embed_texts(["source code"])
    assert client.embeddings.create.call_count == 1


def test_embedding_configuration_defaults_and_validation() -> None:
    assert config(EMBEDDING_VECTOR_DIMENSIONS="").embedding_vector_dimensions is None
    assert config().enable_real_embeddings is False
    with pytest.raises(ValidationError):
        config(EMBEDDING_CHUNK_SIZE_CHARS=128, EMBEDDING_CHUNK_OVERLAP_CHARS=128)
    with pytest.raises(ValidationError):
        config(EMBEDDING_BATCH_SIZE=0)


def sdk_response(vectors, *, reverse: bool = False):
    data = [SimpleNamespace(index=index, embedding=vector) for index, vector in enumerate(vectors)]
    return SimpleNamespace(
        data=list(reversed(data)) if reverse else data, usage=SimpleNamespace(prompt_tokens=100)
    )
