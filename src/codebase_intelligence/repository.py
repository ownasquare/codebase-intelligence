"""Durable repository manifest operations."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from codebase_intelligence.database import Database
from codebase_intelligence.models import (
    RepositoryRecord,
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
    utc_now,
)


class RepositoryNotFoundError(KeyError):
    """Raised when a requested repository manifest does not exist."""


class InvalidRepositoryTransitionError(ValueError):
    """Raised when a repository status transition is not legal."""


_REPOSITORY_TRANSITIONS: dict[RepositoryStatus, frozenset[RepositoryStatus]] = {
    RepositoryStatus.QUEUED: frozenset(
        {RepositoryStatus.INDEXING, RepositoryStatus.FAILED, RepositoryStatus.DELETING}
    ),
    RepositoryStatus.INDEXING: frozenset(
        {RepositoryStatus.READY, RepositoryStatus.FAILED, RepositoryStatus.DELETING}
    ),
    RepositoryStatus.READY: frozenset(
        {RepositoryStatus.INDEXING, RepositoryStatus.FAILED, RepositoryStatus.DELETING}
    ),
    RepositoryStatus.FAILED: frozenset(
        {RepositoryStatus.QUEUED, RepositoryStatus.INDEXING, RepositoryStatus.DELETING}
    ),
    RepositoryStatus.DELETING: frozenset(),
}

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_SECRET = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_?token|api_?key|token|key|password|secret|signature|sig)=)[^&\s]+"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
)
_PREFIXED_SECRET = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
)
_UNSAFE_ERROR_CODE = re.compile(r"[^a-z0-9_]+")


def _sanitize_error_code(value: str, *, fallback: str) -> str:
    normalized = _UNSAFE_ERROR_CODE.sub("_", value.strip().lower()).strip("_")
    return (normalized or fallback)[:64]


def _sanitize_error_message(value: str | BaseException) -> str:
    message = _CONTROL_CHARACTERS.sub(" ", str(value))
    message = _BEARER_SECRET.sub("Bearer [redacted]", message)
    message = _BASIC_SECRET.sub("Basic [redacted]", message)
    message = _PREFIXED_SECRET.sub("[redacted]", message)
    message = _URL_USERINFO.sub(r"\1[redacted]@", message)
    message = _QUERY_SECRET.sub(r"\1[redacted]", message)
    message = _ASSIGNED_SECRET.sub(r"\1=[redacted]", message)
    message = " ".join(message.split())
    return (message or "Operation failed.")[:500]


def _dump_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be JSON serializable") from error


def _repository_from_row(row: sqlite3.Row) -> RepositoryRecord:
    stats_value = json.loads(str(row["stats_json"]))
    return RepositoryRecord(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        source_kind=row["source_kind"],
        source_url=row["source_url"],
        source_ref=row["source_ref"],
        commit_sha=row["commit_sha"],
        collection_name=row["collection_name"],
        index_fingerprint=row["index_fingerprint"],
        stats=RepositoryStats.model_validate(stats_value),
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validated_name(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized or len(normalized) > 100:
        raise ValueError("repository name must contain between 1 and 100 characters")
    return normalized


def _validate_transition(current: RepositoryStatus, target: RepositoryStatus) -> None:
    if target == current:
        return
    if target not in _REPOSITORY_TRANSITIONS[current]:
        raise InvalidRepositoryTransitionError(
            f"repository cannot transition from {current.value} to {target.value}"
        )


class RepositoryStore:
    """Create and evolve repository manifests in short atomic transactions."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def create_repository(
        self,
        *,
        name: str,
        source_kind: SourceKind,
        source_url: str | None = None,
        source_ref: str | None = None,
    ) -> RepositoryRecord:
        """Persist a queued repository manifest with a generated UUID."""

        repository_id = str(uuid4())
        now = utc_now().isoformat(timespec="microseconds")
        stats_json = _dump_json(RepositoryStats().model_dump(mode="json"))
        kind = SourceKind(source_kind)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO repositories (
                    id, name, status, source_kind, source_url, source_ref,
                    stats_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    _validated_name(name),
                    RepositoryStatus.QUEUED.value,
                    kind.value,
                    source_url,
                    source_ref,
                    stats_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("repository insert did not return a record")
        return _repository_from_row(row)

    def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        """Return one repository or ``None`` when it does not exist."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        return None if row is None else _repository_from_row(row)

    def list_repositories(self, *, limit: int = 100, offset: int = 0) -> list[RepositoryRecord]:
        """List repositories newest first using bounded pagination."""

        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM repositories
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_repository_from_row(row) for row in rows]

    def update_repository(
        self,
        repository_id: str,
        *,
        name: str | None = None,
        status: RepositoryStatus | None = None,
        commit_sha: str | None = None,
        collection_name: str | None = None,
        index_fingerprint: str | None = None,
        stats: RepositoryStats | Mapping[str, Any] | None = None,
    ) -> RepositoryRecord:
        """Update supplied manifest fields while enforcing legal status transitions."""

        stats_model = None if stats is None else RepositoryStats.model_validate(stats)
        target_status = None if status is None else RepositoryStatus(status)
        with self.database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if current_row is None:
                raise RepositoryNotFoundError(repository_id)
            current = _repository_from_row(current_row)
            if target_status is not None:
                _validate_transition(current.status, target_status)
            connection.execute(
                """
                UPDATE repositories SET
                    name = CASE WHEN ? THEN ? ELSE name END,
                    status = CASE WHEN ? THEN ? ELSE status END,
                    commit_sha = CASE WHEN ? THEN ? ELSE commit_sha END,
                    collection_name = CASE WHEN ? THEN ? ELSE collection_name END,
                    index_fingerprint = CASE WHEN ? THEN ? ELSE index_fingerprint END,
                    stats_json = CASE WHEN ? THEN ? ELSE stats_json END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name is not None,
                    None if name is None else _validated_name(name),
                    target_status is not None,
                    None if target_status is None else target_status.value,
                    commit_sha is not None,
                    commit_sha,
                    collection_name is not None,
                    collection_name,
                    index_fingerprint is not None,
                    index_fingerprint,
                    stats_model is not None,
                    None
                    if stats_model is None
                    else _dump_json(stats_model.model_dump(mode="json")),
                    utc_now().isoformat(timespec="microseconds"),
                    repository_id,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if updated_row is None:
            raise RuntimeError("repository update did not return a record")
        return _repository_from_row(updated_row)

    def mark_repository_ready(
        self,
        repository_id: str,
        *,
        commit_sha: str | None,
        collection_name: str,
        index_fingerprint: str,
        stats: RepositoryStats | Mapping[str, Any],
    ) -> RepositoryRecord:
        """Atomically publish a completed index and clear previous errors."""

        stats_model = RepositoryStats.model_validate(stats)
        with self.database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if current_row is None:
                raise RepositoryNotFoundError(repository_id)
            current = _repository_from_row(current_row)
            _validate_transition(current.status, RepositoryStatus.READY)
            connection.execute(
                """
                UPDATE repositories SET
                    status = ?, commit_sha = ?, collection_name = ?,
                    index_fingerprint = ?, stats_json = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    RepositoryStatus.READY.value,
                    commit_sha,
                    collection_name,
                    index_fingerprint,
                    _dump_json(stats_model.model_dump(mode="json")),
                    utc_now().isoformat(timespec="microseconds"),
                    repository_id,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if updated_row is None:
            raise RuntimeError("repository readiness update did not return a record")
        return _repository_from_row(updated_row)

    def mark_repository_failed(
        self,
        repository_id: str,
        *,
        error_code: str,
        error_message: str | BaseException,
    ) -> RepositoryRecord:
        """Persist a safe terminal indexing error without secret-bearing details."""

        with self.database.transaction() as connection:
            current_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if current_row is None:
                raise RepositoryNotFoundError(repository_id)
            current = _repository_from_row(current_row)
            _validate_transition(current.status, RepositoryStatus.FAILED)
            connection.execute(
                """
                UPDATE repositories SET
                    status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    RepositoryStatus.FAILED.value,
                    _sanitize_error_code(error_code, fallback="repository_failed"),
                    _sanitize_error_message(error_message),
                    utc_now().isoformat(timespec="microseconds"),
                    repository_id,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        if updated_row is None:
            raise RuntimeError("repository failure update did not return a record")
        return _repository_from_row(updated_row)

    def delete_repository(self, repository_id: str) -> bool:
        """Physically delete a repository manifest and cascade its jobs."""

        with self.database.transaction() as connection:
            result = connection.execute("DELETE FROM repositories WHERE id = ?", (repository_id,))
        return result.rowcount == 1
