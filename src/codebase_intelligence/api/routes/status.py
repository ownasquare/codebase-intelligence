"""Non-secret runtime configuration status."""

from __future__ import annotations

from fastapi import APIRouter, Request

from codebase_intelligence import __version__
from codebase_intelligence.api.dependencies import get_container
from codebase_intelligence.models import ProviderState, StatusResponse

router = APIRouter(tags=["status"])


@router.get("/status", response_model=StatusResponse)
def status(request: Request) -> StatusResponse:
    container = get_container(request)
    settings = container.settings
    embedding_model = (
        settings.voyage_embedding_model
        if settings.embedding_provider == "voyage"
        else settings.openai_embedding_model
        if settings.embedding_provider == "openai"
        else "deterministic-hash-v1"
    )
    return StatusResponse(
        application=settings.app_name,
        version=__version__,
        environment=settings.environment,
        embedding=ProviderState(
            provider=settings.embedding_provider,
            model=embedding_model,
            ready=container.embedding_operational,
            mode="demo" if settings.embedding_provider == "deterministic" else "production",
        ),
        answer=ProviderState(
            provider=settings.answer_provider,
            model=(
                settings.openai_chat_model
                if settings.answer_provider == "openai"
                else "ranked-source-extracts"
            ),
            ready=container.answer_operational,
            mode="demo" if settings.answer_provider == "extractive" else "production",
        ),
        qdrant_mode="server" if settings.qdrant_url else "embedded",
        inline_worker=settings.inline_worker,
    )
