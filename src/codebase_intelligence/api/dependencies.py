"""FastAPI dependency accessors and optional single-user API authentication."""

from __future__ import annotations

from secrets import compare_digest
from typing import cast

from fastapi import Header, Request

from codebase_intelligence.container import AppContainer
from codebase_intelligence.exceptions import CodebaseIntelligenceError, ProviderUnavailableError
from codebase_intelligence.ingestion.pipeline import IngestionService
from codebase_intelligence.rag_service import RAGService


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise CodebaseIntelligenceError(
            "SERVICE_STARTING", "The service is still starting.", status_code=503
        )
    return cast(AppContainer, container)


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    container = get_container(request)
    configured = container.settings.api_key
    if configured is None or not configured.get_secret_value():
        return
    if x_api_key is None or not compare_digest(x_api_key, configured.get_secret_value()):
        raise CodebaseIntelligenceError(
            "UNAUTHORIZED", "A valid API key is required.", status_code=401
        )


def get_rag_service(request: Request) -> RAGService:
    container = get_container(request)
    if container.rag_service is None:
        raise ProviderUnavailableError("embedding")
    return container.rag_service


def get_ingestion_service(request: Request) -> IngestionService:
    container = get_container(request)
    if container.ingestion_service is None:
        raise ProviderUnavailableError("embedding")
    return container.ingestion_service
