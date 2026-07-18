"""Protected access to redacted source from a repository's active index."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from codebase_intelligence.api.dependencies import get_container
from codebase_intelligence.exceptions import ProviderUnavailableError
from codebase_intelligence.models import SourceDetailResponse, SourceListResponse
from codebase_intelligence.source_service import SourceExplorerService

router = APIRouter(prefix="/repositories", tags=["explorer"])


def _explorer(request: Request) -> SourceExplorerService:
    service = get_container(request).source_explorer
    if service is None:
        raise ProviderUnavailableError("embedding")
    return service


@router.get("/{repository_id}/sources", response_model=SourceListResponse)
def list_sources(
    repository_id: str,
    request: Request,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    language: Annotated[
        str | None,
        Query(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_+.#-]+$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> SourceListResponse:
    return _explorer(request).list_sources(
        repository_id,
        query=q,
        language=language,
        limit=limit,
    )


@router.get("/{repository_id}/source", response_model=SourceDetailResponse)
def get_source(
    repository_id: str,
    request: Request,
    path: Annotated[str, Query(min_length=1, max_length=1000)],
) -> SourceDetailResponse:
    return _explorer(request).get_source(repository_id, path=path)


__all__ = ["router"]
