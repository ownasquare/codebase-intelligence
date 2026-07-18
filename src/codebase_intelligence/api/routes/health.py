"""Liveness and dependency-readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from codebase_intelligence.api.dependencies import get_container
from codebase_intelligence.models import HealthResponse

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="ok", checks={"process": True})


@router.get("/ready", response_model=HealthResponse)
def ready(request: Request, response: Response) -> HealthResponse:
    container = get_container(request)
    checks = container.readiness_checks()
    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if all(checks.values()) else "degraded",
        checks=checks,
    )
