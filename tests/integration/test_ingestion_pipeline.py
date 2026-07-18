from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.database import Database
from codebase_intelligence.ingestion.pipeline import IngestionService, uploaded_bytes
from codebase_intelligence.job_service import JobService
from codebase_intelligence.models import (
    CodeChunk,
    JobKind,
    JobStatus,
    QuestionRequest,
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
)
from codebase_intelligence.rag_service import RAGService
from codebase_intelligence.repository import RepositoryStore
from codebase_intelligence.vector_store import CodeVectorIndex
from codebase_intelligence.worker import JobWorker

pytestmark = pytest.mark.integration


def _settings(data_dir: Path) -> Settings:
    return Settings(
        environment="test",
        data_dir=data_dir,
        embedding_provider="deterministic",
        deterministic_embedding_dimension=128,
        answer_provider="extractive",
        inline_worker=False,
        worker_poll_seconds=0.1,
        worker_lease_seconds=30,
        max_archive_bytes=1024 * 1024,
        max_extracted_bytes=4 * 1024 * 1024,
        max_indexable_bytes=4 * 1024 * 1024,
        max_file_bytes=1024 * 1024,
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return buffer.getvalue()


def _sample_archive() -> bytes:
    return _zip_bytes(
        {
            "sample-main/src/auth.py": (
                "def authenticate_request(bearer_token: str) -> bool:\n"
                '    """Validate the caller\'s bearer token."""\n'
                "    return bearer_token.startswith('valid-')\n"
            ),
            "sample-main/src/payments.py": (
                "def capture_payment(amount_cents: int, gateway) -> str:\n"
                '    """Capture a checkout payment through the gateway."""\n'
                "    return gateway.capture(amount_cents)\n"
            ),
            "sample-main/README.md": "# Sample service\nAuthentication and payment flow demo.\n",
        }
    )


def _chunk(repository_id: str, identifier: str, text: str) -> CodeChunk:
    return CodeChunk(
        id=identifier,
        repository_id=repository_id,
        path=f"src/{identifier}.py",
        language="python",
        symbol=identifier,
        symbol_kind="function",
        start_line=1,
        end_line=1,
        parser="tree_sitter",
        text=text,
        content_hash=identifier.rjust(64, "0"),
    )


@pytest.mark.asyncio
async def test_zip_job_survives_service_reconstruction_and_completes_full_lifecycle(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repositories = RepositoryStore(database)
    submitting_jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, submitting_jobs, vector_index)

    try:
        async with uploaded_bytes(_sample_archive(), "sample.zip") as upload:
            submitted = await ingestion.submit_upload(upload, name="sample-service")

        assert submitted.status is RepositoryStatus.QUEUED
        archive_path = settings.repositories_dir / submitted.repository_id / "archive.zip"
        assert archive_path.is_file()

        # A fresh queue service can read and claim the persisted job, proving the
        # submission is not dependent on in-memory task state.
        durable_jobs = JobService(
            Database(settings.database_path),
            lease_seconds=30,
            max_attempts=2,
        )
        persisted = durable_jobs.get_job(submitted.job_id)
        assert persisted is not None
        assert persisted.status is JobStatus.QUEUED

        worker = JobWorker(
            durable_jobs,
            ingestion,
            poll_seconds=0.1,
            lease_seconds=30,
            worker_id="integration-worker",
        )
        assert await worker.run_once() is True

        completed = durable_jobs.get_job(submitted.job_id)
        ready = repositories.get_repository(submitted.repository_id)
        assert completed is not None and completed.status is JobStatus.SUCCEEDED
        assert ready is not None and ready.status is RepositoryStatus.READY
        assert ready.stats.file_count == 3
        assert ready.stats.chunk_count >= 2
        assert ready.collection_name is not None
        assert ready.collection_name.startswith(vector_index.collection_name(ready.id))
        assert vector_index.has_collection(ready.id, collection_name=ready.collection_name)

        rag = RAGService(settings, repositories, vector_index, completion_provider=None)
        answer = await rag.ask(
            ready.id,
            QuestionRequest(question="Where is the authentication bearer token logic?", top_k=8),
        )
        assert answer.answer_mode == "extractive"
        assert answer.citations
        assert any(citation.path == "src/auth.py" for citation in answer.citations)
        assert all(citation.repository_id == ready.id for citation in answer.citations)
        assert "[S" in answer.answer

        previous_collection = ready.collection_name
        settings.qdrant_collection_prefix = "changed_prefix"
        reindex = ingestion.enqueue_reindex(ready.id)
        assert reindex.status is RepositoryStatus.INDEXING
        assert await worker.run_once() is True
        reindex_job = durable_jobs.get_job(reindex.job_id)
        reindexed = repositories.get_repository(ready.id)
        assert reindex_job is not None and reindex_job.status is JobStatus.SUCCEEDED
        assert reindexed is not None and reindexed.status is RepositoryStatus.READY
        assert reindexed.stats.chunk_count == ready.stats.chunk_count
        assert reindexed.collection_name != previous_collection
        assert reindexed.collection_name.startswith("changed_prefix_")
        assert previous_collection is not None
        assert not vector_index.has_collection(
            ready.id,
            collection_name=previous_collection,
        )

        ingestion.delete_repository(ready.id)
        assert repositories.get_repository(ready.id) is None
        assert durable_jobs.get_job(submitted.job_id) is None
        assert durable_jobs.get_job(reindex.job_id) is None
        assert not vector_index.repository_collections(ready.id)
        assert not (settings.repositories_dir / ready.id).exists()
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_stale_worker_cannot_mutate_repository_after_lease_reassignment(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    database = Database(settings.database_path)
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)
    try:
        async with uploaded_bytes(_sample_archive(), "sample.zip") as upload:
            submitted = await ingestion.submit_upload(upload)
        claimed = jobs.claim_next_job("stale-worker")
        assert claimed is not None
        with database.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET lease_owner = ? WHERE id = ?",
                ("replacement-worker", claimed.id),
            )

        ingestion.process_job(claimed, "stale-worker")

        untouched = repositories.get_repository(submitted.repository_id)
        reassigned = jobs.get_job(claimed.id)
        assert untouched is not None and untouched.status is RepositoryStatus.QUEUED
        assert reassigned is not None and reassigned.status is JobStatus.RUNNING
        assert reassigned.lease_owner == "replacement-worker"
        assert not vector_index.repository_collections(submitted.repository_id)
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_delete_requires_explicit_collection_absence_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "runtime")
    database = Database(settings.database_path)
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)
    worker = JobWorker(
        jobs,
        ingestion,
        poll_seconds=0.1,
        lease_seconds=30,
        worker_id="delete-readback-worker",
    )
    try:
        async with uploaded_bytes(_sample_archive(), "sample.zip") as upload:
            submitted = await ingestion.submit_upload(upload)
        assert await worker.run_once() is True
        ready = repositories.get_repository(submitted.repository_id)
        assert ready is not None and ready.collection_name is not None
        repository_dir = settings.repositories_dir / ready.id

        monkeypatch.setattr(vector_index, "delete", lambda *_args, **_kwargs: False)
        with pytest.raises(RuntimeError, match="vector deletion"):
            ingestion.delete_repository(ready.id)

        deleting = repositories.get_repository(ready.id)
        assert deleting is not None and deleting.status is RepositoryStatus.DELETING
        assert vector_index.has_collection(ready.id, collection_name=ready.collection_name)
        assert repository_dir.exists()
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_failed_reindex_preserves_last_good_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "runtime")
    database = Database(settings.database_path)
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)
    worker = JobWorker(
        jobs,
        ingestion,
        poll_seconds=0.1,
        lease_seconds=30,
        worker_id="reindex-worker",
    )
    try:
        async with uploaded_bytes(_sample_archive(), "sample.zip") as upload:
            submitted = await ingestion.submit_upload(upload)
        assert await worker.run_once() is True
        ready = repositories.get_repository(submitted.repository_id)
        assert ready is not None and ready.collection_name is not None
        last_good_collection = ready.collection_name

        def fail_index(repository_id: str, chunks: object) -> str:
            raise RuntimeError("temporary embedding failure")

        monkeypatch.setattr(vector_index, "index", fail_index)
        reindex = ingestion.enqueue_reindex(ready.id)
        assert await worker.run_once() is True
        retried_job = jobs.get_job(reindex.job_id)
        assert retried_job is not None and retried_job.status is JobStatus.QUEUED
        assert await worker.run_once() is True

        failed_job = jobs.get_job(reindex.job_id)
        restored = repositories.get_repository(ready.id)
        assert failed_job is not None and failed_job.status is JobStatus.FAILED
        assert restored is not None and restored.status is RepositoryStatus.READY
        assert restored.collection_name == last_good_collection
        assert vector_index.has_collection(
            ready.id,
            collection_name=last_good_collection,
        )

        assert vector_index.delete(
            ready.id,
            collection_name=last_good_collection,
        )
        missing_reindex = ingestion.enqueue_reindex(ready.id)
        assert await worker.run_once() is True
        assert await worker.run_once() is True
        missing_failed_job = jobs.get_job(missing_reindex.job_id)
        missing_failed_repository = repositories.get_repository(ready.id)
        assert missing_failed_job is not None
        assert missing_failed_job.status is JobStatus.FAILED
        assert missing_failed_repository is not None
        assert missing_failed_repository.status is RepositoryStatus.FAILED
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_non_indexable_archive_fails_job_without_creating_vector_collection(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)

    try:
        archive = _zip_bytes({"empty-main/.env": "PASSWORD=must-not-appear-in-errors\n"})
        async with uploaded_bytes(archive, "empty.zip") as upload:
            submitted = await ingestion.submit_upload(upload)

        worker = JobWorker(
            jobs,
            ingestion,
            poll_seconds=0.1,
            lease_seconds=30,
            worker_id="failure-worker",
        )
        assert await worker.run_once() is True

        failed_job = jobs.get_job(submitted.job_id)
        failed_repository = repositories.get_repository(submitted.repository_id)
        assert failed_job is not None and failed_job.status is JobStatus.FAILED
        assert failed_job.error_code == "scanerror"
        assert failed_repository is not None
        assert failed_repository.status is RepositoryStatus.FAILED
        assert failed_repository.error_message is not None
        assert "must-not-appear-in-errors" not in failed_repository.error_message
        assert not vector_index.repository_collections(submitted.repository_id)
    finally:
        vector_index.close()


