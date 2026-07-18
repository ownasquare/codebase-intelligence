"""Application dependency composition and lifecycle ownership."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from codebase_intelligence.config import Settings
from codebase_intelligence.database import Database
from codebase_intelligence.ingestion.pipeline import IngestionService
from codebase_intelligence.job_service import JobService
from codebase_intelligence.observability import get_logger
from codebase_intelligence.providers import create_completion_provider
from codebase_intelligence.rag_service import RAGService
from codebase_intelligence.repository import RepositoryStore
from codebase_intelligence.source_service import SourceExplorerService
from codebase_intelligence.vector_store import CodeVectorIndex
from codebase_intelligence.worker import JobWorker

logger = get_logger(__name__)


class AppContainer:
    """Own dependencies shared by HTTP handlers and the inline worker."""

    def __init__(self, settings: Settings, *, enable_inline_worker: bool = True) -> None:
        self.settings = settings
        self.enable_inline_worker = enable_inline_worker
        settings.ensure_directories()
        self.database = Database(settings.database_path)
        self.database.initialize()
        self.repositories = RepositoryStore(self.database)
        self.jobs = JobService(
            self.database,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
        )
        self.vector_index: CodeVectorIndex | None = None
        self.ingestion_service: IngestionService | None = None
        self.rag_service: RAGService | None = None
        self.source_explorer: SourceExplorerService | None = None
        self.worker: JobWorker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._embedding_initialization_failed = False
        self._answer_initialization_failed = False

        if settings.embedding_ready:
            try:
                self.vector_index = CodeVectorIndex(settings)
                self.ingestion_service = IngestionService(
                    settings,
                    self.repositories,
                    self.jobs,
                    self.vector_index,
                )
                self.source_explorer = SourceExplorerService(
                    settings,
                    self.repositories,
                    self.vector_index,
                )
            except Exception as error:
                self._embedding_initialization_failed = True
                logger.warning(
                    "embedding_initialization_failed",
                    error_type=type(error).__name__,
                )

        if self.vector_index is not None:
            completion = None
            if settings.answer_ready:
                try:
                    completion = create_completion_provider(settings)
                except Exception as error:
                    self._answer_initialization_failed = True
                    logger.warning(
                        "answer_initialization_failed",
                        error_type=type(error).__name__,
                    )
            try:
                self.rag_service = RAGService(
                    settings,
                    self.repositories,
                    self.vector_index,
                    completion,
                )
            except Exception as error:
                self._answer_initialization_failed = True
                logger.warning(
                    "rag_initialization_failed",
                    error_type=type(error).__name__,
                )

    @property
    def embedding_operational(self) -> bool:
        return self.vector_index is not None and not self._embedding_initialization_failed

    @property
    def answer_operational(self) -> bool:
        if self.settings.answer_provider == "extractive":
            return self.rag_service is not None
        return (
            self.settings.answer_ready
            and self.rag_service is not None
            and not self._answer_initialization_failed
        )

    async def start(self) -> None:
        """Start the inline worker when this process owns local queue execution."""

        if self.ingestion_service is not None:
            reconciliation = await asyncio.to_thread(self.ingestion_service.reconcile_startup)
            if any(reconciliation.values()):
                logger.info("startup_reconciliation", **reconciliation)
        if (
            not self.enable_inline_worker
            or not self.settings.inline_worker
            or self.ingestion_service is None
            or self._worker_task is not None
        ):
            return
        self.worker = JobWorker(
            self.jobs,
            self.ingestion_service,
            poll_seconds=self.settings.worker_poll_seconds,
            lease_seconds=self.settings.worker_lease_seconds,
        )
        self._worker_task = asyncio.create_task(
            self.worker.run_forever(),
            name="codebase-intelligence-inline-worker",
        )
        self._worker_task.add_done_callback(self._log_worker_completion)

    @staticmethod
    def _log_worker_completion(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "inline_worker_stopped_unexpectedly",
                error_type=type(error).__name__,
            )

    def readiness_checks(self) -> dict[str, bool]:
        """Return non-secret dependency health without flattening answer-provider mode."""

        try:
            database_ready = self.database.applied_migrations() == (1, 2, 3)
        except Exception:
            database_ready = False
        qdrant_ready = False
        if self.vector_index is not None:
            try:
                qdrant_ready = self.vector_index.healthcheck()
            except Exception:
                qdrant_ready = False
        checks = {
            "database": database_ready,
            "embedding": self.settings.embedding_ready and self.embedding_operational,
            "qdrant": qdrant_ready,
        }
        if self.enable_inline_worker and self.settings.inline_worker:
            checks["worker"] = self._worker_task is not None and not self._worker_task.done()
        return checks

    async def close(self) -> None:
        """Cooperatively stop work before releasing the vector client."""

        if self.worker is not None:
            self.worker.stop()
        if self._worker_task is not None:
            with suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        if self.vector_index is not None:
            await asyncio.to_thread(self.vector_index.close)


__all__ = ["AppContainer"]
