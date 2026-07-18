"""Streamlit AppTest coverage for the public repository workbench."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

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


def _view(app: AppTest, label: str) -> AppTest:
    navigation = next(radio for radio in app.radio if radio.label == "Workspace view")
    navigation.set_value(label)
    return app.run()


def _question_input(app: AppTest):  # type: ignore[no-untyped-def]
    return next(area for area in app.text_area if area.label == "Question")


@pytest.mark.ui
def test_empty_state_leads_with_import_and_clears_private_token(
    fake_client: FakeApiClient,
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    app = run_app(fake_client)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["GitHub", "ZIP upload"]
    assert "Add your first repository" in _all_visible_text(app)
    assert "never executed" in _all_visible_text(app)

    find_text_input(app, "GitHub repository URL").set_value("https://github.com/acme/private-repo")
    find_text_input(app, "Branch, tag, or commit (optional)").set_value("develop")
    find_text_input(app, "Private repository token (optional)").set_value("one-use-token")
    find_button(app, "Add GitHub repository").click().run()

    assert fake_client.github_calls[0]["token"] == "one-use-token"
    assert find_text_input(app, "Private repository token (optional)").value == ""
    assert "Repository accepted" in _all_visible_text(app)


@pytest.mark.ui
def test_returning_workspace_is_compact_and_import_is_collapsed(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    app = run_app(FakeApiClient([repository_record()]))

    assert not app.exception
    assert [title.value for title in app.title] == ["Codebase Intelligence"]
    assert any(selectbox.label == "Repository" for selectbox in app.selectbox)
    navigation = next(radio for radio in app.radio if radio.label == "Workspace view")
    assert navigation.options == ["Ask", "Source", "Repository"]
    assert navigation.value == "Ask"
    add_repository = next(
        expander for expander in app.expander if expander.label == "Add repository"
    )
    assert add_repository.proto.expanded is False
    system = next(expander for expander in app.expander if expander.label == "System")
    assert system.proto.expanded is False
    text = _all_visible_text(app)
    assert "payments-service" in text
    assert "Find evidence" in [button.label for button in app.button]
    assert not app.chat_input


@pytest.mark.ui
def test_switching_repositories_returns_to_ask(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient(
        [
            repository_record(repository_id="repo-1", name="payments-service"),
            repository_record(repository_id="repo-2", name="identity-service"),
        ]
    )
    app = run_app(fake)
    app = _view(app, "Source")
    repository_selector = next(
        selectbox for selectbox in app.selectbox if selectbox.label == "Repository"
    )
    repository_selector.set_value("repo-2").run()

    navigation = next(radio for radio in app.radio if radio.label == "Workspace view")
    assert navigation.value == "Ask"
    assert app.session_state["selected_repository_id"] == "repo-2"
    assert "identity-service" in _all_visible_text(app)


@pytest.mark.ui
def test_added_repository_wins_over_the_existing_selector_value(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    app = _view(app, "Repository")
    find_text_input(app, "GitHub repository URL").set_value(
        "https://github.com/acme/new-repository"
    )
    find_button(app, "Add GitHub repository").click().run()

    repository_selector = next(
        selectbox for selectbox in app.selectbox if selectbox.label == "Repository"
    )
    assert repository_selector.value == "repo-created"
    assert app.session_state["selected_repository_id"] == "repo-created"
    assert app.session_state["workspace_view"] == "Ask"
    assert "new-repository" in _all_visible_text(app)


@pytest.mark.ui
def test_active_repository_uses_durable_job_progress_without_ready_workbench_polling(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient(
        [repository_record(status=RepositoryStatus.INDEXING, repository_id="repo-created")]
    )
    app = run_app(fake)

    text = _all_visible_text(app)
    assert not app.exception
    assert "Embedding" in text
    assert app.get("progress")[0].value == 64
    assert fake.list_jobs_calls[-1]["repository_id"] == "repo-created"
    assert not app.text_area


@pytest.mark.ui
def test_successful_investigation_renders_neutral_finding_and_match_reasons(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    _question_input(app).set_value("Where is authentication?")
    find_button(app, "Find evidence").click().run()

    text = _all_visible_text(app)
    assert not app.exception
    assert fake.question_calls[0]["history"] == []
    assert "Question" in text
    assert "Finding" in text
    assert "Authentication starts" in text
    assert any("src/auth/service.py" in expander.label for expander in app.expander)
    assert "Path match" in text
    assert "Symbol match" in text
    assert "Combined" not in text
    assert "View evidence 1.1 in Source" in [button.label for button in app.button]
    assert not app.chat_message


@pytest.mark.ui
def test_blank_question_warns_without_calling_the_api(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    find_button(app, "Find evidence").click().run()

    assert fake.question_calls == []
    assert "Enter a question" in _all_visible_text(app)


@pytest.mark.ui
def test_example_question_only_prefills_the_question(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    find_button(app, "Where is authentication enforced?").click().run()

    assert fake.question_calls == []
    assert _question_input(app).value == "Where is authentication enforced?"
    examples = next(expander for expander in app.expander if expander.label == "Example questions")
    assert examples.proto.expanded is False


@pytest.mark.ui
def test_failed_finding_is_visible_but_excluded_from_later_api_history(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    fake.question_error = ApiError("The answer service is temporarily unavailable.")
    app = run_app(fake)
    _question_input(app).set_value("Where is billing?")
    find_button(app, "Find evidence").click().run()

    assert "temporarily unavailable" in _all_visible_text(app)
    fake.question_error = None
    _question_input(app).set_value("Where is authentication?")
    find_button(app, "Find evidence").click().run()

    assert len(fake.question_calls) == 1
    assert fake.question_calls[0]["history"] == []


@pytest.mark.ui
def test_investigation_can_be_exported_and_cleared(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    app = run_app(FakeApiClient([repository_record()]))
    _question_input(app).set_value("Where is authentication?")
    find_button(app, "Find evidence").click().run()

    assert app.get("download_button")
    assert app.get("download_button")[0].label == "Download Markdown"
    find_button(app, "Clear findings").click().run()

    assert "Findings will appear here" in _all_visible_text(app)


@pytest.mark.ui
def test_reindex_marks_existing_findings_stale(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    _question_input(app).set_value("Where is authentication?")
    find_button(app, "Find evidence").click().run()
    _view(app, "Repository")
    find_button(app, "Reindex repository").click().run()
    _view(app, "Ask")

    assert fake.reindex_calls == ["repo-1"]
    assert "earlier repository index" in _all_visible_text(app)


@pytest.mark.ui
def test_explore_filters_and_renders_indexed_redacted_source(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    _view(app, "Source")
    find_text_input(app, "Search indexed files").set_value("auth").run()

    text = _all_visible_text(app)
    assert not app.exception
    assert fake.source_list_calls[-1]["query"] == "auth"
    assert fake.source_detail_calls[-1] == {
        "repository_id": "repo-1",
        "path": "src/auth/service.py",
    }
    assert "Indexed, redacted preview" in text
    assert "authenticate_request" in text
    assert "[REDACTED:ASSIGNMENT]" in text


@pytest.mark.ui
def test_citation_opens_the_exact_path_in_source(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    _question_input(app).set_value("Where is authentication?")
    find_button(app, "Find evidence").click().run()
    find_button(app, "View evidence 1.1 in Source").click().run()

    navigation = next(radio for radio in app.radio if radio.label == "Workspace view")
    assert navigation.value == "Source"
    assert fake.source_detail_calls[-1]["path"] == "src/auth/service.py"
    assert app.session_state["source_line_repo-1"] == 18


@pytest.mark.ui
def test_repository_view_keeps_maintenance_collapsed_and_out_of_ask(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    assert "Delete repository" not in [button.label for button in app.button]

    _view(app, "Repository")
    repository_view = _all_visible_text(app)
    assert "Repository source" in repository_view
    assert [metric.label for metric in app.metric] == [
        "Files",
        "Sections",
        "Indexed size",
        "Redactions",
    ]
    maintenance = next(expander for expander in app.expander if expander.label == "Maintenance")
    assert maintenance.proto.expanded is False
    labels = [button.label for button in app.button]
    assert "Reindex repository" in labels
    assert "Delete repository" in labels


@pytest.mark.ui
def test_delete_requires_confirmation_and_removes_repository(
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake = FakeApiClient([repository_record()])
    app = run_app(fake)
    _view(app, "Repository")
    find_button(app, "Delete repository").click().run()

    assert "cannot be undone" in _all_visible_text(app)
    assert fake.delete_calls == []
    find_button(app, "Delete permanently").click().run()

    assert fake.delete_calls == ["repo-1"]
    assert "Add your first repository" in _all_visible_text(app)


@pytest.mark.ui
def test_offline_state_remains_actionable_and_safe(
    fake_client: FakeApiClient,
    run_app: Callable[[FakeApiClient], AppTest],
) -> None:
    fake_client.list_error = ApiError(
        "The API is unavailable. Check that the service is running and try again.",
        code="unavailable",
    )
    app = run_app(fake_client)

    text = _all_visible_text(app)
    assert not app.exception
    assert "Workspace unavailable" in text
    assert "API is unavailable" in text
    assert "Refresh" in [button.label for button in app.button]


def test_app_and_design_avoid_ai_styling_and_unsafe_html() -> None:
    from codebase_intelligence.ui import app as app_module
    from codebase_intelligence.ui.design import APP_STYLES

    source = app_module.__loader__.get_source(app_module.__name__)
    assert source is not None
    assert "unsafe_allow_html" not in source
    assert "st.chat_message" not in source
    assert 'WORKSPACE_VIEWS = ("Ask", "Source", "Repository")' in source
    assert source.index("for index, entry in enumerate(reversed(entries), start=1)") < source.index(
        'with st.expander("Save or clear findings"'
    )
    assert "gradient" not in APP_STYLES.casefold()
    assert "robot" not in APP_STYLES.casefold()
    assert "focus-visible" in APP_STYLES
    assert "focus-within" in APP_STYLES
    assert "border: 1px solid var(--ci-line) !important" in APP_STYLES
    assert "overflow-x: auto" in APP_STYLES

    config_path = Path(__file__).parents[2] / ".streamlit" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["server"]["enableXsrfProtection"] is True
    assert config["server"]["enableCORS"] is True
    assert config["client"]["showErrorDetails"] is False
