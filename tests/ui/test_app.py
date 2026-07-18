"""Streamlit AppTest coverage for core repository and chat states."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from streamlit.testing.v1 import AppTest

from codebase_intelligence.models import RepositoryStatus
from codebase_intelligence.ui.client import ApiError
from tests.ui.conftest import (
    FakeApiClient,
    find_button,
    find_text_input,
    repository_record,
)


def _all_visible_text(app: AppTest) -> str:
    collections = (
        app.title,
        app.header,
        app.subheader,
        app.markdown,
        app.caption,
        app.info,
        app.warning,
        app.error,
        app.success,
        app.code,
    )
    return "\n".join(str(element.value) for collection in collections for element in collection)


@pytest.mark.ui
def test_empty_state_guides_repository_ingestion(
    fake_client: FakeApiClient,
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    app = run_app(fake_client)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["GitHub repository", "ZIP upload"]
    text = _all_visible_text(app)
    assert "No repositories are indexed yet" in text
    assert "Repository code is treated as untrusted data" in text
    assert find_text_input(app, "Private repository token (optional)").value == ""


@pytest.mark.ui
def test_private_token_is_cleared_after_github_submission(
    fake_client: FakeApiClient,
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    app = run_app(fake_client)
    find_text_input(app, "GitHub repository URL").set_value("https://github.com/acme/private-repo")
    find_text_input(app, "Branch, tag, or commit (optional)").set_value("develop")
    find_text_input(app, "Private repository token (optional)").set_value("one-use-private-token")
    find_button(app, "Index GitHub repository").click().run()

    assert not app.exception
    assert fake_client.github_calls == [
        {
            "url": "https://github.com/acme/private-repo",
            "ref": "develop",
            "token": "one-use-private-token",
            "name": None,
        }
    ]
    assert find_text_input(app, "Private repository token (optional)").value == ""
    assert "Repository accepted" in _all_visible_text(app)


@pytest.mark.ui
def test_active_repository_renders_polled_job_progress(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient(
        [repository_record(status=RepositoryStatus.INDEXING, repository_id="repo-created")]
    )
    app = run_app(fake)
    app.session_state["repository_jobs"] = {"repo-created": "job-created"}
    app.run()

    assert not app.exception
    text = _all_visible_text(app)
    assert "being indexed" in text
    assert "Embedding · 64% complete" in text
    assert not app.chat_input


@pytest.mark.ui
def test_failed_repository_renders_recovery_and_reindex(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient(
        [
            repository_record(
                status=RepositoryStatus.FAILED,
                error_message="Parser could not produce safe chunks.",
            )
        ]
    )
    app = run_app(fake)

    assert "Parser could not produce safe chunks" in _all_visible_text(app)
    find_button(app, "Reindex repository").click().run()

    assert not app.exception
    assert fake.reindex_calls == ["repo-1"]
    assert "fresh index has been queued" in _all_visible_text(app)


@pytest.mark.ui
def test_ready_repository_chat_renders_grounded_source_card(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)

    assert app.chat_input
    app.chat_input[0].set_value("Where is authentication?").run()

    assert not app.exception
    assert fake.question_calls[0]["repository_id"] == "repo-1"
    text = _all_visible_text(app)
    assert "Authentication starts" in text
    assert "src/auth/service.py" in text
    assert "authenticate_request" in text
    assert "lines 18-36" in text
    assert "Score 0.943" in text
    assert any(code.value.startswith("def authenticate_request") for code in app.code)


@pytest.mark.ui
def test_delete_requires_confirmation_and_removes_repository(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)

    find_button(app, "Delete repository").click().run()
    assert "cannot be undone" in _all_visible_text(app)
    assert fake.delete_calls == []

    find_button(app, "Delete permanently").click().run()

    assert not app.exception
    assert fake.delete_calls == ["repo-1"]
    assert "was removed from this workspace" in _all_visible_text(app)
    assert "No repositories are indexed yet" in _all_visible_text(app)


@pytest.mark.ui
def test_api_failure_renders_sanitized_retryable_state(
    fake_client: FakeApiClient,
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake_client.list_error = ApiError(
        "The API is unavailable. Check that the service is running and try again.",
        code="unavailable",
    )
    app = run_app(fake_client)

    assert not app.exception
    text = _all_visible_text(app)
    assert "API is unavailable" in text
    assert "actions will return" in text


def test_app_never_enables_unsafe_markdown_html() -> None:
    from codebase_intelligence.ui import app as app_module

    source = app_module.__loader__.get_source(app_module.__name__)
    assert source is not None
    assert "unsafe_allow_html" not in source
