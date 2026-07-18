"""Application-level repository submission and indexing orchestration."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from filelock import FileLock, Timeout

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import (
    IngestionError as APIIngestionError,
)
from codebase_intelligence.exceptions import (
    ResourceConflictError,
    ResourceNotFoundError,
)
from codebase_intelligence.ingestion.chunker import ChunkingError, ChunkLimitError, CodeChunker
from codebase_intelligence.ingestion.file_filter import (
    RepositoryScanner,
    ScanError,
    ScanLimitError,
)
from codebase_intelligence.ingestion.redaction import SecretRedactionError
from codebase_intelligence.ingestion.source_loader import (
    ArchiveLimitError,
    GitHubRepository,
    GitHubRequestError,
    GitHubSourceLoader,
    InvalidSourceError,
    SafeArchiveExtractor,
    UnsafeArchiveError,
)
from codebase_intelligence.ingestion.source_loader import (
    IngestionError as SourceIngestionError,
)
from codebase_intelligence.job_service import (
    ActiveJobExistsError,
    InvalidJobTransitionError,
    JobLeaseError,
    JobNotFoundError,
    JobService,
)
from codebase_intelligence.lifecycle import LifecycleCoordinator
from codebase_intelligence.models import (
    CodeChunk,
    GitHubRepositoryRequest,
    JobKind,
    JobRecord,
    JobStage,
    JobStatus,
    RepositoryCreateResponse,
    RepositoryRecord,
    RepositoryStats,
    RepositoryStatus,
    SourceKind,
)
from codebase_intelligence.observability import get_logger
from codebase_intelligence.providers import index_fingerprint
from codebase_intelligence.repository import (
    InvalidRepositoryTransitionError,
    RepositoryNotFoundError,
    RepositoryStore,
)
from codebase_intelligence.security import (
    UnsafeFilenameError,
    safe_error_message,
    validate_safe_filename,
)
from codebase_intelligence.vector_store import CodeVectorIndex

logger = get_logger(__name__)


class AsyncUpload(Protocol):
    """The bounded part of FastAPI's UploadFile contract used by this service."""

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


_NON_RETRYABLE_ERRORS = (
    ArchiveLimitError,
    ChunkLimitError,
    ChunkingError,
    InvalidSourceError,
    ScanError,
    ScanLimitError,
    SecretRedactionError,
    UnsafeArchiveError,
    ValueError,
)


