"""Shared domain and API contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class SourceKind(StrEnum):
    GITHUB = "github"
    ZIP = "zip"


class RepositoryStatus(StrEnum):
    QUEUED = "queued"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class JobKind(StrEnum):
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    SCANNING = "scanning"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    DELETING = "deleting"
    COMPLETE = "complete"


class RepositoryStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_count: int = 0
    chunk_count: int = 0
    skipped_file_count: int = 0
    tree_sitter_file_count: int = 0
    fallback_file_count: int = 0
    redaction_count: int = 0
    indexed_bytes: int = 0
    languages: dict[str, int] = Field(default_factory=dict)


class RepositoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: RepositoryStatus
    source_kind: SourceKind
    source_url: str | None = None
    source_ref: str | None = None
    commit_sha: str | None = None
    collection_name: str | None = None
    index_fingerprint: str | None = None
    stats: RepositoryStats = Field(default_factory=RepositoryStats)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repository_id: str
    kind: JobKind
    status: JobStatus
    stage: JobStage
    progress: int = Field(ge=0, le=100)
    attempt: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class CodeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repository_id: str
    commit_sha: str | None = None
    path: str
    language: str
    symbol: str | None = None
    symbol_kind: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parser: Literal["tree_sitter", "fallback"]
    text: str
    content_hash: str

    @field_validator("end_line")
    @classmethod
    def end_line_is_valid(cls, value: int, info: Any) -> int:
        start_line = info.data.get("start_line")
        if isinstance(start_line, int) and value < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return value


class GitHubRepositoryRequest(BaseModel):
    url: str = Field(min_length=19, max_length=500)
    ref: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValueError("name must contain printable characters")
        return normalized


class RepositoryCreateResponse(BaseModel):
    repository_id: str
    job_id: str
    status: RepositoryStatus


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)


class Citation(BaseModel):
    source_id: str
    repository_id: str
    commit_sha: str | None = None
    path: str
    language: str
    symbol: str | None = None
    symbol_kind: str | None = None
    start_line: int
    end_line: int
    score: float | None = None
    excerpt: str
    permalink: str | None = None


class QuestionResponse(BaseModel):
    answer: str
    answer_mode: Literal["openai", "extractive"]
    citations: list[Citation]
    repository_id: str
    question: str


class ProviderState(BaseModel):
    provider: str
    model: str
    ready: bool
    mode: Literal["production", "demo"]


class StatusResponse(BaseModel):
    application: str
    version: str
    environment: str
    embedding: ProviderState
    answer: ProviderState
    qdrant_mode: Literal["embedded", "server"]
    inline_worker: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, bool] = Field(default_factory=dict)


class ProblemDetail(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None = None
