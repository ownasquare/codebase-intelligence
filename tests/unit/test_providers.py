import asyncio
from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import ProviderUnavailableError
from codebase_intelligence.providers import (
    DeterministicEmbedding,
    create_completion_provider,
    create_embedding_model,
    index_fingerprint,
)


def test_deterministic_embedding_is_stable_and_query_sensitive() -> None:
    model = DeterministicEmbedding(model_name="test", dimension=128)

    first = model.get_text_embedding("authenticate active user")
    second = model.get_text_embedding("authenticate active user")
    different = model.get_text_embedding("capture payment gateway")

    assert first == second
    assert first != different
    assert len(first) == 128


def test_missing_paid_provider_credential_fails_closed(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        embedding_provider="voyage",
        voyage_api_key=None,
    )

    with pytest.raises(ProviderUnavailableError, match="voyage"):
        create_embedding_model(settings)


def test_index_fingerprint_changes_with_embedding_contract(tmp_path) -> None:
    first = Settings(
        data_dir=tmp_path,
        embedding_provider="deterministic",
        deterministic_embedding_dimension=128,
    )
    second = Settings(
        data_dir=tmp_path,
        embedding_provider="deterministic",
        deterministic_embedding_dimension=256,
    )

    assert index_fingerprint(first) != index_fingerprint(second)


def test_paid_provider_factories_receive_explicit_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeEmbedding:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("codebase_intelligence.providers.VoyageEmbedding", FakeEmbedding)
    voyage = Settings(
        data_dir=tmp_path,
        embedding_provider="voyage",
        voyage_api_key="test-voyage-key",
    )
    create_embedding_model(voyage)
    assert calls[-1]["model_name"] == "voyage-code-3"
    assert calls[-1]["voyage_api_key"] == "test-voyage-key"

    monkeypatch.setattr("codebase_intelligence.providers.OpenAIEmbedding", FakeEmbedding)
    openai = Settings(
        data_dir=tmp_path,
        embedding_provider="openai",
        openai_api_key="test-openai-key",
    )
    create_embedding_model(openai)
    assert calls[-1]["model"] == "text-embedding-3-small"
    assert calls[-1]["api_key"] == "test-openai-key"


def test_completion_provider_uses_configured_model_without_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeLLM:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        async def acomplete(self, prompt: str) -> str:
            return f"grounded: {prompt}"

    monkeypatch.setattr("codebase_intelligence.providers.OpenAI", FakeLLM)
    settings = Settings(
        data_dir=tmp_path,
        embedding_provider="deterministic",
        answer_provider="openai",
        openai_api_key="test-openai-key",
    )
    provider = create_completion_provider(settings)
    assert provider is not None
    answer = asyncio.run(provider.complete("question"))

    assert answer == "grounded: question"
    assert calls[-1]["model"] == "gpt-5-mini"
    assert calls[-1]["temperature"] == 0.1