class IngestionService:
    """Accept untrusted sources and turn durable jobs into isolated vector indexes."""

    def __init__(
        self,
        settings: Settings,
        repositories: RepositoryStore,
        jobs: JobService,
        vector_index: CodeVectorIndex,
        *,
        source_loader: GitHubSourceLoader | None = None,
        extractor: SafeArchiveExtractor | None = None,
        scanner: RepositoryScanner | None = None,
        chunker: CodeChunker | None = None,
    ) -> None:
        self.settings = settings
        self.repositories = repositories
        self.jobs = jobs
        self.vector_index = vector_index
        self.lifecycle = LifecycleCoordinator(jobs.database, repositories, jobs)
        self.source_loader = source_loader or GitHubSourceLoader(settings)
        self.extractor = extractor or SafeArchiveExtractor(settings)
        self.scanner = scanner or RepositoryScanner(settings)
        self.chunker = chunker or CodeChunker(settings)
        self._locks_dir = settings.data_dir / "locks"
        self._locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _repository_dir(self, repository_id: str) -> Path:
        try:
            canonical_id = str(UUID(repository_id))
        except ValueError as error:
            raise ResourceNotFoundError("Repository") from error
        return self.settings.repositories_dir / canonical_id

    def _operation_lock(self, repository_id: str, *, timeout: float = 0) -> FileLock:
        canonical_id = self._repository_dir(repository_id).name
        return FileLock(str(self._locks_dir / f"{canonical_id}.lock"), timeout=timeout)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)

    def _new_staging_path(self) -> Path:
        self.settings.staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.settings.staging_dir / f"{uuid4()}.zip"

    @staticmethod
    def _validated_name(name: str) -> str:
        normalized = " ".join(name.split())
        if (
            not normalized
            or len(normalized) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            raise APIIngestionError(
                "INVALID_REPOSITORY_NAME",
                "Repository name must contain between 1 and 100 printable characters.",
            )
        return normalized

    async def submit_github(
        self,
        request: GitHubRepositoryRequest,
        *,
        token: str | None = None,
    ) -> RepositoryCreateResponse:
        """Resolve and download GitHub bytes before persisting a token-free job."""

        try:
            identity = GitHubRepository.parse(request.url)
            repository_name = self._validated_name(request.name or identity.name)
            staging_path = self._new_staging_path()
            download = await self.source_loader.download(
                identity.canonical_url,
                request.ref,
                staging_path,
                token=token,
            )
        except InvalidSourceError as error:
            raise APIIngestionError("INVALID_GITHUB_SOURCE", str(error)) from error
        except ArchiveLimitError as error:
            raise APIIngestionError("ARCHIVE_TOO_LARGE", str(error), status_code=413) from error
        except GitHubRequestError as error:
            raise APIIngestionError("GITHUB_REQUEST_FAILED", str(error), status_code=502) from error
        except SourceIngestionError as error:
            raise APIIngestionError("SOURCE_DOWNLOAD_FAILED", str(error)) from error

        repository_id = str(uuid4())
        job_id = str(uuid4())
        repository_dir = self._repository_dir(repository_id)
        try:
            repository_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            archive_path = repository_dir / "archive.zip"
            staging_path.rename(archive_path)
            os.chmod(archive_path, 0o600)
            repository, job = self.lifecycle.create_submission(
                repository_id=repository_id,
                job_id=job_id,
                name=repository_name,
                source_kind=SourceKind.GITHUB,
                source_url=download.canonical_url,
                source_ref=request.ref,
                commit_sha=download.commit_sha,
            )
            return RepositoryCreateResponse(
                repository_id=repository.id,
                job_id=job.id,
                status=repository.status,
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            if self.repositories.get_repository(repository_id) is None:
                self._remove_path(repository_dir)
            raise

    async def submit_upload(
        self,
        upload: AsyncUpload,
        *,
        name: str | None = None,
    ) -> RepositoryCreateResponse:
        """Persist a bounded ZIP upload and enqueue it without trusting its filename."""

        try:
            filename = validate_safe_filename(upload.filename or "")
        except UnsafeFilenameError as error:
            raise APIIngestionError("INVALID_UPLOAD_FILENAME", str(error)) from error
        if Path(filename).suffix.casefold() != ".zip":
            raise APIIngestionError("INVALID_ARCHIVE_TYPE", "Upload a ZIP archive.")
        repository_name = self._validated_name(name or Path(filename).stem)

        staging_path = self._new_staging_path()
        written = 0
        try:
            with staging_path.open("xb") as destination:
                os.chmod(staging_path, 0o600)
                while True:
                    chunk = await upload.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.settings.max_archive_bytes:
                        raise APIIngestionError(
                            "ARCHIVE_TOO_LARGE",
                            "The repository archive is too large.",
                            status_code=413,
                        )
                    destination.write(chunk)
            if written == 0:
                raise APIIngestionError("EMPTY_ARCHIVE", "The ZIP archive is empty.")
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise

        repository_id = str(uuid4())
        job_id = str(uuid4())
        repository_dir = self._repository_dir(repository_id)
        try:
            repository_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            archive_path = repository_dir / "archive.zip"
            staging_path.rename(archive_path)
            os.chmod(archive_path, 0o600)
            repository, job = self.lifecycle.create_submission(
                repository_id=repository_id,
                job_id=job_id,
                name=repository_name,
                source_kind=SourceKind.ZIP,
            )
            return RepositoryCreateResponse(
                repository_id=repository.id,
                job_id=job.id,
                status=repository.status,
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            if self.repositories.get_repository(repository_id) is None:
                self._remove_path(repository_dir)
            raise

    def enqueue_reindex(self, repository_id: str) -> RepositoryCreateResponse:
        """Queue a rebuild from the already acquired immutable local snapshot."""

        try:
            with self._operation_lock(repository_id):
                repository = self.repositories.get_repository(repository_id)
                if repository is None:
                    raise ResourceNotFoundError("Repository")
                repository_dir = self._repository_dir(repository_id)
                if (
                    not (repository_dir / "source").is_dir()
                    and not (repository_dir / "archive.zip").is_file()
                ):
                    raise ResourceConflictError("The immutable repository snapshot is unavailable.")
                repository, job = self.lifecycle.start_reindex(
                    repository_id=repository_id,
                    job_id=str(uuid4()),
                )
        except (ActiveJobExistsError, InvalidRepositoryTransitionError) as error:
            raise ResourceConflictError("Repository work is already in progress.") from error
        except Timeout as error:
            raise ResourceConflictError(
                "Repository indexing is active; wait for it to finish."
            ) from error
        return RepositoryCreateResponse(
            repository_id=repository_id,
            job_id=job.id,
            status=repository.status,
        )

    def delete_repository(self, repository_id: str) -> None:
        """Synchronously delete vectors, bytes, and manifest under a cross-process lock."""

        repository = self.repositories.get_repository(repository_id)
        if repository is None:
            raise ResourceNotFoundError("Repository")
        try:
            with self._operation_lock(repository_id):
                running = self.jobs.list_jobs(
                    repository_id=repository_id,
                    status=JobStatus.RUNNING,
                )
                if running:
                    raise ResourceConflictError(
                        "Repository indexing is active; wait for it to finish before deletion."
                    )
                self.repositories.update_repository(
                    repository_id,
                    status=RepositoryStatus.DELETING,
                )
                for job in self.jobs.list_jobs(
                    repository_id=repository_id,
                    status=JobStatus.QUEUED,
                ):
                    self.jobs.cancel_job(job.id)
                collections = set(self.vector_index.repository_collections(repository_id))
                if repository.collection_name:
                    collections.add(repository.collection_name)
                for collection_name in collections:
                    self.vector_index.delete(
                        repository_id,
                        collection_name=collection_name,
                    )
                remaining_collections = {
                    collection_name
                    for collection_name in collections
                    if self.vector_index.has_collection(
                        repository_id,
                        collection_name=collection_name,
                    )
                }
                if remaining_collections or self.vector_index.repository_collections(repository_id):
                    raise RuntimeError("Repository vector deletion could not be verified.")
                repository_dir = self._repository_dir(repository_id)
                self._remove_path(repository_dir)
                if repository_dir.exists():
                    raise RuntimeError("Repository file deletion could not be verified.")
                if not self.repositories.delete_repository(repository_id):
                    raise RuntimeError("Repository manifest deletion could not be verified.")
        except Timeout as error:
            raise ResourceConflictError(
                "Repository indexing is active; wait for it to finish before deletion."
            ) from error

    @staticmethod
    def _source_root(extraction_root: Path) -> Path:
        entries = sorted(extraction_root.iterdir(), key=lambda path: path.name.casefold())
        if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
            return entries[0]
        return extraction_root

    def _prepare_source(self, job: JobRecord, repository_dir: Path) -> Path:
        source_dir = repository_dir / "source"
        archive_path = repository_dir / "archive.zip"
        if job.kind is JobKind.INGEST:
            self._remove_path(source_dir)
        if not source_dir.is_dir():
            if not archive_path.is_file() or archive_path.is_symlink():
                raise UnsafeArchiveError("The repository archive is unavailable.")
            self.extractor.extract(archive_path, source_dir)
        return self._source_root(source_dir)

    def reconcile_startup(self) -> dict[str, int]:
        """Repair orphaned durable state and prune only stale unreferenced storage."""

        counts = {"jobs_requeued": 0, "repositories_failed": 0, "paths_removed": 0}
        repositories: list[RepositoryRecord] = []
        offset = 0
        while True:
            page = self.repositories.list_repositories(limit=1000, offset=offset)
            repositories.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        by_id = {repository.id: repository for repository in repositories}
        stale_before = time.time() - max(
            3600.0,
            self.settings.github_download_timeout_seconds * 2,
        )

        for parent in (self.settings.staging_dir, self.settings.repositories_dir):
            for path in parent.iterdir():
                try:
                    modified = path.lstat().st_mtime
                except OSError:
                    continue
                is_orphan_repository = (
                    parent == self.settings.repositories_dir and path.name not in by_id
                )
                if modified < stale_before and (
                    parent == self.settings.staging_dir or is_orphan_repository
                ):
                    try:
                        self._remove_path(path)
                        counts["paths_removed"] += 1
                    except OSError as error:
                        logger.warning(
                            "startup_path_cleanup_failed",
                            error_type=type(error).__name__,
                        )

        for repository in repositories:
            try:
                with self._operation_lock(repository.id):
                    active = self.jobs.list_jobs(
                        repository_id=repository.id,
                        status=JobStatus.QUEUED,
                    ) + self.jobs.list_jobs(
                        repository_id=repository.id,
                        status=JobStatus.RUNNING,
                    )
                    repository_dir = self._repository_dir(repository.id)
                    snapshot_exists = (repository_dir / "source").is_dir() or (
                        repository_dir / "archive.zip"
                    ).is_file()
                    if (
                        repository.status in {RepositoryStatus.QUEUED, RepositoryStatus.INDEXING}
                        and not active
                    ):
                        if snapshot_exists:
                            kind = (
                                JobKind.INGEST
                                if repository.status is RepositoryStatus.QUEUED
                                else JobKind.REINDEX
                            )
                            with suppress(ActiveJobExistsError):
                                self.jobs.enqueue_job(repository.id, kind)
                                counts["jobs_requeued"] += 1
                        else:
                            self.repositories.mark_repository_failed(
                                repository.id,
                                error_code="SNAPSHOT_MISSING",
                                error_message="The immutable repository snapshot is unavailable.",
                            )
                            counts["repositories_failed"] += 1
                    active_collection = repository.collection_name
                    if repository.status is RepositoryStatus.READY and (
                        active_collection is None
                        or not self.vector_index.has_collection(
                            repository.id,
                            collection_name=active_collection,
                        )
                    ):
                        self.repositories.mark_repository_failed(
                            repository.id,
                            error_code="INDEX_MISSING",
                            error_message=(
                                "The published repository index is unavailable; "
                                "reindex is required."
                            ),
                        )
                        counts["repositories_failed"] += 1
                    if repository.status in {RepositoryStatus.READY, RepositoryStatus.FAILED}:
                        for collection in self.vector_index.repository_collections(repository.id):
                            if collection != active_collection:
                                self.vector_index.delete(
                                    repository.id,
                                    collection_name=collection,
                                )
            except Timeout:
                continue
            except Exception as error:
                logger.warning(
                    "startup_repository_reconciliation_failed",
                    repository_id=repository.id,
                    error_type=type(error).__name__,
                )
        return counts

    def _build_index(
        self,
        job: JobRecord,
        worker_id: str,
        repository: RepositoryRecord,
    ) -> tuple[str, RepositoryStats]:
        repository_dir = self._repository_dir(repository.id)
        self.jobs.update_progress(
            job.id,
            worker_id,
            stage=JobStage.EXTRACTING,
            progress=10,
        )
        source_root = self._prepare_source(job, repository_dir)
        self.jobs.update_progress(
            job.id,
            worker_id,
            stage=JobStage.SCANNING,
            progress=25,
        )
        scan = self.scanner.scan(source_root)
        if not scan.files:
            raise ScanError("The repository contains no indexable text files.")

        self.jobs.update_progress(
            job.id,
            worker_id,
            stage=JobStage.PARSING,
            progress=40,
        )
        languages: Counter[str] = Counter()
        tree_sitter_files = 0
        fallback_files = 0
        redaction_count = 0
        chunk_count = 0

        def chunks() -> Iterator[CodeChunk]:
            nonlocal chunk_count, fallback_files, redaction_count, tree_sitter_files
            for source_file in scan.files:
                result = self.chunker.chunk_file_result(
                    source_file,
                    repository.id,
                    repository.commit_sha,
                )
                redaction_count += result.redaction_count
                if result.chunks:
                    languages[source_file.language] += 1
                    if any(chunk.parser == "tree_sitter" for chunk in result.chunks):
                        tree_sitter_files += 1
                    else:
                        fallback_files += 1
                for chunk in result.chunks:
                    chunk_count += 1
                    if chunk_count > self.settings.max_chunks:
                        raise ChunkLimitError("The repository produces too many chunks.")
                    yield chunk
            if chunk_count == 0:
                raise ChunkingError("The repository contains no indexable code chunks.")

        self.jobs.update_progress(
            job.id,
            worker_id,
            stage=JobStage.EMBEDDING,
            progress=60,
        )
        collection_name = self.vector_index.index(repository.id, chunks())
        self.jobs.update_progress(
            job.id,
            worker_id,
            stage=JobStage.INDEXING,
            progress=90,
        )
        stats = RepositoryStats(
            file_count=scan.file_count,
            chunk_count=chunk_count,
            skipped_file_count=scan.skipped_file_count,
            tree_sitter_file_count=tree_sitter_files,
            fallback_file_count=fallback_files,
            redaction_count=redaction_count,
            indexed_bytes=scan.indexed_bytes,
            languages=dict(sorted(languages.items())),
        )
        return collection_name, stats

    def _cleanup_inactive_collections(
        self,
        repository_id: str,
        *,
        active_collection: str,
        previous_collection: str | None,
    ) -> None:
        targets: set[str] = set()
        try:
            targets.update(self.vector_index.repository_collections(repository_id))
        except Exception as error:
            logger.warning(
                "collection_cleanup_discovery_failed",
                repository_id=repository_id,
                error_type=type(error).__name__,
            )
        if previous_collection:
            targets.add(previous_collection)
        targets.discard(active_collection)
        for collection_name in targets:
            try:
                self.vector_index.delete(
                    repository_id,
                    collection_name=collection_name,
                )
            except Exception as error:
                logger.warning(
                    "collection_cleanup_delete_failed",
                    repository_id=repository_id,
                    error_type=type(error).__name__,
                )
        remaining: list[str] = []
        for collection_name in targets:
            try:
                if self.vector_index.has_collection(
                    repository_id,
                    collection_name=collection_name,
                ):
                    remaining.append(collection_name)
            except Exception:
                remaining.append(collection_name)
        if remaining:
            logger.warning(
                "collection_cleanup_incomplete",
                repository_id=repository_id,
                remaining_count=len(remaining),
            )

    def _fail_processing_job(
        self,
        job: JobRecord,
        worker_id: str,
        error: BaseException,
        *,
        previous_collection: str | None,
        new_collection: str | None,
    ) -> None:
        current = self.repositories.get_repository(job.repository_id)
        if new_collection and (current is None or current.collection_name != new_collection):
            try:
                self.vector_index.delete(
                    job.repository_id,
                    collection_name=new_collection,
                )
            except Exception as cleanup_error:
                logger.warning(
                    "failed_collection_cleanup_failed",
                    repository_id=job.repository_id,
                    error_type=type(cleanup_error).__name__,
                )
        restore_collection: str | None = None
        if job.kind is JobKind.REINDEX and previous_collection:
            try:
                if self.vector_index.has_collection(
                    job.repository_id,
                    collection_name=previous_collection,
                ):
                    restore_collection = previous_collection
            except Exception:
                restore_collection = None
        try:
            self.lifecycle.fail_processing_job(
                repository_id=job.repository_id,
                job_id=job.id,
                worker_id=worker_id,
                error_code=type(error).__name__,
                error_message=safe_error_message(error),
                retryable=not isinstance(error, _NON_RETRYABLE_ERRORS),
                restore_collection=restore_collection,
            )
        except (InvalidJobTransitionError, JobLeaseError, JobNotFoundError):
            logger.warning(
                "job_failure_transition_rejected",
                job_id=job.id,
                worker_id=worker_id,
            )

    def process_job(self, job: JobRecord, worker_id: str) -> None:
        """Run one claimed ingest/reindex job and make both records terminal."""

        previous_collection: str | None = None
        new_collection: str | None = None
        if job.kind not in {JobKind.INGEST, JobKind.REINDEX}:
            self._fail_processing_job(
                job,
                worker_id,
                ValueError("The job kind is not supported by this worker."),
                previous_collection=None,
                new_collection=None,
            )
            return
        try:
            with self._operation_lock(job.repository_id):
                self.jobs.renew_lease(job.id, worker_id)
                repository = self.repositories.get_repository(job.repository_id)
                if repository is None:
                    raise RepositoryNotFoundError(job.repository_id)
                if repository.status is RepositoryStatus.DELETING:
                    raise InvalidRepositoryTransitionError(
                        "A deleting repository cannot be indexed."
                    )
                previous_collection = repository.collection_name
                repository = self.repositories.update_repository(
                    repository.id,
                    status=RepositoryStatus.INDEXING,
                )
                new_collection, stats = self._build_index(job, worker_id, repository)
                self.jobs.renew_lease(job.id, worker_id)
                self.lifecycle.complete_index(
                    repository_id=repository.id,
                    job_id=job.id,
                    worker_id=worker_id,
                    commit_sha=repository.commit_sha,
                    collection_name=new_collection,
                    index_fingerprint=index_fingerprint(self.settings),
                    stats=stats,
                )
                self._cleanup_inactive_collections(
                    repository.id,
                    active_collection=new_collection,
                    previous_collection=previous_collection,
                )
        except Exception as error:
            self._fail_processing_job(
                job,
                worker_id,
                error,
                previous_collection=previous_collection,
                new_collection=new_collection,
            )

    async def process_job_async(self, job: JobRecord, worker_id: str) -> None:
        await asyncio.to_thread(self.process_job, job, worker_id)


@asynccontextmanager
async def uploaded_bytes(data: bytes, filename: str) -> AsyncIterator[AsyncUpload]:
    """Tiny in-memory upload adapter useful for programmatic clients and tests."""

    class _Upload:
        def __init__(self) -> None:
            self.filename: str | None = filename
            self._offset = 0

        async def read(self, size: int = -1) -> bytes:
            if size < 0:
                size = len(data) - self._offset
            chunk = data[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    yield _Upload()


__all__ = ["AsyncUpload", "IngestionService", "uploaded_bytes"]
