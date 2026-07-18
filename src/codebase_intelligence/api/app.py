"""FastAPI application factory and stable HTTP error boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from codebase_intelligence import __version__
from codebase_intelligence.api.dependencies import require_api_key
from codebase_intelligence.api.routes import explorer, health, jobs, query, repositories, status
from codebase_intelligence.config import Settings
from codebase_intelligence.container import AppContainer
from codebase_intelligence.exceptions import CodebaseIntelligenceError
from codebase_intelligence.job_service import (
    InvalidJobTransitionError,
    JobNotFoundError,
)
from codebase_intelligence.models import ProblemDetail
from codebase_intelligence.observability import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    configure_logging,
    current_request_id,
    get_logger,
)
from codebase_intelligence.repository import (
    InvalidRepositoryTransitionError,
    RepositoryNotFoundError,
)

logger = get_logger(__name__)


class RequestBodyTooLargeError(Exception):
    """Raised before multipart parsing can spool an unbounded request body."""


class RequestBodyLimitMiddleware:
    """Apply a hard ASGI byte limit, including chunked multipart requests."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "type": "about:blank",
                "title": "Request too large",
                "status": 413,
                "detail": "The request body exceeds the configured limit.",
                "code": "REQUEST_TOO_LARGE",
            },
        )
        await response(scope, receive, send)


def _problem_response(
    *,
    status_code: int,
    title: str,
    detail: str,
    code: str,
) -> JSONResponse:
    problem = ProblemDetail(
        title=title,
        status=status_code,
        detail=detail,
        code=code,
        request_id=current_request_id(),
    )
    headers = {"WWW-Authenticate": "API-Key"} if status_code == 401 else None
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
        headers=headers,
    )


def create_app(
    settings: Settings | None = None,
    *,
    container: AppContainer | None = None,
) -> FastAPI:
    """Create an app whose dependencies are initialized only inside its lifespan."""

    configured = settings or (container.settings if container is not None else Settings())
    configure_logging(
        level=configured.log_level,
        json_logs=configured.environment == "production",
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active = container or AppContainer(configured)
        application.state.container = active
        await active.start()
        try:
            yield
        finally:
            await active.close()

    application = FastAPI(
        title=configured.app_name,
        version=__version__,
        description="Secure, repository-scoped code retrieval with line-level citations.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=configured.max_archive_bytes + 1024 * 1024,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=configured.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-GitHub-Token", "X-Request-ID"],
    )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(RequestContextMiddleware)

    @application.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": configured.app_name,
            "version": __version__,
            "openapi": "/api/v1/openapi.json",
            "liveness": "/api/v1/health/live",
            "readiness": "/api/v1/health/ready",
        }

    application.include_router(health.router)
    protected = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
    protected.include_router(status.router)
    protected.include_router(repositories.router)
    protected.include_router(explorer.router)
    protected.include_router(jobs.router)
    protected.include_router(query.router)

    @protected.get("/openapi.json", include_in_schema=False)
    def protected_openapi_schema() -> JSONResponse:
        return JSONResponse(application.openapi())

    application.include_router(protected)

    @application.exception_handler(CodebaseIntelligenceError)
    async def handle_application_error(
        _request: Request,
        error: CodebaseIntelligenceError,
    ) -> JSONResponse:
        return _problem_response(
            status_code=error.status_code,
            title=error.code.replace("_", " ").title(),
            detail=error.detail,
            code=error.code,
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _problem_response(
            status_code=422,
            title="Request validation failed",
            detail="The request does not match the documented API contract.",
            code="VALIDATION_ERROR",
        )

    @application.exception_handler(RepositoryNotFoundError)
    @application.exception_handler(JobNotFoundError)
    async def handle_missing_record(_request: Request, _error: Exception) -> JSONResponse:
        return _problem_response(
            status_code=404,
            title="Not found",
            detail="The requested resource was not found.",
            code="NOT_FOUND",
        )

    @application.exception_handler(InvalidRepositoryTransitionError)
    @application.exception_handler(InvalidJobTransitionError)
    async def handle_invalid_transition(_request: Request, error: Exception) -> JSONResponse:
        return _problem_response(
            status_code=409,
            title="Invalid state",
            detail=str(error),
            code="INVALID_STATE",
        )

    @application.exception_handler(HTTPException)
    async def handle_http_error(_request: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail if isinstance(error.detail, str) else "The request failed."
        return _problem_response(
            status_code=error.status_code,
            title="HTTP error",
            detail=detail,
            code=f"HTTP_{error.status_code}",
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        logger.error("unhandled_request_error", error_type=type(error).__name__)
        return _problem_response(
            status_code=500,
            title="Internal server error",
            detail="The service could not complete the request.",
            code="INTERNAL_ERROR",
        )

    return application


app = create_app()


__all__ = ["app", "create_app"]
