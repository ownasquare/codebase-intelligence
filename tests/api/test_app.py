from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from codebase_intelligence.api.app import create_app
from codebase_intelligence.config import Settings
from codebase_intelligence.container import AppContainer
from codebase_intelligence.models import JobStatus, RepositoryStatus
from codebase_intelligence.worker import JobWorker

pytestmark = pytest.mark.integration

API_KEY = "integration-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}


def _settings(data_dir: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "data_dir": data_dir,
        "embedding_provider": "deterministic",
        "deterministic_embedding_dimension": 128,
        "answer_provider": "extractive",
        "inline_worker": False,
        "worker_poll_seconds": 0.1,
        "worker_lease_seconds": 30,
        "max_archive_bytes": 1024 * 1024,
        "max_extracted_bytes": 4 * 1024 * 1024,
        "max_indexable_bytes": 4 * 1024 * 1024,
        "max_file_bytes": 1024 * 1024,
        "api_key": API_KEY,
    }
    values.update(overrides)
    return Settings(**values)


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "checkout-main/src/auth.py",
            (
                "def authenticate_session(session_id: str) -> bool:\n"
                '    """Validate an authenticated checkout session."""\n'
                "    return session_id.startswith('session-')\n"
            ),
        )
        archive.writestr(
            "checkout-main/src/payment.py",
            (
                "def process_payment(amount: int, gateway) -> str:\n"
                '    """Authorize and capture the checkout payment."""\n'
                "    authorization = gateway.authorize(amount)\n"
                "    return gateway.capture(authorization)\n"
            ),
        )
    return buffer.getvalue()


@pytest.fixture
def api_client(tmp_path: Path) -> Iterator[tuple[TestClient, AppContainer]]:
    settings = _settings(tmp_path / "runtime")
    container = AppContainer(settings, enable_inline_worker=False)
    app = create_app(settings, container=container)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, container


def _run_one_job(container: AppContainer, *, worker_id: str) -> bool:
    assert container.ingestion_service is not None
    worker = JobWorker(
        container.jobs,
        container.ingestion_service,
        poll_seconds=0.1,
        lease_seconds=30,
        worker_id=worker_id,
    )
    return asyncio.run(worker.run_once())


def test_public_probes_and_protected_routes_enforce_api_key(
    api_client: tuple[TestClient, AppContainer],
) -> None:
    client, _ = api_client

    root = client.get("/")
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")
    unauthorized = client.get("/api/v1/status")
    authorized = client.get("/api/v1/status", headers=AUTH_HEADERS)
    protected_schema = client.get("/api/v1/openapi.json", headers=AUTH_HEADERS)

    assert root.status_code == 200
    assert live.status_code == 200 and live.json()["checks"] == {"process": True}
    assert ready.status_code == 200 and all(ready.json()["checks"].values())
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "API-Key"
    assert unauthorized.json()["code"] == "UNAUTHORIZED"
    assert authorized.status_code == 200
    assert authorized.json()["embedding"]["mode"] == "demo"
    assert authorized.json()["answer"]["mode"] == "demo"
    assert protected_schema.status_code == 200
    assert protected_schema.json()["info"]["title"] == "Codebase Intelligence"
    assert root.headers["x-content-type-options"] == "nosniff"
    assert root.headers["x-request-id"]


