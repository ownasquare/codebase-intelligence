"""Repository-scoped access to already-redacted source stored in an active index."""

from __future__ import annotations

from typing import Protocol

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import (
    CodebaseIntelligenceError,
    IndexMissingError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from codebase_intelligence.models import (
    RepositoryRecord,
    RepositoryStatus,
    SourceDetailResponse,
    SourceListResponse,
)
from codebase_intelligence.providers import index_fingerprint
from codebase_intelligence.vector_store import CodeVectorIndex


class RepositoryLookup(Protocol):
    def get_repository(self, repository_id: str) -> RepositoryRecord | None: ...


class SourceExplorerService:
    """Serve only repository-scoped content from the published vector collection."""

    def __init__(
        self,
        settings: Settings,
        repositories: RepositoryLookup,
        vector_index: CodeVectorIndex,
    ) -> None:
        self.settings = settings
        self.repositories = repositories
        self.vector_index = vector_index

    def _ready_repository(self, repository_id: str) -> RepositoryRecord:
        repository = self.repositories.get_repository(repository_id)
        if repository is None:
            raise ResourceNotFoundError("Repository")
        if repository.status is not RepositoryStatus.READY:
            raise ResourceConflictError(
                "Repository must be ready before indexed source can be explored."
            )
        if not repository.collection_name or repository.index_fingerprint != index_fingerprint(
            self.settings
        ):
            raise ResourceConflictError(
                "Repository index settings have changed; reindex before exploring source."
            )
        if not self.vector_index.has_collection(
            repository_id,
            collection_name=repository.collection_name,
        ):
            raise IndexMissingError
        return repository

    @staticmethod
    def _validated_path(path: str) -> str:
        if (
            not path
            or path != path.strip()
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise CodebaseIntelligenceError(
                "INVALID_SOURCE_PATH",
                "Source path must be a normalized repository-relative path.",
                status_code=422,
            )
        return path

    def list_sources(
        self,
        repository_id: str,
        *,
        query: str | None = None,
        language: str | None = None,
        limit: int = 200,
    ) -> SourceListResponse:
        repository = self._ready_repository(repository_id)
        collection = repository.collection_name
        if collection is None:  # pragma: no cover - narrowed by _ready_repository
            raise IndexMissingError
        normalized_query = query.strip() if query and query.strip() else None
        normalized_language = language.strip() if language and language.strip() else None
        files, total = self.vector_index.list_sources(
            repository_id,
            collection_name=collection,
            query=normalized_query,
            language=normalized_language,
            limit=limit,
        )
        return SourceListResponse(
            repository_id=repository_id,
            collection_name=collection,
            total=total,
            files=files,
        )

    def get_source(self, repository_id: str, *, path: str) -> SourceDetailResponse:
        repository = self._ready_repository(repository_id)
        collection = repository.collection_name
        if collection is None:  # pragma: no cover - narrowed by _ready_repository
            raise IndexMissingError
        normalized_path = self._validated_path(path)
        result = self.vector_index.get_source(
            repository_id,
            collection_name=collection,
            path=normalized_path,
        )
        if result is None:
            raise ResourceNotFoundError("Indexed source")
        sections, truncated = result
        return SourceDetailResponse(
            repository_id=repository_id,
            collection_name=collection,
            path=normalized_path,
            language=sections[0].language,
            sections=sections,
            truncated=truncated,
        )


__all__ = ["SourceExplorerService"]
