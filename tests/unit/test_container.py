from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.container import AppContainer


def _settings(path: Path, *, inline_worker: bool = False) -> Settings:
    return Settings(
        environment="test",
        data_dir=path,
        embedding_provider="deterministic",
        answer_provider="extractive",
        inline_worker=inline_worker,
        worker_poll_seconds=0.1,
    )


def test_answer_provider_initialization_failure_keeps_ingestion_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_completion(_settings: Settings) -> None:
        raise RuntimeError("answer provider failed")

    monkeypatch.setattr(
        "codebase_intelligence.container.create_completion_provider",
        fail_completion,
    )
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        embedding_provider="deterministic",
        answer_provider="openai",
        openai_api_key="test-key",
        inline_worker=False,
    )
    container = AppContainer(settings, enable_inline_worker=False)
    try:
        assert container.embedding_operational is True
        assert container.ingestion_service is not None
        assert container.rag_service is not None
        assert container.answer_operational is False
    finally:
        asyncio.run(container.close())


def test_missing_answer_credentials_report_provider_unavailable(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        data_dir=tmp_path,
        embedding_provider="deterministic",
        answer_provider="openai",
        openai_api_key=None,
        inline_worker=False,
    )
    container = AppContainer(settings, enable_inline_worker=False)
    try:
        assert container.embedding_operational is True
        assert container.rag_service is not None
        assert container.answer_operational is False
    finally:
        asyncio.run(container.close())


def test_inline_worker_lifecycle_is_healthy(tmp_path: Path) -> None:
    container = AppContainer(_settings(tmp_path, inline_worker=True))

    async def exercise() -> None:
        await container.start()
        assert container.readiness_checks() == {
            "database": True,
            "embedding": True,
            "qdrant": True,
            "worker": True,
        }
        assert container.worker is not None
        container.worker.stop()
        assert container._worker_task is not None
        await container._worker_task
        assert container.readiness_checks()["worker"] is False
        await container.close()

    asyncio.run(exercise())
