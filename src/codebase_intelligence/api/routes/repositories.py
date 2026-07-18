"""Repository lifecycle endpoints for GitHub and ZIP sources."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile, status

from codebase_intelligence.api.dependencies import get_container, get_ingestion_service
from codebase_intelligence.exceptions import IngestionError, ResourceNotFoundError
from codebase_intelligence.ingestion.pipeline import IngestionService
from codebase_intelligence.models import (
    GitHubRepositoryRequest,
    RepositoryCreateResponse,
    RepositoryRecord,
)

router = APIRouter(prefix="/repositories", tags=["repositories"])


@router.get("", response_model=list[RepositoryRecord])
def list_repositories(request: Request) -> list[RepositoryRecord]:
    return get_container(request).repositories.list_repositories()


@router.post("", response_model=RepositoryCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_github_repository(
    payload: GitHubRepositoryRequest,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
    github_token: Annotated[
        str | None,
        Header(alias="X-GitHub-Token", description="Request-only token for a private repository"),
    ] = None,
) -> RepositoryCreateResponse:
    return await service.submit_github(payload, token=github_token)


@router.post(
    "/upload",
    response_model=RepositoryCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_repository(
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
    file: Annotated[UploadFile, File(description="A bounded ZIP archive")],
    name: Annotated[str | None, Form(max_length=100)] = None,
) -> RepositoryCreateResponse:
    if name is not None and not name.strip():
        raise IngestionError("INVALID_REPOSITORY_NAME", "Repository name cannot be empty.")
    try:
        return await service.submit_upload(file, name=name)
    finally:
        await file.close()


@router.get("/{repository_id}", response_model=RepositoryRecord)
def get_repository(repository_id: str, request: Request) -> RepositoryRecord:
    repository = get_container(request).repositories.get_repository(repository_id)
    if repository is None:
        raise ResourceNotFoundError("Repository")
    return repository


@router.post(
    "/{repository_id}/reindex",
    response_model=RepositoryCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_repository(
    repository_id: str,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> RepositoryCreateResponse:
    return service.enqueue_reindex(repository_id)


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository(
    repository_id: str,
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> Response:
    await asyncio.to_thread(service.delete_repository, repository_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
