from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from codebase_intelligence.database import Database
from codebase_intelligence.job_service import (
    ActiveJobExistsError,
    InvalidJobTransitionError,
    JobLeaseError,
    JobNotFoundError,
    JobService,
)
from codebase_intelligence.models import (
    JobKind,
    JobStage,
    JobStatus,
    RepositoryStatus,
    SourceKind,
)
from codebase_intelligence.repository import RepositoryStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _claim_in_process(arguments: tuple[str, str]) -> str | None:
    database_path, worker_id = arguments
    service = JobService(
        Database(Path(database_path)),
        lease_seconds=30,
        max_attempts=2,
        clock=MutableClock(),
    )
    claimed = service.claim_next_job(worker_id)
    return None if claimed is None else claimed.id


@pytest.fixture
def services(tmp_path: Path) -> tuple[Database, RepositoryStore, JobService, MutableClock]:
    database = Database(tmp_path / "manifest.sqlite3")
    database.initialize()
    repositories = RepositoryStore(database)
    clock = MutableClock()
    jobs = JobService(database, lease_seconds=30, max_attempts=2, clock=clock)
    return database, repositories, jobs, clock


def test_enqueue_get_list_and_payload_round_trip(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, _ = services
    repository = repositories.create_repository(name="auth", source_kind=SourceKind.ZIP)

    job = jobs.enqueue_job(
        repository.id,
        JobKind.INGEST,
        payload={"archive": "staging/repo.zip", "limits": {"files": 100}},
    )

    assert len(job.id) == 36
    assert job.status is JobStatus.QUEUED
    assert job.payload["limits"] == {"files": 100}
    assert jobs.get_job(job.id) == job
    assert jobs.list_jobs(repository_id=repository.id) == [job]


def test_only_one_worker_can_atomically_claim_a_job(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    database, repositories, jobs, clock = services
    repository = repositories.create_repository(name="billing", source_kind=SourceKind.ZIP)
    queued = jobs.enqueue_job(repository.id, JobKind.INGEST, payload={"kind": "zip"})
    barrier = Barrier(2)

    def claim(worker_id: str) -> str | None:
        service = JobService(database, lease_seconds=30, max_attempts=2, clock=clock)
        barrier.wait()
        claimed = service.claim_next_job(worker_id)
        return None if claimed is None else claimed.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-a", "worker-b")))

    assert claims.count(queued.id) == 1
    assert claims.count(None) == 1
    assert jobs.get_job(queued.id).attempt == 1  # type: ignore[union-attr]


def test_claim_transaction_is_safe_across_processes(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    database, repositories, jobs, _ = services
    repository = repositories.create_repository(name="multiprocess", source_kind=SourceKind.ZIP)
    queued = jobs.enqueue_job(repository.id, JobKind.INGEST)

    with ProcessPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                _claim_in_process,
                ((str(database.path), "process-a"), (str(database.path), "process-b")),
            )
        )

    assert claims.count(queued.id) == 1
    assert claims.count(None) == 1


def test_progress_is_monotonic_and_worker_owned(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, _ = services
    repository = repositories.create_repository(name="checkout", source_kind=SourceKind.ZIP)
    queued = jobs.enqueue_job(repository.id, JobKind.INGEST)
    claimed = jobs.claim_next_job("worker-a")
    assert claimed is not None and claimed.id == queued.id

    progressed = jobs.update_progress(
        queued.id,
        "worker-a",
        stage=JobStage.SCANNING,
        progress=35,
    )
    assert progressed.stage is JobStage.SCANNING
    assert progressed.progress == 35

    with pytest.raises(InvalidJobTransitionError):
        jobs.update_progress(
            queued.id,
            "worker-a",
            stage=JobStage.FETCHING,
            progress=40,
        )
    with pytest.raises(InvalidJobTransitionError):
        jobs.update_progress(
            queued.id,
            "worker-a",
            stage=JobStage.PARSING,
            progress=34,
        )
    with pytest.raises(JobLeaseError):
        jobs.update_progress(
            queued.id,
            "worker-b",
            stage=JobStage.PARSING,
            progress=50,
        )


def test_stale_lease_is_requeued_then_exhausted(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, clock = services
    repository = repositories.create_repository(name="retry", source_kind=SourceKind.ZIP)
    queued = jobs.enqueue_job(repository.id, JobKind.INGEST)

    first = jobs.claim_next_job("worker-a")
    assert first is not None and first.attempt == 1
    clock.advance(seconds=31)

    second = jobs.claim_next_job("worker-b")
    assert second is not None and second.id == queued.id
    assert second.attempt == 2
    assert second.lease_owner == "worker-b"
    clock.advance(seconds=31)

    recovery = jobs.recover_stale_jobs()
    exhausted = jobs.get_job(queued.id)
    assert recovery.requeued == 0
    assert recovery.failed == 1
    assert exhausted is not None
    assert exhausted.status is JobStatus.FAILED
    assert exhausted.error_code == "job_attempts_exhausted"
    assert exhausted.lease_owner is None
    failed_repository = repositories.get_repository(repository.id)
    assert failed_repository is not None
    assert failed_repository.status.value == "failed"
    assert failed_repository.error_code == "job_attempts_exhausted"
    assert jobs.claim_next_job("worker-c") is None


def test_exhausted_reindex_never_trusts_collection_name_without_readback(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, clock = services
    repository = repositories.create_repository(name="stale-index", source_kind=SourceKind.ZIP)
    repositories.update_repository(repository.id, status=RepositoryStatus.INDEXING)
    repositories.mark_repository_ready(
        repository.id,
        commit_sha=None,
        collection_name="possibly-missing-collection",
        index_fingerprint="fingerprint",
        stats={},
    )
    repositories.update_repository(repository.id, status=RepositoryStatus.INDEXING)
    queued = jobs.enqueue_job(repository.id, JobKind.REINDEX)

    first = jobs.claim_next_job("worker-a")
    assert first is not None and first.id == queued.id
    clock.advance(seconds=31)
    second = jobs.claim_next_job("worker-b")
    assert second is not None and second.attempt == 2
    clock.advance(seconds=31)
    jobs.recover_stale_jobs()

    failed_repository = repositories.get_repository(repository.id)
    assert failed_repository is not None
    assert failed_repository.status.value == "failed"
    assert failed_repository.collection_name == "possibly-missing-collection"


def test_lease_renewal_does_not_replay_progress_and_active_job_is_unique(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, clock = services
    repository = repositories.create_repository(name="lease", source_kind=SourceKind.ZIP)
    queued = jobs.enqueue_job(repository.id, JobKind.INGEST)
    with pytest.raises(ActiveJobExistsError):
        jobs.enqueue_job(repository.id, JobKind.REINDEX)

    claimed = jobs.claim_next_job("worker-a")
    assert claimed is not None and claimed.id == queued.id
    progressed = jobs.update_progress(
        queued.id,
        "worker-a",
        stage=JobStage.PARSING,
        progress=40,
    )
    original_expiry = progressed.lease_expires_at
    clock.advance(seconds=10)
    renewed = jobs.renew_lease(queued.id, "worker-a")

    assert renewed.stage is JobStage.PARSING
    assert renewed.progress == 40
    assert renewed.lease_expires_at is not None
    assert original_expiry is not None and renewed.lease_expires_at > original_expiry


def test_succeed_fail_retry_and_cancel_lifecycle(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, _ = services
    repository = repositories.create_repository(name="lifecycle", source_kind=SourceKind.ZIP)

    success_job = jobs.enqueue_job(repository.id, JobKind.INGEST)
    assert jobs.claim_next_job("worker-a") is not None
    succeeded = jobs.succeed_job(success_job.id, "worker-a")
    assert succeeded.status is JobStatus.SUCCEEDED
    assert succeeded.stage is JobStage.COMPLETE
    assert succeeded.progress == 100
    assert succeeded.completed_at is not None

    retry_job = jobs.enqueue_job(repository.id, JobKind.REINDEX)
    assert jobs.claim_next_job("worker-a") is not None
    retried = jobs.fail_job(
        retry_job.id,
        "worker-a",
        error_code="transient/provider",
        error_message="Bearer top-secret-token timed out",
        retryable=True,
    )
    assert retried.status is JobStatus.QUEUED
    assert retried.progress == 0
    assert retried.error_code == "transient_provider"
    assert retried.error_message is not None
    assert "top-secret-token" not in retried.error_message

    terminal = jobs.claim_next_job("worker-b")
    assert terminal is not None and terminal.id == retry_job.id
    failed = jobs.fail_job(
        retry_job.id,
        "worker-b",
        error_code="provider_failed",
        error_message="password=hunter2",
    )
    assert failed.status is JobStatus.FAILED
    assert failed.error_message is not None and "hunter2" not in failed.error_message

    cancelled_job = jobs.enqueue_job(repository.id, JobKind.DELETE)
    cancelled = jobs.cancel_job(cancelled_job.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.completed_at is not None

    with pytest.raises(InvalidJobTransitionError):
        jobs.cancel_job(success_job.id)


def test_delete_repository_cascades_jobs_and_missing_job_is_explicit(
    services: tuple[Database, RepositoryStore, JobService, MutableClock],
) -> None:
    _, repositories, jobs, _ = services
    repository = repositories.create_repository(name="cascade", source_kind=SourceKind.ZIP)
    job = jobs.enqueue_job(repository.id, JobKind.DELETE)

    assert repositories.delete_repository(repository.id) is True
    assert jobs.get_job(job.id) is None
    with pytest.raises(JobNotFoundError):
        jobs.cancel_job(job.id)
