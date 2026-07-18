"""Durable inline and standalone workers for repository indexing jobs."""

from __future__ import annotations

import argparse
import asyncio
import socket
from contextlib import suppress
from uuid import uuid4

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.pipeline import IngestionService
from codebase_intelligence.job_service import (
    InvalidJobTransitionError,
    JobLeaseError,
    JobNotFoundError,
    JobService,
)
from codebase_intelligence.observability import configure_logging, get_logger

logger = get_logger(__name__)


class JobWorker:
    """Claim durable jobs, renew their leases, and delegate bounded processing."""

    def __init__(
        self,
        jobs: JobService,
        ingestion: IngestionService,
        *,
        poll_seconds: float,
        lease_seconds: int,
        worker_id: str | None = None,
    ) -> None:
        self.jobs = jobs
        self.ingestion = ingestion
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid4()}"
        self._stop = asyncio.Event()

    async def _renew_lease(self, job_id: str) -> None:
        await asyncio.to_thread(self.jobs.renew_lease, job_id, self.worker_id)

    async def run_once(self) -> bool:
        """Process at most one job and return whether work was claimed."""

        job = await asyncio.to_thread(self.jobs.claim_next_job, self.worker_id)
        if job is None:
            return False
        processing = asyncio.create_task(
            self.ingestion.process_job_async(job, self.worker_id),
            name=f"codebase-job-{job.id}",
        )
        heartbeat_seconds = max(1.0, self.lease_seconds / 3)
        try:
            while not processing.done():
                done, _ = await asyncio.wait({processing}, timeout=heartbeat_seconds)
                if done:
                    break
                try:
                    await self._renew_lease(job.id)
                except (InvalidJobTransitionError, JobLeaseError, JobNotFoundError):
                    logger.warning("worker_lease_lost", job_id=job.id, worker_id=self.worker_id)
                    break
                except Exception as error:
                    logger.error(
                        "worker_heartbeat_failed",
                        job_id=job.id,
                        worker_id=self.worker_id,
                        error_type=type(error).__name__,
                    )
                    break
        finally:
            try:
                await processing
            except Exception as error:
                logger.error(
                    "worker_job_failed_unhandled",
                    job_id=job.id,
                    worker_id=self.worker_id,
                    error_type=type(error).__name__,
                )
        return True

    async def run_forever(self) -> None:
        """Recover stale jobs and poll until a cooperative stop is requested."""

        await asyncio.to_thread(self.jobs.recover_stale_jobs)
        logger.info("worker_started", worker_id=self.worker_id)
        try:
            while not self._stop.is_set():
                try:
                    worked = await self.run_once()
                except Exception as error:
                    logger.error(
                        "worker_iteration_failed",
                        worker_id=self.worker_id,
                        error_type=type(error).__name__,
                    )
                    worked = False
                if worked:
                    continue
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
        finally:
            logger.info("worker_stopped", worker_id=self.worker_id)

    def stop(self) -> None:
        self._stop.set()


async def _run_standalone(settings: Settings) -> None:
    if not settings.qdrant_url:
        raise RuntimeError("A standalone worker requires CODEBASE_INTEL_QDRANT_URL.")
    from codebase_intelligence.container import AppContainer

    container = AppContainer(settings, enable_inline_worker=False)
    await container.start()
    if container.ingestion_service is None:
        await container.close()
        raise RuntimeError("The embedding and Qdrant providers are not ready.")
    worker = JobWorker(
        container.jobs,
        container.ingestion_service,
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.worker_lease_seconds,
    )
    try:
        await worker.run_forever()
    finally:
        await container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Codebase Intelligence job worker.")
    parser.parse_args()
    settings = Settings()
    configure_logging(
        level=settings.log_level,
        json_logs=settings.environment == "production",
    )
    with suppress(KeyboardInterrupt):
        asyncio.run(_run_standalone(settings))


if __name__ == "__main__":
    main()
