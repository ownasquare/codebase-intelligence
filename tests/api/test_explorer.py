from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from codebase_intelligence.api.app import create_app
from codebase_intelligence.container import AppContainer
from tests.api.test_app import AUTH_HEADERS, _run_one_job, _settings, _zip_bytes

pytestmark = pytest.mark.integration


@pytest.fixture
def explorer_client(tmp_path: Path) -> Iterator[tuple[TestClient, AppContainer]]:
    settings = _settings(tmp_path / "explorer")
    container = AppContainer(settings, enable_inline_worker=False)
    app = create_app(settings, container=container)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, container


def _ready_repository(client: TestClient, container: AppContainer) -> str:
    response = client.post(
        "/api/v1/repositories/upload",
        headers=AUTH_HEADERS,
        files={"file": ("checkout.zip", _zip_bytes(), "application/zip")},
        data={"name": "checkout-service"},
    )
    assert response.status_code == 202
    repository_id = str(response.json()["repository_id"])
    assert _run_one_job(container, worker_id="explorer-worker") is True
    return repository_id


def test_explorer_routes_are_protected_and_require_ready_repository(
    explorer_client: tuple[TestClient, AppContainer],
) -> None:
    client, container = explorer_client
    upload = client.post(
        "/api/v1/repositories/upload",
        headers=AUTH_HEADERS,
        files={"file": ("checkout.zip", _zip_bytes(), "application/zip")},
    ).json()
    repository_id = str(upload["repository_id"])

    unauthorized = client.get(f"/api/v1/repositories/{repository_id}/sources")
    premature = client.get(
        f"/api/v1/repositories/{repository_id}/sources",
        headers=AUTH_HEADERS,
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "UNAUTHORIZED"
    assert premature.status_code == 409
    assert premature.json()["code"] == "INVALID_STATE"
    assert _run_one_job(container, worker_id="explorer-ready-worker") is True


def test_explorer_lists_and_reads_exact_indexed_source(
    explorer_client: tuple[TestClient, AppContainer],
) -> None:
    client, container = explorer_client
    repository_id = _ready_repository(client, container)

    listed = client.get(
        f"/api/v1/repositories/{repository_id}/sources",
        headers=AUTH_HEADERS,
        params={"q": "authenticate", "language": "python", "limit": 20},
    )
    detail = client.get(
        f"/api/v1/repositories/{repository_id}/source",
        headers=AUTH_HEADERS,
        params={"path": "src/auth.py"},
    )
    question = client.post(
        f"/api/v1/repositories/{repository_id}/questions",
        headers=AUTH_HEADERS,
        json={"question": "Where is authentication session validation?"},
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["repository_id"] == repository_id
    assert listed.json()["total"] == 1
    assert listed.json()["files"][0]["path"] == "src/auth.py"
    assert detail.status_code == 200, detail.text
    assert detail.json()["path"] == "src/auth.py"
    assert "authenticate_session" in detail.json()["sections"][0]["content"]
    assert detail.json()["truncated"] is False
    assert question.status_code == 200
    assert question.json()["citations"][0]["retrieval_signals"] is not None


def test_explorer_validates_filters_paths_and_repository_scope(
    explorer_client: tuple[TestClient, AppContainer],
) -> None:
    client, container = explorer_client
    repository_id = _ready_repository(client, container)
    base = f"/api/v1/repositories/{repository_id}"

    invalid_limit = client.get(
        f"{base}/sources",
        headers=AUTH_HEADERS,
        params={"limit": 0},
    )
    invalid_language = client.get(
        f"{base}/sources",
        headers=AUTH_HEADERS,
        params={"language": "python/../../"},
    )
    missing_source = client.get(
        f"{base}/source",
        headers=AUTH_HEADERS,
        params={"path": "src/missing.py"},
    )
    unsafe_path = client.get(
        f"{base}/source",
        headers=AUTH_HEADERS,
        params={"path": "../src/auth.py"},
    )
    missing_repository = client.get(
        "/api/v1/repositories/not-a-repository/sources",
        headers=AUTH_HEADERS,
    )

    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["code"] == "VALIDATION_ERROR"
    assert invalid_language.status_code == 422
    assert missing_source.status_code == 404
    assert missing_source.json()["code"] == "NOT_FOUND"
    assert unsafe_path.status_code == 422
    assert unsafe_path.json()["code"] == "INVALID_SOURCE_PATH"
    assert missing_repository.status_code == 404
