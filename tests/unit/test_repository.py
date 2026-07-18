from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codebase_intelligence.database import Database
from codebase_intelligence.models import (
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
)
from codebase_intelligence.repository import (
    InvalidRepositoryTransitionError,
    RepositoryNotFoundError,
    RepositoryStore,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "manifest.sqlite3", busy_timeout_ms=7_500)
    database.initialize()
    return database


@pytest.fixture
def store(database: Database) -> RepositoryStore:
    return RepositoryStore(database)


def test_database_initializes_wal_foreign_keys_timeout_and_migrations(
    database: Database,
) -> None:
    with database.connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 7_500
    assert database.applied_migrations() == (1, 2, 3)


def test_create_get_list_update_ready_and_delete_repository(
    store: RepositoryStore,
) -> None:
    created = store.create_repository(
        name="payments-service",
        source_kind=SourceKind.GITHUB,
        source_url="https://github.com/example/payments-service",
        source_ref="main",
    )

    assert len(created.id) == 36
    assert created.status is RepositoryStatus.QUEUED
    assert created.stats == RepositoryStats()
    assert store.get_repository(created.id) == created
    assert store.list_repositories() == [created]

    indexing = store.update_repository(
        created.id,
        status=RepositoryStatus.INDEXING,
        commit_sha="abc123",
    )
    ready = store.mark_repository_ready(
        created.id,
        commit_sha="abc123",
        collection_name="repo_payments",
        index_fingerprint="sha256:feedface",
        stats=RepositoryStats(file_count=12, chunk_count=41, languages={"python": 12}),
    )

    assert indexing.status is RepositoryStatus.INDEXING
    assert ready.status is RepositoryStatus.READY
    assert ready.stats.file_count == 12
    assert ready.error_code is None
    assert ready.error_message is None
    assert store.delete_repository(created.id) is True
    assert store.delete_repository(created.id) is False
    assert store.get_repository(created.id) is None


def test_repository_failures_are_sanitized(store: RepositoryStore) -> None:
    repository = store.create_repository(name="unsafe", source_kind=SourceKind.ZIP)

    failed = store.mark_repository_failed(
        repository.id,
        error_code="provider-failed! bearer-secret",
        error_message=(
            "Authorization: Bearer super-secret-token "
            "https://user:password@example.com/archive?token=also-secret "
            "Basic dXNlcjpwYXNzd29yZA== sk-proj-exampletoken12345"
        ),
    )

    assert failed.status is RepositoryStatus.FAILED
    assert failed.error_code == "provider_failed_bearer_secret"
    assert failed.error_message is not None
    assert "super-secret-token" not in failed.error_message
    assert "password" not in failed.error_message
    assert "also-secret" not in failed.error_message
    assert "dXNlcjpwYXNzd29yZA" not in failed.error_message
    assert "exampletoken12345" not in failed.error_message
    assert "[redacted]" in failed.error_message


def test_repository_transitions_and_missing_records_are_rejected(
    store: RepositoryStore,
) -> None:
    repository = store.create_repository(name="auth", source_kind=SourceKind.ZIP)

    with pytest.raises(InvalidRepositoryTransitionError):
        store.mark_repository_ready(
            repository.id,
            commit_sha=None,
            collection_name="repo_auth",
            index_fingerprint="sha256:auth",
            stats=RepositoryStats(),
        )

    with pytest.raises(RepositoryNotFoundError):
        store.update_repository("00000000-0000-0000-0000-000000000000", name="missing")


def test_foreign_keys_are_enforced_on_every_connection(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, repository_id, kind, status, stage, progress, attempt,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                "ingest",
                "queued",
                "queued",
                0,
                0,
                "{}",
                "2026-07-17T00:00:00.000000+00:00",
                "2026-07-17T00:00:00.000000+00:00",
            ),
        )
