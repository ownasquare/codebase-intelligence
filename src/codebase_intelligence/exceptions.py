"""Stable, sanitized application errors."""

from __future__ import annotations


class CodebaseIntelligenceError(Exception):
    """Base error safe to map to an API problem response."""

    def __init__(self, code: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class ResourceNotFoundError(CodebaseIntelligenceError):
    def __init__(self, resource: str = "Resource") -> None:
        super().__init__("NOT_FOUND", f"{resource} was not found.", status_code=404)


class ResourceConflictError(CodebaseIntelligenceError):
    def __init__(self, detail: str) -> None:
        super().__init__("INVALID_STATE", detail, status_code=409)


class IndexMissingError(CodebaseIntelligenceError):
    def __init__(self) -> None:
        super().__init__(
            "INDEX_MISSING",
            "The published repository index is unavailable; reindex before asking questions.",
            status_code=409,
        )


class ProviderUnavailableError(CodebaseIntelligenceError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            "PROVIDER_UNAVAILABLE",
            f"The configured {provider} provider is not ready.",
            status_code=503,
        )


class IngestionError(CodebaseIntelligenceError):
    def __init__(self, code: str, detail: str, *, status_code: int = 422) -> None:
        super().__init__(code, detail, status_code=status_code)
