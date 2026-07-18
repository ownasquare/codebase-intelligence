"""Contract tests for the Streamlit API client."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from codebase_intelligence.models import ChatMessage, RepositoryStatus
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