def test_zip_upload_to_question_reindex_and_delete_readback(
    api_client: tuple[TestClient, AppContainer],
) -> None:
    client, container = api_client

    submitted = client.post(
        "/api/v1/repositories/upload",
        headers=AUTH_HEADERS,
        files={"file": ("checkout.zip", _zip_bytes(), "application/zip")},
        data={"name": "checkout-service"},
    )
    assert submitted.status_code == 202, submitted.text
    submitted_body = submitted.json()
    repository_id = submitted_body["repository_id"]
    ingest_job_id = submitted_body["job_id"]
    assert submitted_body["status"] == RepositoryStatus.QUEUED

    queued_repository = client.get(f"/api/v1/repositories/{repository_id}", headers=AUTH_HEADERS)
    queued_job = client.get(f"/api/v1/jobs/{ingest_job_id}", headers=AUTH_HEADERS)
    premature_question = client.post(
        f"/api/v1/repositories/{repository_id}/questions",
        headers=AUTH_HEADERS,
        json={"question": "Where is authentication implemented?"},
    )
    assert queued_repository.json()["status"] == RepositoryStatus.QUEUED
    assert queued_job.json()["status"] == JobStatus.QUEUED
    assert premature_question.status_code == 409
    assert premature_question.json()["code"] == "INVALID_STATE"

    assert _run_one_job(container, worker_id="api-ingest-worker") is True

    completed_job = client.get(f"/api/v1/jobs/{ingest_job_id}", headers=AUTH_HEADERS)
    ready_repository = client.get(f"/api/v1/repositories/{repository_id}", headers=AUTH_HEADERS)
    assert completed_job.status_code == 200
    assert completed_job.json()["status"] == JobStatus.SUCCEEDED
    assert completed_job.json()["progress"] == 100
    assert ready_repository.json()["status"] == RepositoryStatus.READY
    assert ready_repository.json()["stats"]["file_count"] == 2
    assert ready_repository.json()["stats"]["chunk_count"] >= 2

    question = client.post(
        f"/api/v1/repositories/{repository_id}/questions",
        headers=AUTH_HEADERS,
        json={"question": "Where is authentication session validation?", "top_k": 8},
    )
    assert question.status_code == 200, question.text
    answer = question.json()
    assert answer["answer_mode"] == "extractive"
    assert answer["repository_id"] == repository_id
    assert answer["citations"]
    assert any(citation["path"] == "src/auth.py" for citation in answer["citations"])
    assert all(citation["repository_id"] == repository_id for citation in answer["citations"])

    reindex = client.post(f"/api/v1/repositories/{repository_id}/reindex", headers=AUTH_HEADERS)
    assert reindex.status_code == 202
    reindex_job_id = reindex.json()["job_id"]
    assert _run_one_job(container, worker_id="api-reindex-worker") is True
    assert (
        client.get(f"/api/v1/jobs/{reindex_job_id}", headers=AUTH_HEADERS).json()["status"]
        == JobStatus.SUCCEEDED
    )

    deleted = client.delete(f"/api/v1/repositories/{repository_id}", headers=AUTH_HEADERS)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (
        client.get(f"/api/v1/repositories/{repository_id}", headers=AUTH_HEADERS).status_code == 404
    )
    assert client.get(f"/api/v1/jobs/{ingest_job_id}", headers=AUTH_HEADERS).status_code == 404
    assert all(
        repository["id"] != repository_id
        for repository in client.get("/api/v1/repositories", headers=AUTH_HEADERS).json()
    )


def test_readiness_is_503_when_embedding_provider_is_unconfigured(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path / "degraded",
        embedding_provider="voyage",
        voyage_api_key=None,
        answer_provider="extractive",
        api_key=None,
    )
    container = AppContainer(settings, enable_inline_worker=False)
    app = create_app(settings, container=container)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["database"] is True
    assert response.json()["checks"]["embedding"] is False
    assert response.json()["checks"]["qdrant"] is False


def test_upload_limits_and_request_validation_return_stable_problems(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "limits", max_archive_bytes=1024)
    container = AppContainer(settings, enable_inline_worker=False)
    app = create_app(settings, container=container)

    with TestClient(app, raise_server_exceptions=False) as client:
        oversized_archive = client.post(
            "/api/v1/repositories/upload",
            headers=AUTH_HEADERS,
            files={"file": ("large.zip", b"x" * 1025, "application/zip")},
        )
        hard_body_limit = client.post(
            "/api/v1/repositories/upload",
            headers={**AUTH_HEADERS, "Content-Type": "application/octet-stream"},
            content=b"x" * (1024 + 1024 * 1024 + 1),
        )
        invalid_github_body = client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADERS,
            json={"url": "not-a-repository"},
        )
        missing_upload = client.post(
            "/api/v1/repositories/upload",
            headers=AUTH_HEADERS,
            data={"name": "missing-file"},
        )

    assert oversized_archive.status_code == 413
    assert oversized_archive.json()["code"] == "ARCHIVE_TOO_LARGE"
    assert hard_body_limit.status_code == 413
    assert hard_body_limit.json()["code"] == "REQUEST_TOO_LARGE"
    assert invalid_github_body.status_code == 422
    assert invalid_github_body.json()["code"] == "VALIDATION_ERROR"
    assert missing_upload.status_code == 422
    assert missing_upload.json()["code"] == "VALIDATION_ERROR"
    assert container.repositories.list_repositories() == []


def test_job_listing_and_queued_cancellation_are_atomic(
    api_client: tuple[TestClient, AppContainer],
) -> None:
    client, _ = api_client
    submitted = client.post(
        "/api/v1/repositories/upload",
        headers=AUTH_HEADERS,
        files={"file": ("checkout.zip", _zip_bytes(), "application/zip")},
    ).json()
    job_id = submitted["job_id"]
    repository_id = submitted["repository_id"]

    listed = client.get(
        "/api/v1/jobs",
        headers=AUTH_HEADERS,
        params={"repository_id": repository_id, "status": "queued"},
    )
    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=AUTH_HEADERS)
    repeated = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=AUTH_HEADERS)

    assert listed.status_code == 200
    assert [job["id"] for job in listed.json()] == [job_id]
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == JobStatus.CANCELLED
    assert repeated.status_code == 409
    assert repeated.json()["code"] == "INVALID_STATE"
