from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from codebase_intelligence.database import Database
from codebase_intelligence.job_service import (
    ActiveJobExistsError,
    InvalidJobTransitionError,
    JobService,
)
from codebase_intelligence.lifecycle import LifecycleCoordinator
from codebase_intelligence.models import JobStatus, RepositoryStats, RepositoryStatus, SourceKind
from codebase_intelligence.repository import (
    InvalidRepositoryTransitionError,
    RepositoryStore,
)


def test_concurrent_reindex_commits_exactly_one_active_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "manifest.sqlite3")
    repositories = RepositoryStore(database)
    jobs = JobService(database)
    lifecycle = LifecycleCoordinator(database, repositories, jobs)
    repository = repositories.create_repository(name="concurrent", source_kind=SourceKind.ZIP)
    repositories.update_repository(repository.id, status=RepositoryStatus.INDEXING)
    repositories.mark_repository_ready(
        repository.id,
        commit_sha=None,
        collection_name="collection-v1",
        index_fingerprint="fingerprint-v1",
        stats=RepositoryStats(file_count=1, chunk_count=1),
    )
    barrier = Barrier(2)

    def start() -> str | None:
        barrier.wait()
        try:
            _, job = lifecycle.start_reindex(
                repository_id=repository.id,
                job_id=str(uuid4()),
            )
            return job.id
        except (
            ActiveJobExistsError,
            InvalidJobTransitionError,
            InvalidRepositoryTransitionError,
        ):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))

    accepted = [result for result in results if result is not None]
    active = jobs.list_jobs(repository_id=repository.id)
    current = repositories.get_repository(repository.id)
    assert len(accepted) == 1
    assert len(active) == 1
    assert current is not None and current.status is RepositoryStatus.INDEXING


def test_terminal_processing_failure_commits_job_and_repository_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "manifest.sqlite3")
    repositories = RepositoryStore(database)
    jobs = JobService(database)
    lifecycle = LifecycleCoordinator(database, repositories, jobs)
    repository, queued = lifecycle.create_submission(
        repository_id=str(uuid4()),
        job_id=str(uuid4()),
        name="atomic-failure",
        source_kind=SourceKind.ZIP,
    )
    claimed = jobs.claim_next_job("worker-a")
    assert claimed is not None and claimed.id == queued.id

    failed_repository, failed_job = lifecycle.fail_processing_job(
        repository_id=repository.id,
        job_id=claimed.id,
        worker_id="worker-a",
        error_code="provider_failed",
        error_message="password=not-for-storage",
        retryable=False,
    )

    assert failed_job.status is JobStatus.FAILED
    assert failed_repository.status is RepositoryStatus.FAILED
    assert failed_job.error_message == failed_repository.error_message
    assert failed_job.error_message is not None
    assert "not-for-storage" not in failed_job.error_message


def test_terminal_processing_failure_rejects_cross_repository_pair(tmp_path: Path) -> None:
    database = Database(tmp_path / "manifest.sqlite3")
    repositories = RepositoryStore(database)
    jobs = JobService(database)
    lifecycle = LifecycleCoordinator(database, repositories, jobs)
    repository_a, queued = lifecycle.create_submission(
        repository_id=str(uuid4()),
        job_id=str(uuid4()),
        name="repository-a",
        source_kind=SourceKind.ZIP,
    )
    repository_b = repositories.create_repository(name="repository-b", source_kind=SourceKind.ZIP)
    claimed = jobs.claim_next_job("worker-a")
    assert claimed is not None and claimed.id == queued.id

    with pytest.raises(InvalidJobTransitionError, match="does not belong"):
        lifecycle.fail_processing_job(
            repository_id=repository_b.id,
            job_id=claimed.id,
            worker_id="worker-a",
            error_code="provider_failed",
            error_message="provider failed",
            retryable=False,
        )

    untouched_a = repositories.get_repository(repository_a.id)
    untouched_b = repositories.get_repository(repository_b.id)
    running = jobs.get_job(claimed.id)
    assert untouched_a is not None and untouched_a.status is RepositoryStatus.QUEUED
    assert untouched_b is not None and untouched_b.status is RepositoryStatus.QUEUED
    assert running is not None and running.status is JobStatus.RUNNING