def test_reconcile_startup_requeues_orphaned_manifests_without_duplicating_active_jobs(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)

    try:
        queued = repositories.create_repository(name="queued", source_kind=SourceKind.ZIP)
        queued_dir = settings.repositories_dir / queued.id
        queued_dir.mkdir()
        (queued_dir / "archive.zip").write_bytes(b"durable snapshot")

        indexing = repositories.create_repository(name="indexing", source_kind=SourceKind.ZIP)
        repositories.update_repository(indexing.id, status=RepositoryStatus.INDEXING)
        (settings.repositories_dir / indexing.id / "source").mkdir(parents=True)

        active = repositories.create_repository(name="active", source_kind=SourceKind.ZIP)
        active_dir = settings.repositories_dir / active.id
        active_dir.mkdir()
        (active_dir / "archive.zip").write_bytes(b"durable snapshot")
        active_job = jobs.enqueue_job(active.id, JobKind.INGEST)

        counts = ingestion.reconcile_startup()

        queued_jobs = jobs.list_jobs(repository_id=queued.id)
        indexing_jobs = jobs.list_jobs(repository_id=indexing.id)
        active_jobs = jobs.list_jobs(repository_id=active.id)
        assert counts == {"jobs_requeued": 2, "repositories_failed": 0, "paths_removed": 0}
        assert len(queued_jobs) == 1 and queued_jobs[0].kind is JobKind.INGEST
        assert len(indexing_jobs) == 1 and indexing_jobs[0].kind is JobKind.REINDEX
        assert active_jobs == [active_job]

        # Reconciliation is idempotent while those durable jobs remain active.
        assert ingestion.reconcile_startup() == {
            "jobs_requeued": 0,
            "repositories_failed": 0,
            "paths_removed": 0,
        }
        assert len(jobs.list_jobs(repository_id=queued.id)) == 1
        assert len(jobs.list_jobs(repository_id=indexing.id)) == 1
    finally:
        vector_index.close()


