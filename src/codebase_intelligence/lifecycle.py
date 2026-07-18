"""Atomic repository/job state transitions spanning the two durable records."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from codebase_intelligence.database import Database
from codebase_intelligence.job_service import (
    ActiveJobExistsError,
    InvalidJobTransitionError,
    JobLeaseError,
    JobNotFoundError,
    JobService,
    _as_utc,
    _job_from_row,
    _validate_worker_id,
)
from codebase_intelligence.models import (
    JobKind,
    JobRecord,
    JobStage,
    JobStatus,
    RepositoryRecord,
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
    utc_now,
)
from codebase_intelligence.repository import (
    InvalidRepositoryTransitionError,
    RepositoryNotFoundError,
    RepositoryStore,
    _dump_json,
    _repository_from_row,
    _sanitize_error_code,
    _sanitize_error_message,
    _validate_transition,
    _validated_name,
)


class LifecycleCoordinator:
    """Commit related manifest and queue mutations in one SQLite transaction."""

    def __init__(
        self,
        database: Database,
        repositories: RepositoryStore,
        jobs: JobService,
    ) -> None:
        self.database = database
        self.repositories = repositories
        self.jobs = jobs

    def create_submission(
        self,
        *,
        repository_id: str,
        job_id: str,
        name: str,
        source_kind: SourceKind,
        source_url: str | None = None,
        source_ref: str | None = None,
        commit_sha: str | None = None,
    ) -> tuple[RepositoryRecord, JobRecord]:
        """Insert one repository and its ingest job atomically after bytes are durable."""

        now = utc_now().isoformat(timespec="microseconds")
        stats_json = _dump_json(RepositoryStats().model_dump(mode="json"))
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO repositories (
                        id, name, status, source_kind, source_url, source_ref,
                        commit_sha, stats_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository_id,
                        _validated_name(name),
                        RepositoryStatus.QUEUED.value,
                        SourceKind(source_kind).value,
                        source_url,
                        source_ref,
                        commit_sha,
                        stats_json,
                        now,
                        now,
                    ),
                )
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
                        JobKind.INGEST.value,
                        JobStatus.QUEUED.value,
                        JobStage.QUEUED.value,
                        0,
                        0,
                        "{}",
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ActiveJobExistsError("repository submission could not be committed") from error
        repository = self.repositories.get_repository(repository_id)
        job = self.jobs.get_job(job_id)
        if repository is None or job is None:
            raise RuntimeError("Repository submission commit could not be read back.")
        return repository, job

    def start_reindex(
        self,
        *,
        repository_id: str,
        job_id: str,
    ) -> tuple[RepositoryRecord, JobRecord]:
        """Move a repository to indexing and insert exactly one active job atomically."""

        now = utc_now().isoformat(timespec="microseconds")
        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM repositories WHERE id = ?",
                    (repository_id,),
                ).fetchone()
                if row is None:
                    raise RepositoryNotFoundError(repository_id)
                repository = _repository_from_row(row)
                if repository.status not in {
                    RepositoryStatus.READY,
                    RepositoryStatus.FAILED,
                }:
                    raise InvalidRepositoryTransitionError("repository work is already in progress")
                _validate_transition(repository.status, RepositoryStatus.INDEXING)
                connection.execute(
                    """
                    UPDATE repositories SET
                        status = ?, error_code = NULL, error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (RepositoryStatus.INDEXING.value, now, repository_id),
                )
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
                        JobKind.REINDEX.value,
                        JobStatus.QUEUED.value,
                        JobStage.QUEUED.value,
                        0,
                        0,
                        "{}",
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ActiveJobExistsError("repository already has queued or running work") from error
        committed_repository = self.repositories.get_repository(repository_id)
        committed_job = self.jobs.get_job(job_id)
        if committed_repository is None or committed_job is None:
            raise RuntimeError("Reindex commit could not be read back.")
        return committed_repository, committed_job

    def complete_index(
        self,
        *,
        repository_id: str,
        job_id: str,
        worker_id: str,
        commit_sha: str | None,
        collection_name: str,
        index_fingerprint: str,
        stats: RepositoryStats | Mapping[str, Any],
    ) -> tuple[RepositoryRecord, JobRecord]:
        """Publish the new collection and succeed its live job in one transaction."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.jobs.clock())
        now_iso = now.isoformat(timespec="microseconds")
        stats_model = RepositoryStats.model_validate(stats)
        with self.database.transaction() as connection:
            repository_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            if repository_row is None:
                raise RepositoryNotFoundError(repository_id)
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise JobNotFoundError(job_id)
            repository = _repository_from_row(repository_row)
            job = _job_from_row(job_row)
            if job.repository_id != repository_id:
                raise InvalidJobTransitionError("job does not belong to the repository")
            _validate_transition(repository.status, RepositoryStatus.READY)
            if job.status is not JobStatus.RUNNING:
                raise InvalidJobTransitionError(f"job is not running: {job.status.value}")
            if job.lease_owner != owner:
                raise JobLeaseError("job lease is owned by another worker")
            if job.lease_expires_at is None or _as_utc(job.lease_expires_at) <= now:
                raise JobLeaseError("job lease has expired")
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
                    now_iso,
                    repository_id,
                ),
            )
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
        completed_repository = self.repositories.get_repository(repository_id)
        completed_job = self.jobs.get_job(job_id)
        if completed_repository is None or completed_job is None:
            raise RuntimeError("Index completion commit could not be read back.")
        return completed_repository, completed_job

    def fail_processing_job(
        self,
        *,
        repository_id: str,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str | BaseException,
        retryable: bool,
        restore_collection: str | None = None,
    ) -> tuple[RepositoryRecord, JobRecord]:
        """Fail or requeue live work and reconcile its repository atomically."""

        owner = _validate_worker_id(worker_id)
        now = _as_utc(self.jobs.clock())
        now_iso = now.isoformat(timespec="microseconds")
        safe_code = _sanitize_error_code(error_code, fallback="job_failed")
        safe_message = _sanitize_error_message(error_message)
        with self.database.transaction() as connection:
            repository_row = connection.execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id,),
            ).fetchone()
            if repository_row is None:
                raise RepositoryNotFoundError(repository_id)
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise JobNotFoundError(job_id)
            repository = _repository_from_row(repository_row)
            job = _job_from_row(job_row)
            if job.repository_id != repository_id:
                raise InvalidJobTransitionError("job does not belong to the repository")
            self.jobs._require_live_lease(job, owner, now)
            should_retry = retryable and job.attempt < self.jobs.max_attempts
            if should_retry:
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
                if repository.status is not RepositoryStatus.DELETING:
                    if job.kind is JobKind.REINDEX and restore_collection is not None:
                        connection.execute(
                            """
                            UPDATE repositories SET
                                status = ?, collection_name = ?, error_code = NULL,
                                error_message = NULL, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                RepositoryStatus.READY.value,
                                restore_collection,
                                now_iso,
                                repository_id,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE repositories SET
                                status = ?, error_code = ?, error_message = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                RepositoryStatus.FAILED.value,
                                safe_code,
                                safe_message,
                                now_iso,
                                repository_id,
                            ),
                        )
        completed_repository = self.repositories.get_repository(repository_id)
        completed_job = self.jobs.get_job(job_id)
        if completed_repository is None or completed_job is None:
            raise RuntimeError("Index failure commit could not be read back.")
        return completed_repository, completed_job


__all__ = ["LifecycleCoordinator"]
