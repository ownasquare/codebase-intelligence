"""Typed, timeout-bounded client used by the Streamlit application."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from codebase_intelligence.models import (
    ChatMessage,
    HealthResponse,
    JobRecord,
    JobStatus,
    ProblemDetail,
    QuestionRequest,
    QuestionResponse,
    RepositoryCreateResponse,
    RepositoryRecord,
    SourceDetailResponse,
    SourceListResponse,
    StatusResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=105.0, write=120.0, pool=5.0)
_MAX_ERROR_LENGTH = 280
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:authorization|x-api-key|x-github-token)\s*[:=]\s*\S+"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def _sanitize_message(value: object, *, fallback: str) -> str:
    """Return a compact message that cannot echo common credential formats."""

    if not isinstance(value, str):
        return fallback
    message = " ".join(value.replace("\x00", "").split())
    for pattern in _SENSITIVE_PATTERNS:
        message = pattern.sub("[redacted]", message)
    if not message:
        return fallback
    if len(message) > _MAX_ERROR_LENGTH:
        return f"{message[: _MAX_ERROR_LENGTH - 1]}…"
    return message


class ApiError(RuntimeError):
    """Safe API failure suitable for direct display in the UI."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "api_error",
        request_id: str | None = None,
    ) -> None:
        safe_message = _sanitize_message(message, fallback="The request could not be completed.")
        super().__init__(safe_message)
        self.message = safe_message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class ApiClient:
    """Small synchronous client around the versioned FastAPI surface.

    Provider and API credentials are supplied by process configuration. A private
    GitHub token is attached only to its single create request and is never added
    to the client's persistent headers.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Pass either client or transport, not both.")
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return

        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def timeout(self) -> httpx.Timeout:
        """Expose the effective timeout for diagnostics and contract tests."""

        return self._client.timeout

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def health(self) -> HealthResponse:
        return self._get_model("/api/v1/health/live", HealthResponse)

    def status(self) -> StatusResponse:
        return self._get_model("/api/v1/status", StatusResponse)

    def list_repositories(self) -> list[RepositoryRecord]:
        payload = self._request_json("GET", "/api/v1/repositories")
        if isinstance(payload, dict) and "repositories" in payload:
            payload = payload["repositories"]
        if not isinstance(payload, list):
            raise ApiError(
                "The API returned an unexpected repository list.",
                code="invalid_response",
            )
        try:
            return [RepositoryRecord.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise ApiError(
                "The API returned an unexpected repository record.",
                code="invalid_response",
            ) from exc

    def create_github_repository(
        self,
        *,
        url: str,
        ref: str | None = None,
        token: str | None = None,
        name: str | None = None,
    ) -> RepositoryCreateResponse:
        body: dict[str, str] = {"url": url.strip()}
        if ref and ref.strip():
            body["ref"] = ref.strip()
        if name and name.strip():
            body["name"] = name.strip()
        headers = {"X-GitHub-Token": token} if token else None
        payload = self._request_json(
            "POST",
            "/api/v1/repositories",
            json=body,
            headers=headers,
        )
        return self._validate_model(RepositoryCreateResponse, payload)

    def upload_repository(
        self,
        *,
        filename: str,
        content: bytes,
        name: str | None = None,
    ) -> RepositoryCreateResponse:
        safe_filename = self._safe_filename(filename)
        data = {"name": name.strip()} if name and name.strip() else None
        payload = self._request_json(
            "POST",
            "/api/v1/repositories/upload",
            files={"file": (safe_filename, content, "application/zip")},
            data=data,
        )
        return self._validate_model(RepositoryCreateResponse, payload)

    def get_repository(self, repository_id: str) -> RepositoryRecord:
        repository_path = self._identifier(repository_id)
        return self._get_model(
            f"/api/v1/repositories/{repository_path}",
            RepositoryRecord,
        )

    def list_sources(
        self,
        repository_id: str,
        *,
        query: str | None = None,
        language: str | None = None,
        limit: int = 200,
    ) -> SourceListResponse:
        """Return the bounded redacted source catalog for one active repository index."""

        repository_path = self._identifier(repository_id)
        params: dict[str, str | int] = {"limit": limit}
        if query and query.strip():
            params["q"] = query.strip()
        if language and language.strip():
            params["language"] = language.strip()
        return self._get_model(
            f"/api/v1/repositories/{repository_path}/sources",
            SourceListResponse,
            params=params,
        )

    def get_source(self, repository_id: str, path: str) -> SourceDetailResponse:
        """Return indexed redacted sections for one exact repository-relative path."""

        repository_path = self._identifier(repository_id)
        if not path.strip():
            raise ApiError("A source path is required.", code="invalid_source_path")
        return self._get_model(
            f"/api/v1/repositories/{repository_path}/source",
            SourceDetailResponse,
            params={"path": path},
        )

    def delete_repository(self, repository_id: str) -> None:
        repository_path = self._identifier(repository_id)
        self._request("DELETE", f"/api/v1/repositories/{repository_path}")

    def reindex_repository(self, repository_id: str) -> RepositoryCreateResponse:
        repository_path = self._identifier(repository_id)
        payload = self._request_json(
            "POST",
            f"/api/v1/repositories/{repository_path}/reindex",
        )
        return self._validate_model(RepositoryCreateResponse, payload)

    def ask_question(
        self,
        repository_id: str,
        *,
        question: str,
        top_k: int = 8,
        history: list[ChatMessage] | None = None,
    ) -> QuestionResponse:
        repository_path = self._identifier(repository_id)
        request = QuestionRequest(
            question=question,
            top_k=top_k,
            history=history or [],
        )
        payload = self._request_json(
            "POST",
            f"/api/v1/repositories/{repository_path}/questions",
            json=request.model_dump(mode="json"),
        )
        return self._validate_model(QuestionResponse, payload)

    def get_job(self, job_id: str) -> JobRecord:
        job_path = self._identifier(job_id)
        return self._get_model(f"/api/v1/jobs/{job_path}", JobRecord)

    def list_jobs(
        self,
        *,
        repository_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRecord]:
        """List durable jobs using the API's existing bounded filter contract."""

        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if repository_id is not None:
            cleaned_repository_id = repository_id.strip()
            if not cleaned_repository_id:
                raise ApiError(
                    "A repository identifier is required.",
                    code="invalid_identifier",
                )
            params["repository_id"] = cleaned_repository_id
        if status is not None:
            params["status"] = status.value

        payload = self._request_json("GET", "/api/v1/jobs", params=params)
        if not isinstance(payload, list):
            raise ApiError(
                "The API returned an unexpected job list.",
                code="invalid_response",
            )
        try:
            return [JobRecord.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise ApiError(
                "The API returned an unexpected job record.",
                code="invalid_response",
            ) from exc

    def _get_model(self, path: str, model: type[ModelT], **kwargs: Any) -> ModelT:
        return self._validate_model(model, self._request_json("GET", path, **kwargs))

    def _request_json(self, method: str, path: str, **kwargs: Any) -> object:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                "The API returned a response that could not be read.",
                status_code=response.status_code,
                code="invalid_response",
            ) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(
                "The API took too long to respond. Try again in a moment.",
                code="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                "The API is unavailable. Check that the service is running and try again.",
                code="unavailable",
            ) from exc

        if response.is_error:
            raise self._error_from_response(response)
        return response

    @staticmethod
    def _validate_model(model: type[ModelT], payload: object) -> ModelT:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise ApiError(
                "The API returned data in an unexpected format.",
                code="invalid_response",
            ) from exc

    @staticmethod
    def _error_from_response(response: httpx.Response) -> ApiError:
        fallback = ApiClient._fallback_for_status(response.status_code)
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            try:
                problem = ProblemDetail.model_validate(payload)
            except ValidationError:
                problem = None
            if problem is not None:
                return ApiError(
                    _sanitize_message(problem.detail, fallback=fallback),
                    status_code=response.status_code,
                    code=problem.code,
                    request_id=problem.request_id,
                )

        return ApiError(
            fallback,
            status_code=response.status_code,
            code=f"http_{response.status_code}",
        )

    @staticmethod
    def _fallback_for_status(status_code: int) -> str:
        if status_code == 401:
            return "The API rejected its configured authentication."
        if status_code == 403:
            return "This operation is not permitted."
        if status_code == 404:
            return "The requested item no longer exists."
        if status_code == 409:
            return "The repository is not ready for this operation."
        if status_code == 413:
            return "The uploaded repository exceeds the configured safety limit."
        if status_code == 422:
            return "The request contains invalid or incomplete information."
        if status_code >= 500:
            return "The API could not complete the operation. Try again shortly."
        return "The request could not be completed."

    @staticmethod
    def _identifier(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ApiError("A repository or job identifier is required.", code="invalid_identifier")
        return quote(cleaned, safe="")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        normalized = filename.replace("\\", "/")
        leaf = PurePosixPath(normalized).name.strip()
        if not leaf or leaf in {".", ".."}:
            return "repository.zip"
        return leaf[:255]


__all__ = ["DEFAULT_TIMEOUT", "ApiClient", "ApiError"]