def test_reconcile_startup_fails_queued_and_indexing_manifests_without_snapshots(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)

    try:
        queued = repositories.create_repository(name="queued-missing", source_kind=SourceKind.ZIP)
        indexing = repositories.create_repository(
            name="indexing-missing", source_kind=SourceKind.ZIP
        )
        repositories.update_repository(indexing.id, status=RepositoryStatus.INDEXING)
        ready = repositories.create_repository(name="ready-missing", source_kind=SourceKind.ZIP)
        repositories.update_repository(ready.id, status=RepositoryStatus.INDEXING)
        repositories.mark_repository_ready(
            ready.id,
            commit_sha=None,
            collection_name="missing-physical-collection",
            index_fingerprint="fingerprint",
            stats=RepositoryStats(file_count=1, chunk_count=1),
        )

        counts = ingestion.reconcile_startup()

        assert counts == {"jobs_requeued": 0, "repositories_failed": 3, "paths_removed": 0}
        for repository_id in (queued.id, indexing.id):
            failed = repositories.get_repository(repository_id)
            assert failed is not None and failed.status is RepositoryStatus.FAILED
            assert failed.error_code == "snapshot_missing"
            assert failed.error_message == "The immutable repository snapshot is unavailable."
            assert jobs.list_jobs(repository_id=repository_id) == []
        missing_index = repositories.get_repository(ready.id)
        assert missing_index is not None and missing_index.status is RepositoryStatus.FAILED
        assert missing_index.error_code == "index_missing"
        assert jobs.list_jobs(repository_id=ready.id) == []
    finally:
        vector_index.close()


