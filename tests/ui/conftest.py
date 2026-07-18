"""Deterministic UI fakes and fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from codebase_intelligence.config import Settings
from codebase_intelligence.models import (
    Citation,
    HealthResponse,
    JobRecord,
    JobStage,
    JobStatus,
    ProviderState,
    QuestionResponse,
    RepositoryCreateResponse,
    RepositoryRecord,
    RepositoryStats,
    RepositoryStatus,
    RetrievalSignals,
    SourceDetailResponse,
    SourceFileSummary,
    SourceKind,
    SourceListResponse,
    SourceSection,
    StatusResponse,
)
from codebase_intelligence.ui.client import ApiError

APP_FILE = Path(__file__).resolve().parents[2] / "src" / "codebase_intelligence" / "ui" / "app.py"
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def repository_record(
    *,
    repository_id: str = "repo-1",
    name: str = "payments-service",
    status: RepositoryStatus = RepositoryStatus.READY,
    error_message: str | None = None,
) -> RepositoryRecord:
    return RepositoryRecord(
        id=repository_id,
        name=name,
        status=status,
        source_kind=SourceKind.GITHUB,
        source_url=f"https://github.com/acme/{name}",
        source_ref="main",
        commit_sha="a" * 40,
        collection_name=f"repo_{repository_id}",
        index_fingerprint="fingerprint",
        stats=RepositoryStats(
            file_count=42,
            chunk_count=180,
            skipped_file_count=3,
            tree_sitter_file_count=39,
            fallback_file_count=3,
            redaction_count=1,
            indexed_bytes=96_000,
            languages={"python": 31, "typescript": 11},
        ),
        error_code="index_failed" if error_message else None,
        error_message=error_message,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeApiClient:
    """In-memory replacement for every API operation used by AppTest."""

    def __init__(self, repositories: list[RepositoryRecord] | None = None) -> None:
        self.repositories = list(repositories or [])
        self.list_error: ApiError | None = None
        self.github_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.question_calls: list[dict[str, Any]] = []
        self.source_list_calls: list[dict[str, Any]] = []
        self.source_detail_calls: list[dict[str, str]] = []
        self.list_jobs_calls: list[dict[str, Any]] = []
        self.reindex_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.question_error: ApiError | None = None
        self.source_error: ApiError | None = None

    def health(self) -> HealthResponse:
        return HealthResponse(status="ok", checks={"manifest": True, "qdrant": True})

    def status(self) -> StatusResponse:
        return StatusResponse(
            application="Codebase Intelligence",
            version="0.3.0",
            environment="test",
            embedding=ProviderState(
                provider="deterministic",
                model="test-embedding",
                ready=True,
                mode="demo",
            ),
            answer=ProviderState(
                provider="extractive",
                model="ranked-context",
                ready=True,
                mode="demo",
            ),
            qdrant_mode="embedded",
            inline_worker=True,
        )

    def list_repositories(self) -> list[RepositoryRecord]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.repositories)

    def create_github_repository(
        self,
        *,
        url: str,
        ref: str | None = None,
        token: str | None = None,
        name: str | None = None,
    ) -> RepositoryCreateResponse:
        self.github_calls.append({"url": url, "ref": ref, "token": token, "name": name})
        created = RepositoryCreateResponse(
            repository_id="repo-created",
            job_id="job-created",
            status=RepositoryStatus.QUEUED,
        )
        self.repositories.append(
            repository_record(
                repository_id=created.repository_id,
                name="new-repository",
                status=RepositoryStatus.QUEUED,
            )
        )
        return created

    def upload_repository(
        self,
        *,
        filename: str,
        content: bytes,
        name: str | None = None,
    ) -> RepositoryCreateResponse:
        self.upload_calls.append({"filename": filename, "content": content, "name": name})
        return RepositoryCreateResponse(
            repository_id="repo-uploaded",
            job_id="job-uploaded",
            status=RepositoryStatus.QUEUED,
        )

    def get_repository(self, repository_id: str) -> RepositoryRecord:
        return next(
            repository for repository in self.repositories if repository.id == repository_id
        )

    def get_job(self, job_id: str) -> JobRecord:
        return JobRecord(
            id=job_id,
            repository_id="repo-created",
            kind="ingest",
            status=JobStatus.RUNNING,
            stage=JobStage.EMBEDDING,
            progress=64,
            attempt=1,
            created_at=NOW,
            updated_at=NOW,
            started_at=NOW,
        )

    def list_jobs(
        self,
        *,
        repository_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRecord]:
        self.list_jobs_calls.append(
            {
                "repository_id": repository_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            }
        )
        if repository_id is None:
            return []
        repository = next(
            (item for item in self.repositories if item.id == repository_id),
            None,
        )
        if repository is None:
            return []
        if repository.status in {RepositoryStatus.QUEUED, RepositoryStatus.INDEXING}:
            return [self.get_job("job-created")]
        return [
            JobRecord(
                id=f"job-{repository_id}",
                repository_id=repository_id,
                kind="ingest",
                status=JobStatus.SUCCEEDED,
                stage=JobStage.COMPLETE,
                progress=100,
                attempt=1,
                created_at=NOW,
                updated_at=NOW,
                started_at=NOW,
                completed_at=NOW,
            )
        ][:limit]

    def list_sources(
        self,
        repository_id: str,
        *,
        query: str | None = None,
        language: str | None = None,
        limit: int = 200,
    ) -> SourceListResponse:
        self.source_list_calls.append(
            {
                "repository_id": repository_id,
                "query": query,
                "language": language,
                "limit": limit,
            }
        )
        if self.source_error is not None:
            raise self.source_error
        files = [
            SourceFileSummary(
                path="src/auth/service.py",
                language="python",
                chunk_count=2,
                symbol_count=2,
                start_line=1,
                end_line=48,
            ),
            SourceFileSummary(
                path="src/payments/checkout.ts",
                language="typescript",
                chunk_count=3,
                symbol_count=2,
                start_line=1,
                end_line=72,
            ),
        ]
        if query:
            needle = query.casefold()
            files = [item for item in files if needle in item.path.casefold()]
        if language:
            files = [item for item in files if item.language == language]
        files = files[:limit]
        return SourceListResponse(
            repository_id=repository_id,
            collection_name=f"repo_{repository_id}",
            total=len(files),
            files=files,
        )

    def get_source(self, repository_id: str, path: str) -> SourceDetailResponse:
        self.source_detail_calls.append({"repository_id": repository_id, "path": path})
        if self.source_error is not None:
            raise self.source_error
        if path == "src/payments/checkout.ts":
            language = "typescript"
            sections = [
                SourceSection(
                    chunk_id="checkout-section",
                    path=path,
                    language=language,
                    symbol="capturePayment",
                    symbol_kind="function",
                    start_line=20,
                    end_line=34,
                    parser="tree_sitter",
                    content=(
                        "export function capturePayment(order: Order) {\n"
                        "  return gateway.capture(order.authorization);\n"
                        "}"
                    ),
                )
            ]
        else:
            language = "python"
            sections = [
                SourceSection(
                    chunk_id="auth-section",
                    path="src/auth/service.py",
                    language=language,
                    symbol="authenticate_request",
                    symbol_kind="function",
                    start_line=18,
                    end_line=36,
                    parser="tree_sitter",
                    content=(
                        "def authenticate_request(request):\n"
                        "    token = '[REDACTED:ASSIGNMENT]'\n"
                        "    return sessions.verify(request, token)"
                    ),
                )
            ]
        return SourceDetailResponse(
            repository_id=repository_id,
            collection_name=f"repo_{repository_id}",
            path=path,
            language=language,
            sections=sections,
            truncated=False,
        )

    def reindex_repository(self, repository_id: str) -> RepositoryCreateResponse:
        self.reindex_calls.append(repository_id)
        return RepositoryCreateResponse(
            repository_id=repository_id,
            job_id="job-reindex",
            status=RepositoryStatus.QUEUED,
        )

    def delete_repository(self, repository_id: str) -> None:
        self.delete_calls.append(repository_id)
        self.repositories = [
            repository for repository in self.repositories if repository.id != repository_id
        ]

    def ask_question(
        self,
        repository_id: str,
        *,
        question: str,
        top_k: int = 8,
        history: list[Any] | None = None,
    ) -> QuestionResponse:
        if self.question_error is not None:
            raise self.question_error
        self.question_calls.append(
            {
                "repository_id": repository_id,
                "question": question,
                "top_k": top_k,
                "history": history or [],
            }
        )
        return QuestionResponse(
            answer="Authentication starts in `authenticate_request` and validates the session.",
            answer_mode="extractive",
            repository_id=repository_id,
            question=question,
            citations=[
                Citation(
                    source_id="source-1",
                    repository_id=repository_id,
                    commit_sha="a" * 40,
                    path="src/auth/service.py",
                    language="python",
                    symbol="authenticate_request",
                    symbol_kind="function",
                    start_line=18,
                    end_line=36,
                    score=0.943,
                    retrieval_signals=RetrievalSignals(
                        semantic_score=0.943,
                        combined_score=3.443,
                        path_overlap=1.0,
                        symbol_overlap=0.5,
                        content_overlap=0.5,
                    ),
                    excerpt="\n".join(
                        (
                            "def authenticate_request(request):",
                            "    return sessions.verify(request)",
                        )
                    ),
                    permalink=(
                        "https://github.com/acme/payments-service/blob/"
                        + "a" * 40
                        + "/src/auth/service.py#L18-L36"
                    ),
                )
            ],
        )


@pytest.fixture
def fake_client() -> FakeApiClient:
    return FakeApiClient()


@pytest.fixture
def run_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeApiClient], AppTest]:
    def _run(fake: FakeApiClient) -> AppTest:
        import codebase_intelligence.config as config_module
        import codebase_intelligence.ui.client as client_module

        settings = Settings(
            _env_file=None,
            environment="test",
            api_base_url="http://testserver",
            api_key=None,
            embedding_provider="deterministic",
            answer_provider="extractive",
            voyage_api_key=None,
            openai_api_key=None,
            max_archive_bytes=10 * 1024 * 1024,
        )
        monkeypatch.setattr(config_module, "get_settings", lambda: settings)
        monkeypatch.setattr(client_module, "ApiClient", lambda *_args, **_kwargs: fake)
        return AppTest.from_file(str(APP_FILE), default_timeout=8).run()

    return _run


def find_button(app: AppTest, label: str) -> Any:
    return next(button for button in app.button if button.label == label)


def find_text_input(app: AppTest, label: str) -> Any:
    return next(text_input for text_input in app.text_input if text_input.label == label)
