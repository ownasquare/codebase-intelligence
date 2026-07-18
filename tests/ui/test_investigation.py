"""Pure contract tests for bounded, repository-scoped investigation state."""

from __future__ import annotations

from codebase_intelligence.models import Citation, QuestionResponse
from codebase_intelligence.ui.investigation import (
    MAX_INVESTIGATIONS_PER_REPOSITORY,
    api_history,
    append_failure,
    append_success,
    clear_history,
    export_markdown,
    history_for,
    mark_stale,
)
from tests.ui.conftest import repository_record


def _response(repository_id: str, number: int = 1) -> QuestionResponse:
    return QuestionResponse(
        answer=f"Authentication is checked by the session boundary {number}.",
        answer_mode="extractive",
        repository_id=repository_id,
        question=f"Where is authentication {number}?",
        citations=[
            Citation(
                source_id=f"S{number}",
                repository_id=repository_id,
                path="src/auth/service.py",
                language="python",
                symbol="authenticate_request",
                symbol_kind="function",
                start_line=18,
                end_line=36,
                parser="tree_sitter",
                excerpt="def authenticate_request(request): ...",
            )
        ],
    )


def test_successful_history_is_repository_scoped_and_bounded() -> None:
    histories: dict[str, list[dict[str, object]]] = {}
    for number in range(MAX_INVESTIGATIONS_PER_REPOSITORY + 3):
        histories = append_success(histories, "repo-a", _response("repo-a", number))
    histories = append_success(histories, "repo-b", _response("repo-b", 99))

    repo_a = history_for(histories, "repo-a")
    repo_b = history_for(histories, "repo-b")

    assert len(repo_a) == MAX_INVESTIGATIONS_PER_REPOSITORY
    assert repo_a[0]["question"] == "Where is authentication 3?"
    assert [entry["question"] for entry in repo_b] == ["Where is authentication 99?"]


def test_api_history_includes_only_current_successful_pairs() -> None:
    histories: dict[str, list[dict[str, object]]] = {}
    histories = append_success(histories, "repo-a", _response("repo-a", 1))
    histories = append_failure(
        histories,
        "repo-a",
        question="Where is billing?",
        public_message="The request could not be completed.",
    )
    histories = append_success(histories, "repo-a", _response("repo-a", 2))
    stale_histories = mark_stale(histories, "repo-a")
    stale_histories = append_success(stale_histories, "repo-a", _response("repo-a", 3))

    projected = api_history(history_for(stale_histories, "repo-a"))

    assert [(message.role, message.content) for message in projected] == [
        ("user", "Where is authentication 3?"),
        ("assistant", "Authentication is checked by the session boundary 3."),
    ]
    assert all("billing" not in message.content.lower() for message in projected)


def test_clear_and_stale_operations_do_not_mutate_other_repositories() -> None:
    histories: dict[str, list[dict[str, object]]] = {}
    histories = append_success(histories, "repo-a", _response("repo-a"))
    histories = append_success(histories, "repo-b", _response("repo-b"))

    stale = mark_stale(histories, "repo-a")
    cleared = clear_history(stale, "repo-a")

    assert history_for(stale, "repo-a")[0]["stale"] is True
    assert history_for(histories, "repo-a")[0]["stale"] is False
    assert history_for(cleared, "repo-a") == []
    assert len(history_for(cleared, "repo-b")) == 1


def test_markdown_export_is_deterministic_bounded_and_omits_internal_errors() -> None:
    repository = repository_record()
    histories: dict[str, list[dict[str, object]]] = {}
    histories = append_success(histories, repository.id, _response(repository.id))
    histories = append_failure(
        histories,
        repository.id,
        question="Why did the provider fail?",
        public_message="RuntimeError: internal-provider-host secret detail",
    )

    first = export_markdown(repository, history_for(histories, repository.id))
    second = export_markdown(repository, history_for(histories, repository.id))

    assert first == second
    assert first.startswith("# payments-service investigation\n")
    assert "Indexed commit: `aaaaaaaaaaaa`" in first
    assert "Where is authentication 1?" in first
    assert "src/auth/service.py:18-36" in first
    assert "authenticate_request" in first
    assert "Why did the provider fail?" in first
    assert "Finding unavailable" in first
    assert "internal-provider-host" not in first
    assert len(first) <= 250_000