def test_reconcile_startup_prunes_only_stale_orphans_and_inactive_collections(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "runtime")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)

    try:
        ready = repositories.create_repository(name="ready", source_kind=SourceKind.ZIP)
        repositories.update_repository(ready.id, status=RepositoryStatus.INDEXING)
        inactive_collection = vector_index.index(
            ready.id,
            [_chunk(ready.id, "stale", "def stale_authentication(): pass")],
        )
        active_collection = vector_index.index(
            ready.id,
            [_chunk(ready.id, "active", "def active_authentication(): pass")],
        )
        repositories.mark_repository_ready(
            ready.id,
            commit_sha=None,
            collection_name=active_collection,
            index_fingerprint="test-fingerprint",
            stats=RepositoryStats(file_count=1, chunk_count=1),
        )
        manifest_path = settings.repositories_dir / ready.id
        manifest_path.mkdir()
        (manifest_path / "archive.zip").write_bytes(b"retained manifest snapshot")

        stale_staging = settings.staging_dir / "stale.zip"
        stale_staging.write_bytes(b"stale")
        fresh_staging = settings.staging_dir / "fresh.zip"
        fresh_staging.write_bytes(b"fresh")
        stale_orphan = settings.repositories_dir / "unreferenced-stale"
        stale_orphan.mkdir()
        (stale_orphan / "archive.zip").write_bytes(b"orphan")
        fresh_orphan = settings.repositories_dir / "unreferenced-fresh"
        fresh_orphan.mkdir()

        old_timestamp = time.time() - 7200
        for path in (stale_staging, stale_orphan, manifest_path):
            os.utime(path, (old_timestamp, old_timestamp))

        counts = ingestion.reconcile_startup()

        assert counts == {"jobs_requeued": 0, "repositories_failed": 0, "paths_removed": 2}
        assert not stale_staging.exists()
        assert not stale_orphan.exists()
        assert fresh_staging.exists()
        assert fresh_orphan.exists()
        assert manifest_path.exists()
        assert vector_index.has_collection(ready.id, collection_name=active_collection)
        assert not vector_index.has_collection(ready.id, collection_name=inactive_collection)
        assert vector_index.repository_collections(ready.id) == [active_collection]
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_worker_forever_loop_starts_idles_and_stops_cooperatively(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "runtime")
    database = Database(settings.database_path)
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)
    worker = JobWorker(
        jobs,
        ingestion,
        poll_seconds=0.01,
        lease_seconds=30,
        worker_id="idle-worker",
    )
    try:
        task = asyncio.create_task(worker.run_forever())
        await asyncio.sleep(0.03)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)
        assert await worker.run_once() is False
    finally:
        vector_index.close()


@pytest.mark.asyncio
async def test_worker_forever_contains_one_iteration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "runtime")
    database = Database(settings.database_path)
    repositories = RepositoryStore(database)
    jobs = JobService(database, lease_seconds=30, max_attempts=2)
    vector_index = CodeVectorIndex(settings)
    ingestion = IngestionService(settings, repositories, jobs, vector_index)
    worker = JobWorker(
        jobs,
        ingestion,
        poll_seconds=0.01,
        lease_seconds=30,
        worker_id="resilient-worker",
    )
    calls = 0

    async def flaky_run_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient queue failure")
        worker.stop()
        return False

    monkeypatch.setattr(worker, "run_once", flaky_run_once)
    try:
        await asyncio.wait_for(worker.run_forever(), timeout=1)
        assert calls == 2
    finally:
        vector_index.close()
