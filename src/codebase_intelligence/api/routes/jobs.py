"""Durable job status and cancellation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from codebase_intelligence.api.dependencies import get_container
from codebase_intelligence.exceptions import ResourceConflictError, ResourceNotFoundError
from codebase_intelligence.models import JobRecord, JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobRecord])
def list_jobs(
    request: Request,
    repository_id: str | None = None,
    status: JobStatus | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[JobRecord]:
    return get_container(request).jobs.list_jobs(
        repository_id=repository_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=JobRecord)
def get_job(job_id: str, request: Request) -> JobRecord:
    job = get_container(request).jobs.get_job(job_id)
    if job is None:
        raise ResourceNotFoundError("Job")
    return job


@router.post("/{job_id}/cancel", response_model=JobRecord)
def cancel_job(job_id: str, request: Request) -> JobRecord:
    jobs = get_container(request).jobs
    job = jobs.get_job(job_id)
    if job is None:
        raise ResourceNotFoundError("Job")
    if job.status is JobStatus.RUNNING:
        raise ResourceConflictError(
            "Running jobs cannot be cancelled safely; wait for the current indexing phase."
        )
    return jobs.cancel_job(job_id)
