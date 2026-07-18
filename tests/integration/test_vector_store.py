from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.models import CodeChunk
from codebase_intelligence.vector_store import CodeVectorIndex


def chunk(repository_id: str, identifier: str, text: str, path: str) -> CodeChunk:
    return CodeChunk(
        id=identifier,
        repository_id=repository_id,
        commit_sha="a" * 40,
        path=path,
        language="python",
        symbol=identifier,
        symbol_kind="function",
        start_line=1,
        end_line=2,
        parser="tree_sitter",
        text=text,
        content_hash=identifier.rjust(64, "0"),
    )


def settings_for(path: Path) -> Settings:
    return Settings(
        environment="test",
        data_dir=path,
        embedding_provider="deterministic",
        deterministic_embedding_dimension=128,
        answer_provider="extractive",
    )


@pytest.mark.integration
def test_repository_collections_are_isolated_and_deletable(tmp_path: Path) -> None:
    index = CodeVectorIndex(settings_for(tmp_path))
    try:
        auth_collection = index.index(
            "repo-a",
            [chunk("repo-a", "auth", "authenticate active user session", "src/auth.py")],
        )
        payment_collection = index.index(
            "repo-b",
            [chunk("repo-b", "payment", "capture payment gateway charge", "src/payment.py")],
        )

        auth_results = index.search(
            "repo-a",
            "authenticate user",
            top_k=5,
            collection_name=auth_collection,
        )
        assert auth_results[0].chunk.path == "src/auth.py"
        assert all(result.chunk.repository_id == "repo-a" for result in auth_results)

        assert index.delete("repo-a", collection_name=auth_collection) is True
        assert (
            index.search(
                "repo-a",
                "authenticate",
                top_k=5,
                collection_name=auth_collection,
            )
            == []
        )
        assert index.search(
            "repo-b",
            "payment gateway",
            top_k=5,
            collection_name=payment_collection,
        )
    finally:
        index.close()


@pytest.mark.integration
def test_qdrant_local_collection_survives_client_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    first = CodeVectorIndex(settings)
    collection = first.index(
        "repo-a",
        [chunk("repo-a", "auth", "authenticate bearer token", "src/auth.py")],
    )
    first.close()

    second = CodeVectorIndex(settings)
    try:
        results = second.search(
            "repo-a",
            "bearer authentication",
            top_k=3,
            collection_name=collection,
        )
        assert results[0].chunk.id == "auth"
    finally:
        second.close()


@pytest.mark.integration
def test_cross_repository_chunk_is_rejected(tmp_path: Path) -> None:
    index = CodeVectorIndex(settings_for(tmp_path))
    try:
        with pytest.raises(ValueError, match="every chunk"):
            index.index(
                "repo-a",
                [chunk("repo-b", "foreign", "other repository", "src/other.py")],
            )
    finally:
        index.close()


@pytest.mark.integration
def test_repository_collection_discovery_survives_prefix_changes(tmp_path: Path) -> None:
    original_settings = settings_for(tmp_path)
    original_settings.qdrant_collection_prefix = "original_prefix"
    original = CodeVectorIndex(original_settings)
    old_collection = original.index(
        "repo-a",
        [chunk("repo-a", "auth", "authenticate bearer token", "src/auth.py")],
    )
    original.close()

    changed_settings = settings_for(tmp_path)
    changed_settings.qdrant_collection_prefix = "changed_prefix"
    changed = CodeVectorIndex(changed_settings)
    try:
        assert changed.repository_collections("repo-a") == [old_collection]
        assert changed.delete("repo-a", collection_name=old_collection) is True
        assert changed.repository_collections("repo-a") == []
    finally:
        changed.close()
