"""SQLite connection management and ordered manifest migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codebase_intelligence.models import utc_now


@dataclass(frozen=True, slots=True)
class Migration:
    """One append-only schema migration."""

    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE repositories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'indexing', 'ready', 'failed', 'deleting')
                ),
                source_kind TEXT NOT NULL CHECK (source_kind IN ('github', 'zip')),
                source_url TEXT,
                source_ref TEXT,
                commit_sha TEXT,
                collection_name TEXT,
                index_fingerprint TEXT,
                stats_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX repositories_status_updated_idx
            ON repositories(status, updated_at DESC, id)
            """,
        ),
    ),
    Migration(
        version=2,
        statements=(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('ingest', 'reindex', 'delete')),
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
                ),
                stage TEXT NOT NULL CHECK (
                    stage IN (
                        'queued', 'fetching', 'extracting', 'scanning', 'parsing',
                        'embedding', 'indexing', 'deleting', 'complete'
                    )
                ),
                progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                payload_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_message TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """,
            """
            CREATE INDEX jobs_claim_idx
            ON jobs(status, created_at, id)
            """,
            """
            CREATE INDEX jobs_repository_created_idx
            ON jobs(repository_id, created_at DESC, id)
            """,
            """
            CREATE INDEX jobs_stale_lease_idx
            ON jobs(status, lease_expires_at)
            """,
        ),
    ),
    Migration(
        version=3,
        statements=(
            """
            UPDATE jobs SET
                status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                error_code = 'duplicate_active_job',
                error_message = 'A newer lifecycle invariant cancelled duplicate work.',
                completed_at = updated_at
            WHERE status IN ('queued', 'running')
              AND id NOT IN (
                  SELECT MIN(id) FROM jobs
                  WHERE status IN ('queued', 'running')
                  GROUP BY repository_id
              )
            """,
            """
            CREATE UNIQUE INDEX jobs_one_active_per_repository_idx
            ON jobs(repository_id)
            WHERE status IN ('queued', 'running')
            """,
        ),
    ),
)


class Database:
    """Open short-lived SQLite connections with consistent safety pragmas."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)

    def initialize(self) -> None:
        """Create the database and apply every pending migration atomically."""

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as connection:
            try:
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                applied = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                for migration in MIGRATIONS:
                    if migration.version in applied:
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (migration.version, utc_now().isoformat(timespec="microseconds")),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def applied_migrations(self) -> tuple[int, ...]:
        """Return applied schema versions in ascending order."""

        with self.connection() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield an independent configured connection and always close it."""

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = " + str(self.busy_timeout_ms))
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a transaction isolated across threads and operating-system processes."""

        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
