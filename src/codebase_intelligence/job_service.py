"""Durable job queue with leases, retries, and legal progress transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from codebase_intelligence.database import Database
from codebase_intelligence.models import JobKind, JobRecord, JobStage, JobStatus, utc_now
from codebase_intelligence.repository import (
    RepositoryNotFoundError,
    _dump_json,
    _sanitize_error_code,
    _sanitize_error_message,
)


class JobNotFoundError(KeyError):
    """Raised when a requested job does not exist."""


class InvalidJobTransitionError(ValueError):
    """Raised when a job lifecycle or progress transition is illegal."""


class JobLeaseError(InvalidJobTransitionError):
    """Raised when a worker does not own a live lease for the job."""


class ActiveJobExistsError(InvalidJobTransitionError):
    """Raised when a repository already has queued or running work."""


@dataclass(frozen=True, slots=True)
class LeaseRecoveryResult:
    """Counts produced by a stale-lease recovery pass."""

    requeued: int
    failed: int


_STAGE_PATHS: dict[JobKind, tuple[JobStage, ...]] = {
    JobKind.INGEST: (
        JobStage.QUEUED,
        JobStage.FETCHING,
        JobStage.EXTRACTING,
        JobStage.SCANNING,
        JobStage.PARSING,
        JobStage.EMBEDDING,
        JobStage.INDEXING,
        JobStage.COMPLETE,
    ),
    JobKind.REINDEX: (
        JobStage.QUEUED,
        JobStage.FETCHING,
        JobStage.EXTRACTING,
        JobStage.SCANNING,
        JobStage.PARSING,
        JobStage.EMBEDDING,
        JobStage.INDEXING,
        JobStage.COMPLETE,
    ),
    JobKind.DELETE: (JobStage.QUEUED, JobStage.DELETING, JobStage.COMPLETE),
}


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        repository_id=row["repository_id"],
        kind=row["kind"],
        status=row["status"],
        stage=row["stage"],
        progress=row["progress"],
        attempt=row["attempt"],
        payload=json.loads(str(row["payload_json"])),
        error_code=row["error_code"],
        error_message=row["error_message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _validate_worker_id(worker_id: str) -> str:
    normalized = worker_id.strip()
    if not normalized or len(normalized) > 200 or any(ord(char) < 32 for char in normalized):
        raise ValueError("worker_id must contain between 1 and 200 printable characters")
    return normalized


class JobService:
    """Coordinate jobs safely across worker threads and processes."""

    def __init__(
        self,
        database: Database,
        *,
        lease_seconds: int = 300,
        max_attempts: int = 3,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.database = database
        self.lease_seconds = int(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.clock = clock
        self.database.initialize()

    def enqueue_job(
        self,
        repository_id: str,
        kind: JobKind,
        payload: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """Enqueue a job for an existing repository using a generated UUID."""

        job_id = str(uuid4())
        now = self._now_iso()
        job_kind = JobKind(kind)
        payload_json = _dump_json({} if payload is None else payload)
        with self.database.transaction() as connection:
            repository_exists = connection.execute(
                "SELECT 1 FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
            if repository_exists is None:
                raise RepositoryNotFoundError(repository_id)
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, repository_id, kind, status, stage, progress, attempt,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        repository_id,
                        job_kind.value,
                        JobStatus.QUEUED.value,
                        JobStage.QUEUED.value,
                        0,
                        0,
                        payload_json,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ActiveJobExistsError(
                    "repository already has queued or running work"
                ) from error
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError("job insert did not return a record")
        return _job_from_row(row)

    def get_job(self, job_id: str) -> JobRecord | None:
        """Return one job or ``None`` when it does not exist."""

        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job_from_row(row)

    def list_jobs(
        self,
        *,
        repository_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRecord]:
        """List jobs newest first with optional repository and status filters."""

        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        status_value = None if status is None else JobStatus(status).value
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE (? IS NULL OR repository_id = ?)
                  AND (? IS NULL OR status = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    repository_id,
                    repository_id,
                    status_value,
                    status_value,
                    limit,
                    offset,
                ),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def claim_next_job(self, worker_id: str) -> JobRecord | None:
        """Atomically recover stale work and lease the oldest queued job."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.clock())
        now_iso = now.isoformat(timespec="microseconds")
        lease_expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat(
            timespec="microseconds"
        )
        with self.database.transaction() as connection:
            self._recover_stale_jobs(connection, now_iso)
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ? AND attempt < ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (JobStatus.QUEUED.value, self.max_attempts),
            ).fetchone()
            if row is None:
                return None
            result = connection.execute(
                """
                UPDATE jobs SET
                    status = ?, attempt = attempt + 1, lease_owner = ?,
                    lease_expires_at = ?, started_at = COALESCE(started_at, ?),
                    updated_at = ?, error_code = NULL, error_message = NULL
                WHERE id = ? AND status = ? AND attempt < ?
                """,
                (
                    JobStatus.RUNNING.value,
                    owner,
                    lease_expires_at,
                    now_iso,
                    now_iso,
                    row["id"],
                    JobStatus.QUEUED.value,
                    self.max_attempts,
                ),
            )
            if result.rowcount != 1:
                return None
            claimed_row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        if claimed_row is None:
            raise RuntimeError("job claim did not return a record")
        return _job_from_row(claimed_row)

    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        *,
        stage: JobStage,
        progress: int,
    ) -> JobRecord:
        """Advance a live job monotonically and renew its worker lease."""

        owner = _validate_worker_id(worker_id)
        if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress < 100:
            raise ValueError("progress must be an integer between 0 and 99")
        target_stage = JobStage(stage)
        now = _as_utc(self.clock())
        now_iso = now.isoformat(timespec="microseconds")
        lease_expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat(
            timespec="microseconds"
        )
        with self.database.transaction() as connection:
            row = self._required_job(connection, job_id)
            job = _job_from_row(row)
            self._require_live_lease(job, owner, now)
            self._validate_progress(job, target_stage, progress)
            connection.execute(
                """
                UPDATE jobs SET stage = ?, progress = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_stage.value, progress, lease_expires_at, now_iso, job_id),
            )
            updated_row = self._required_job(connection, job_id)
        return _job_from_row(updated_row)

    def succeed_job(self, job_id: str, worker_id: str) -> JobRecord:
        """Complete a live worker-owned job at 100 percent."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.clock())
        now_iso = now.isoformat(timespec="microseconds")
        with self.database.transaction() as connection:
            job = _job_from_row(self._required_job(connection, job_id))
            self._require_live_lease(job, owner, now)
            connection.execute(
                """
                UPDATE jobs SET
                    status = ?, stage = ?, progress = 100, lease_owner = NULL,
                    lease_expires_at = NULL, error_code = NULL, error_message = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    JobStage.COMPLETE.value,
                    now_iso,
                    now_iso,
                    job_id,
                ),
            )
            updated_row = self._required_job(connection, job_id)
        return _job_from_row(updated_row)

    def renew_lease(self, job_id: str, worker_id: str) -> JobRecord:
        """Renew only the lease, without replaying mutable stage or progress values."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.clock())
        now_iso = now.isoformat(timespec="microseconds")
        lease_expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat(
            timespec="microseconds"
        )
        with self.database.transaction() as connection:
            job = _job_from_row(self._required_job(connection, job_id))
            self._require_live_lease(job, owner, now)
            connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (lease_expires_at, now_iso, job_id),
            )
            updated_row = self._required_job(connection, job_id)
        return _job_from_row(updated_row)

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str | BaseException,
        retryable: bool = False,
    ) -> JobRecord:
        """Fail a live job, optionally requeueing it within the attempt bound."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.clock())
        now_iso = now.isoformat(timespec="microseconds")
        safe_code = _sanitize_error_code(error_code, fallback="job_failed")
        safe_message = _sanitize_error_message(error_message)
        with self.database.transaction() as connection:
            job = _job_from_row(self._required_job(connection, job_id))
            self._require_live_lease(job, owner, now)
            if retryable and job.attempt < self.max_attempts:
                connection.execute(
                    """
                    UPDATE jobs SET
                        status = ?, stage = ?, progress = 0, lease_owner = NULL,
                        lease_expires_at = NULL, error_code = ?, error_message = ?,
                        updated_at = ?, completed_at = NULL
                    WHERE id = ?
                    """,
                    (
                        JobStatus.QUEUED.value,
                        JobStage.QUEUED.value,
                        safe_code,
                        safe_message,
                        now_iso,
                        job_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs SET
                        status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        error_code = ?, error_message = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        JobStatus.FAILED.value,
                        safe_code,
                        safe_message,
                        now_iso,
                        now_iso,
                        job_id,
                    ),
                )
            updated_row = self._required_job(connection, job_id)
        return _job_from_row(updated_row)

    def cancel_job(self, job_id: str) -> JobRecord:
        """Atomically cancel queued work before any worker can claim it."""

        now_iso = self._now_iso()
        with self.database.transaction() as connection:
            self._required_job(connection, job_id)
            result = connection.execute(
                """
                UPDATE jobs SET
                    status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    now_iso,
                    now_iso,
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if result.rowcount != 1:
                job = _job_from_row(self._required_job(connection, job_id))
                raise InvalidJobTransitionError(
                    f"cannot cancel a job with status {job.status.value}"
                )
            updated_row = self._required_job(connection, job_id)
        return _job_from_row(updated_row)

    def recover_stale_jobs(self) -> LeaseRecoveryResult:
        """Requeue expired leases within bounds and fail exhausted work."""

        now_iso = self._now_iso()
        with self.database.transaction() as connection:
            return self._recover_stale_jobs(connection, now_iso)

    def _recover_stale_jobs(
        self, connection: sqlite3.Connection, now_iso: str
    ) -> LeaseRecoveryResult:
        exhausted_repositories = connection.execute(
            """
            SELECT DISTINCT repository_id FROM jobs
            WHERE status = ?
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
              AND attempt >= ?
            """,
            (JobStatus.RUNNING.value, now_iso, self.max_attempts),
        ).fetchall()
        requeued = connection.execute(
            """
            UPDATE jobs SET
                status = ?, stage = ?, progress = 0, lease_owner = NULL,
                lease_expires_at = NULL, error_code = ?, error_message = ?,
                updated_at = ?, completed_at = NULL
            WHERE status = ?
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
              AND attempt < ?
            """,
            (
                JobStatus.QUEUED.value,
                JobStage.QUEUED.value,
                "lease_expired",
                "The previous worker lease expired; the job was safely requeued.",
                now_iso,
                JobStatus.RUNNING.value,
                now_iso,
                self.max_attempts,
            ),
        ).rowcount
        failed = connection.execute(
            """
            UPDATE jobs SET
                status = ?, lease_owner = NULL, lease_expires_at = NULL,
                error_code = ?, error_message = ?, updated_at = ?, completed_at = ?
            WHERE status = ?
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
              AND attempt >= ?
            """,
            (
                JobStatus.FAILED.value,
                "job_attempts_exhausted",
                "The job could not be completed within the configured attempt limit.",
                now_iso,
                now_iso,
                JobStatus.RUNNING.value,
                now_iso,
                self.max_attempts,
            ),
        ).rowcount
        for row in exhausted_repositories:
            connection.execute(
                """
                UPDATE repositories SET
                    status = ?,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    "failed",
                    "job_attempts_exhausted",
                    "Indexing stopped after every worker attempt expired.",
                    now_iso,
                    row["repository_id"],
                    "queued",
                    "indexing",
                ),
            )
        return LeaseRecoveryResult(requeued=requeued, failed=failed)

    def _required_job(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return cast(sqlite3.Row, row)

    def _require_live_lease(self, job: JobRecord, worker_id: str, now: datetime) -> None:
        if job.status is not JobStatus.RUNNING:
            raise InvalidJobTransitionError(f"job is not running: {job.status.value}")
        if job.lease_owner != worker_id:
            raise JobLeaseError("job lease is owned by another worker")
        if job.lease_expires_at is None or _as_utc(job.lease_expires_at) <= now:
            raise JobLeaseError("job lease has expired")

    def _validate_progress(self, job: JobRecord, stage: JobStage, progress: int) -> None:
        path = _STAGE_PATHS[job.kind]
        if stage is JobStage.COMPLETE:
            raise InvalidJobTransitionError("only succeed_job may mark a job complete")
        if stage not in path:
            raise InvalidJobTransitionError(
                f"stage {stage.value} is not valid for {job.kind.value} jobs"
            )
        if path.index(stage) < path.index(job.stage):
            raise InvalidJobTransitionError("job stage cannot move backwards")
        if progress < job.progress:
            raise InvalidJobTransitionError("job progress cannot move backwards")

    def _now_iso(self) -> str:
        return _as_utc(self.clock()).isoformat(timespec="microseconds")
