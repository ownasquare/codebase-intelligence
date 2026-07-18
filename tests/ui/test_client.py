"""Contract tests for the Streamlit API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from codebase_intelligence.models import ChatMessage, JobStatus, RepositoryStatus
from codebase_intelligence.ui.client import ApiClient, ApiError

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC).isoformat()


def _repository_payload(repository_id: str = "repo-1") -> dict[str, object]:
    return {
        "id": repository_id,
        "name": "example",
        "status": "ready",
        "source_kind": "github",
        "source_url": "https://github.com/acme/example",
        "source_ref": "main",
        "commit_sha": "a" * 40,
        "collection_name": f"repo_{repository_id}",
        "index_fingerprint": "fingerprint",
        "stats": {"file_count": 3, "chunk_count": 9},
        "error_code": None,
        "error_message": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _create_payload(repository_id: str = "repo-1") -> dict[str, str]:
    return {"repository_id": repository_id, "job_id": "job-1", "status": "queued"}


def _job_payload(job_id: str = "job-1") -> dict[str, object]:
    return {
        "id": job_id,
        "repository_id": "repo-1",
        "kind": "ingest",
        "status": "running",
        "stage": "embedding",
        "progress": 64,
        "attempt": 1,
        "payload": {},
        "error_code": None,
        "error_message": None,
        "lease_owner": "worker-1",
        "lease_expires_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
        "started_at": NOW,
        "completed_at": None,
    }


def _source_list_payload() -> dict[str, object]:
    return {
        "repository_id": "repo-1",
        "collection_name": "repo_repo1_active",
        "total": 1,
        "files": [
            {
                "path": "src/auth/session.py",
                "language": "python",
                "chunk_count": 2,
                "symbol_count": 2,
                "start_line": 1,
                "end_line": 48,
            }
        ],
    }


def _source_detail_payload() -> dict[str, object]:
    return {
        "repository_id": "repo-1",
        "collection_name": "repo_repo1_active",
        "path": "src/auth/session flow.py",
        "language": "python",
        "sections": [
            {
                "chunk_id": "chunk-authenticate",
                "path": "src/auth/session flow.py",
                "language": "python",
                "symbol": "authenticate_request",
                "symbol_kind": "function",
                "start_line": 12,
                "end_line": 29,
                "parser": "tree_sitter",
                "content": (
                    "def authenticate_request(request):\n    return sessions.verify(request)"
                ),
            }
        ],
        "truncated": False,
    }


def test_github_token_is_single_request_header_and_not_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(202, json=_create_payload())

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    created = client.create_github_repository(
        url="https://github.com/acme/private-repo",
        ref="develop",
        token="private-token-value",
    )

    assert created.status == RepositoryStatus.QUEUED
    assert captured["json"] == {
        "url": "https://github.com/acme/private-repo",
        "ref": "develop",
    }
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-github-token"] == "private-token-value"
    assert "private-token-value" not in str(captured["url"])
    assert "private-token-value" not in json.dumps(captured["json"])


def test_upload_uses_sanitized_leaf_filename_and_multipart() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.content
        return httpx.Response(202, json=_create_payload("repo-upload"))

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    created = client.upload_repository(
        filename="../../unsafe/repository.zip",
        content=b"PK\x03\x04fixture",
        name="Fixture",
    )

    assert created.repository_id == "repo-upload"
    assert str(captured["content_type"]).startswith("multipart/form-data;")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b"repository.zip" in body
    assert b"../../unsafe" not in body
    assert b"PK\x03\x04fixture" in body


def test_repository_list_accepts_wrapped_api_shape() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"repositories": [_repository_payload()]})
    )
    client = ApiClient("http://testserver", transport=transport)

    repositories = client.list_repositories()

    assert len(repositories) == 1
    assert repositories[0].stats.chunk_count == 9


def test_question_request_contains_bounded_history_and_parses_sources() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answer": "The auth flow starts in src/auth.py.",
                "answer_mode": "extractive",
                "citations": [
                    {
                        "source_id": "source-1",
                        "repository_id": "repo-1",
                        "commit_sha": "a" * 40,
                        "path": "src/auth.py",
                        "language": "python",
                        "symbol": "authenticate",
                        "symbol_kind": "function",
                        "start_line": 4,
                        "end_line": 12,
                        "score": 0.9,
                        "excerpt": "def authenticate(): ...",
                        "permalink": None,
                    }
                ],
                "repository_id": "repo-1",
                "question": "Where is authentication?",
            },
        )

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    answer = client.ask_question(
        "repo-1",
        question="Where is authentication?",
        history=[ChatMessage(role="user", content="Show the request boundary")],
    )

    assert answer.citations[0].path == "src/auth.py"
    assert captured["top_k"] == 8
    assert captured["history"] == [{"role": "user", "content": "Show the request boundary"}]


def test_source_list_encodes_optional_filters_and_parses_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=_source_list_payload())

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    sources = client.list_sources(
        "repo-1",
        query=" auth & session ",
        language=" python ",
        limit=25,
    )

    assert captured["path"] == "/api/v1/repositories/repo-1/sources"
    assert captured["params"] == {
        "q": "auth & session",
        "language": "python",
        "limit": "25",
    }
    assert sources.repository_id == "repo-1"
    assert sources.total == 1
    assert sources.files[0].path == "src/auth/session.py"
    assert sources.files[0].chunk_count == 2


def test_source_detail_uses_query_parameter_for_nested_path() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=_source_detail_payload())

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    source = client.get_source("repo-1", "src/auth/session flow.py")

    assert captured["path"] == "/api/v1/repositories/repo-1/source"
    assert captured["params"] == {"path": "src/auth/session flow.py"}
    assert source.path == "src/auth/session flow.py"
    assert source.sections[0].chunk_id == "chunk-authenticate"
    assert source.sections[0].content.startswith("def authenticate_request")


def test_source_list_uses_shared_timeout_handling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request timed out", request=request)

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as raised:
        client.list_sources("repo-1")

    assert raised.value.code == "timeout"
    assert raised.value.message == "The API took too long to respond. Try again in a moment."


def test_source_detail_uses_sanitized_problem_details() -> None:
    exposed = "github_pat_abcdefghijklmnopqrstuvwxyz123456"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            404,
            json={
                "title": "Not Found",
                "status": 404,
                "detail": f"Source was not found; X-GitHub-Token: {exposed}",
                "code": "SOURCE_NOT_FOUND",
                "request_id": "request-source-1",
            },
        )
    )
    client = ApiClient("http://testserver", transport=transport)

    with pytest.raises(ApiError) as raised:
        client.get_source("repo-1", "src/missing.py")

    assert raised.value.status_code == 404
    assert raised.value.code == "SOURCE_NOT_FOUND"
    assert raised.value.request_id == "request-source-1"
    assert exposed not in raised.value.message
    assert "[redacted]" in raised.value.message


def test_job_list_parses_existing_endpoint_and_encodes_filters() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=[_job_payload()])

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))
    jobs = client.list_jobs(
        repository_id="repo-1",
        status=JobStatus.RUNNING,
        limit=10,
        offset=5,
    )

    assert captured["path"] == "/api/v1/jobs"
    assert captured["params"] == {
        "repository_id": "repo-1",
        "status": "running",
        "limit": "10",
        "offset": "5",
    }
    assert len(jobs) == 1
    assert jobs[0].id == "job-1"
    assert jobs[0].status is JobStatus.RUNNING


def test_problem_details_are_sanitized_before_display() -> None:
    exposed = "github_pat_abcdefghijklmnopqrstuvwxyz123456"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            403,
            json={
                "title": "Forbidden",
                "status": 403,
                "detail": f"X-GitHub-Token: {exposed} was rejected",
                "code": "github_forbidden",
                "request_id": "request-1",
            },
        )
    )
    client = ApiClient("http://testserver", transport=transport)

    with pytest.raises(ApiError) as raised:
        client.list_repositories()

    assert exposed not in raised.value.message
    assert "[redacted]" in raised.value.message
    assert raised.value.code == "github_forbidden"
    assert raised.value.request_id == "request-1"


def test_transport_errors_do_not_echo_request_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection failed for http://user:password@testserver/?token=do-not-display",
            request=request,
        )

    client = ApiClient("http://testserver", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as raised:
        client.health()

    assert raised.value.code == "unavailable"
    assert "password" not in raised.value.message
    assert "do-not-display" not in raised.value.message


def test_default_timeouts_are_finite_and_bounded() -> None:
    client = ApiClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    assert client.timeout.connect == 5.0
    assert client.timeout.pool == 5.0
    assert client.timeout.read == 105.0
    assert client.timeout.write == 120.0
