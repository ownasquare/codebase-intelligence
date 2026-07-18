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


@pytest.mark.integration
def test_search_returns_explainable_retrieval_signals(tmp_path: Path) -> None:
    index = CodeVectorIndex(settings_for(tmp_path))
    try:
        collection = index.index(
            "repo-a",
            [chunk("repo-a", "authenticate", "validate bearer user session", "src/auth.py")],
        )

        result = index.search(
            "repo-a",
            "authentication in auth path",
            top_k=1,
            collection_name=collection,
        )[0]

        signals = result.retrieval_signals
        assert signals is not None
        assert signals.semantic_score is not None
        assert -1.0 <= signals.semantic_score <= 1.0
        assert signals.combined_score >= signals.semantic_score
        assert 0.0 <= signals.path_overlap <= 1.0
        assert 0.0 <= signals.symbol_overlap <= 1.0
        assert 0.0 <= signals.content_overlap <= 1.0
    finally:
        index.close()


@pytest.mark.integration
def test_active_index_source_listing_and_exact_detail_are_scoped(tmp_path: Path) -> None:
    index = CodeVectorIndex(settings_for(tmp_path))
    try:
        auth_first = chunk(
            "repo-a",
            "authenticate",
            "def authenticate(token): return validate(token)",
            "src/auth.py",
        ).model_copy(update={"start_line": 10, "end_line": 12})
        auth_second = chunk(
            "repo-a",
            "validate",
            "def validate(token): return token.startswith('session-')",
            "src/auth.py",
        ).model_copy(update={"start_line": 20, "end_line": 22})
        payment = chunk(
            "repo-a",
            "capture",
            "def capture(payment): return gateway.capture(payment)",
            "src/payment.py",
        ).model_copy(update={"start_line": 3, "end_line": 5})
        collection = index.index("repo-a", [auth_second, payment, auth_first])
        foreign_collection = index.index(
            "repo-b",
            [chunk("repo-b", "foreign", "secret foreign content", "src/private.py")],
        )

        files, total = index.list_sources(
            "repo-a",
            collection_name=collection,
            query="validate",
            language="PYTHON",
            limit=20,
        )
        detail = index.get_source(
            "repo-a",
            collection_name=collection,
            path="src/auth.py",
        )
        isolated, isolated_total = index.list_sources(
            "repo-a",
            collection_name=foreign_collection,
        )

        assert total == 1
        assert [source.path for source in files] == ["src/auth.py"]
        assert files[0].chunk_count == 2
        assert files[0].symbol_count == 2
        assert detail is not None
        sections, truncated = detail
        assert [section.start_line for section in sections] == [10, 20]
        assert all(section.path == "src/auth.py" for section in sections)
        assert truncated is False
        assert isolated == []
        assert isolated_total == 0
    finally:
        index.close()


@pytest.mark.integration
def test_source_listing_is_deterministic_and_bounded(tmp_path: Path) -> None:
    index = CodeVectorIndex(settings_for(tmp_path))
    try:
        collection = index.index(
            "repo-a",
            [
                chunk("repo-a", "zeta", "zeta", "zeta.py"),
                chunk("repo-a", "alpha", "alpha", "Alpha.py"),
            ],
        )

        files, total = index.list_sources(
            "repo-a",
            collection_name=collection,
            limit=1,
        )

        assert total == 2
        assert [source.path for source in files] == ["Alpha.py"]
        with pytest.raises(ValueError, match="between 1 and 500"):
            index.list_sources("repo-a", collection_name=collection, limit=0)
    finally:
        index.close()
