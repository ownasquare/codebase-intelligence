from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import (
    CodebaseIntelligenceError,
    IndexMissingError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from codebase_intelligence.models import (
    CodeChunk,
    RepositoryRecord,
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
)
from codebase_intelligence.providers import index_fingerprint
from codebase_intelligence.source_service import SourceExplorerService
from codebase_intelligence.vector_store import CodeVectorIndex

pytestmark = pytest.mark.integration


class FakeRepositories:
    def __init__(self, repository: RepositoryRecord | None) -> None:
        self.repository = repository

    def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        if self.repository is not None and self.repository.id == repository_id:
            return self.repository
        return None


def _settings(path: Path) -> Settings:
    return Settings(
        environment="test",
        data_dir=path,
        embedding_provider="deterministic",
        deterministic_embedding_dimension=128,
        answer_provider="extractive",
    )


def _chunk(repository_id: str, identifier: str, path: str, text: str) -> CodeChunk:
    return CodeChunk(
        id=identifier,
        repository_id=repository_id,
        commit_sha="a" * 40,
        path=path,
        language="python",
        symbol=identifier,
        symbol_kind="function",
        start_line=4,
        end_line=6,
        parser="tree_sitter",
        text=text,
        content_hash=identifier.rjust(64, "0"),
    )


def _repository(
    settings: Settings,
    *,
    collection_name: str | None,
    status: RepositoryStatus = RepositoryStatus.READY,
    fingerprint: str | None = None,
) -> RepositoryRecord:
    now = datetime.now(UTC)
    return RepositoryRecord(
        id="repo-a",
        name="sample",
        status=status,
        source_kind=SourceKind.ZIP,
        collection_name=collection_name,
        index_fingerprint=fingerprint or index_fingerprint(settings),
        stats=RepositoryStats(file_count=1, chunk_count=1),
        created_at=now,
        updated_at=now,
    )


def test_source_explorer_lists_and_returns_only_active_index_content(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    index = CodeVectorIndex(settings)
    try:
        collection = index.index(
            "repo-a",
            [
                _chunk(
                    "repo-a",
                    "authenticate",
                    "src/auth.py",
                    "def authenticate(token): return token.startswith('session-')",
                )
            ],
        )
        service = SourceExplorerService(
            settings,
            FakeRepositories(_repository(settings, collection_name=collection)),
            index,
        )

        listed = service.list_sources("repo-a", query="authenticate", language="python")
        detail = service.get_source("repo-a", path="src/auth.py")

        assert listed.repository_id == "repo-a"
        assert listed.collection_name == collection
        assert listed.total == 1
        assert listed.files[0].path == "src/auth.py"
        assert detail.collection_name == collection
        assert detail.sections[0].content.startswith("def authenticate")
        assert detail.truncated is False
    finally:
        index.close()


def test_source_explorer_enforces_manifest_and_collection_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    index = CodeVectorIndex(settings)
    try:
        missing = SourceExplorerService(settings, FakeRepositories(None), index)
        with pytest.raises(ResourceNotFoundError, match="Repository"):
            missing.list_sources("repo-a")

        not_ready = SourceExplorerService(
            settings,
            FakeRepositories(
                _repository(
                    settings,
                    collection_name="pending",
                    status=RepositoryStatus.INDEXING,
                )
            ),
            index,
        )
        with pytest.raises(ResourceConflictError, match="must be ready"):
            not_ready.list_sources("repo-a")

        stale = SourceExplorerService(
            settings,
            FakeRepositories(
                _repository(settings, collection_name="stale", fingerprint="old-settings")
            ),
            index,
        )
        with pytest.raises(ResourceConflictError, match="reindex"):
            stale.list_sources("repo-a")

        missing_index = SourceExplorerService(
            settings,
            FakeRepositories(_repository(settings, collection_name="missing")),
            index,
        )
        with pytest.raises(IndexMissingError, match="reindex"):
            missing_index.list_sources("repo-a")
    finally:
        index.close()


def test_source_explorer_rejects_cross_repository_and_unsafe_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    index = CodeVectorIndex(settings)
    try:
        foreign_collection = index.index(
            "repo-b",
            [_chunk("repo-b", "private", "src/private.py", "foreign indexed content")],
        )
        service = SourceExplorerService(
            settings,
            FakeRepositories(_repository(settings, collection_name=foreign_collection)),
            index,
        )

        listed = service.list_sources("repo-a")
        assert listed.files == []
        with pytest.raises(ResourceNotFoundError, match="Indexed source"):
            service.get_source("repo-a", path="src/private.py")
        with pytest.raises(CodebaseIntelligenceError, match="repository-relative"):
            service.get_source("repo-a", path="../private.py")
    finally:
        index.close()
