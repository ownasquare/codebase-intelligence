"""Bounded, repository-scoped investigation state for the Streamlit workbench."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from codebase_intelligence.models import ChatMessage, Citation, QuestionResponse, RepositoryRecord

MAX_INVESTIGATIONS_PER_REPOSITORY = 24
MAX_HISTORY_MESSAGES = 12
MAX_EXPORT_CHARS = 250_000


class InvestigationEntry(TypedDict):
    """One visible question/finding attempt stored in session state."""

    question: str
    answer: str
    answer_mode: str | None
    citations: list[dict[str, Any]]
    error: bool
    stale: bool


Histories = dict[str, list[InvestigationEntry]]


def _entry(value: object) -> InvestigationEntry | None:
    if not isinstance(value, Mapping):
        return None
    question = value.get("question")
    answer = value.get("answer")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(answer, str):
        return None
    raw_citations = value.get("citations", [])
    citations: list[dict[str, Any]] = []
    if isinstance(raw_citations, list):
        for item in raw_citations[:20]:
            try:
                citation = Citation.model_validate(item)
            except (TypeError, ValueError):
                continue
            citations.append(citation.model_dump(mode="json"))
    raw_mode = value.get("answer_mode")
    answer_mode = raw_mode if isinstance(raw_mode, str) else None
    return InvestigationEntry(
        question=question.strip()[:4000],
        answer=answer.strip()[:12_000],
        answer_mode=answer_mode,
        citations=citations,
        error=bool(value.get("error", False)),
        stale=bool(value.get("stale", False)),
    )


def normalize_histories(value: object) -> Histories:
    """Return a defensive, bounded copy of arbitrary Streamlit session state."""

    if not isinstance(value, Mapping):
        return {}
    histories: Histories = {}
    for repository_id, raw_entries in value.items():
        if not isinstance(repository_id, str) or not isinstance(raw_entries, Sequence):
            continue
        entries = [entry for item in raw_entries if (entry := _entry(item)) is not None]
        histories[repository_id] = entries[-MAX_INVESTIGATIONS_PER_REPOSITORY:]
    return histories


def history_for(histories: object, repository_id: str) -> list[InvestigationEntry]:
    """Read one isolated history as a copy safe for UI rendering."""

    return list(normalize_histories(histories).get(repository_id, []))


def _replace(
    histories: object,
    repository_id: str,
    entries: Sequence[InvestigationEntry],
) -> Histories:
    updated = normalize_histories(histories)
    updated[repository_id] = list(entries)[-MAX_INVESTIGATIONS_PER_REPOSITORY:]
    return updated


def append_success(
    histories: object,
    repository_id: str,
    response: QuestionResponse,
) -> Histories:
    """Append one successful finding and keep only the newest bounded records."""

    entries = history_for(histories, repository_id)
    entries.append(
        InvestigationEntry(
            question=response.question.strip(),
            answer=response.answer.strip(),
            answer_mode=response.answer_mode,
            citations=[citation.model_dump(mode="json") for citation in response.citations],
            error=False,
            stale=False,
        )
    )
    return _replace(histories, repository_id, entries)


def append_failure(
    histories: object,
    repository_id: str,
    *,
    question: str,
    public_message: str,
) -> Histories:
    """Append a visible safe failure without retaining exception objects or internals."""

    entries = history_for(histories, repository_id)
    entries.append(
        InvestigationEntry(
            question=question.strip()[:4000],
            answer=public_message.strip()[:500],
            answer_mode=None,
            citations=[],
            error=True,
            stale=False,
        )
    )
    return _replace(histories, repository_id, entries)


def clear_history(histories: object, repository_id: str) -> Histories:
    """Clear one repository without changing another repository's history."""

    updated = normalize_histories(histories)
    updated.pop(repository_id, None)
    return updated


def mark_stale(histories: object, repository_id: str) -> Histories:
    """Mark existing findings as belonging to a superseded index."""

    entries = history_for(histories, repository_id)
    stale_entries = [InvestigationEntry(**{**entry, "stale": True}) for entry in entries]
    return _replace(histories, repository_id, stale_entries)


def api_history(
    entries: Sequence[InvestigationEntry],
    *,
    max_messages: int = MAX_HISTORY_MESSAGES,
) -> list[ChatMessage]:
    """Project only successful current findings into bounded answer-provider history."""

    messages: list[ChatMessage] = []
    for entry in entries:
        if entry["error"] or entry["stale"]:
            continue
        messages.extend(
            (
                ChatMessage(role="user", content=entry["question"]),
                ChatMessage(role="assistant", content=entry["answer"]),
            )
        )
    bounded = max(0, min(max_messages, MAX_HISTORY_MESSAGES))
    return messages[-bounded:] if bounded else []


def _inline_code(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ").strip()


def export_markdown(
    repository: RepositoryRecord,
    entries: Sequence[InvestigationEntry],
) -> str:
    """Build deterministic, portable evidence notes without failure internals."""

    lines = [
        f"# {repository.name} investigation",
        "",
        f"Repository ID: `{_inline_code(repository.id)}`",
        f"Source: {repository.source_url or repository.source_kind.value}",
    ]
    if repository.source_ref:
        lines.append(f"Reference: `{_inline_code(repository.source_ref)}`")
    if repository.commit_sha:
        lines.append(f"Indexed commit: `{_inline_code(repository.commit_sha[:12])}`")
    lines.extend(("", "---", ""))

    if not entries:
        lines.append("No investigation findings were recorded.")

    for index, entry in enumerate(entries, start=1):
        lines.extend((f"## {index}. {entry['question']}", ""))
        if entry["stale"]:
            lines.extend(("> This finding belongs to an earlier repository index.", ""))
        if entry["error"]:
            lines.extend(("**Finding unavailable.**", ""))
            continue
        lines.extend((entry["answer"], ""))
        if entry["citations"]:
            lines.extend(("### Evidence", ""))
        for raw_citation in entry["citations"]:
            try:
                citation = Citation.model_validate(raw_citation)
            except ValueError:
                continue
            symbol = f" — `{_inline_code(citation.symbol)}`" if citation.symbol else ""
            location = f"`{_inline_code(citation.path)}:{citation.start_line}-{citation.end_line}`"
            if citation.permalink:
                location = f"[{location}]({citation.permalink})"
            lines.append(f"- {location}{symbol}")
        lines.append("")

    rendered = "\n".join(lines).rstrip() + "\n"
    if len(rendered) <= MAX_EXPORT_CHARS:
        return rendered
    suffix = "\n\n_Export truncated to the local safety limit._\n"
    return rendered[: MAX_EXPORT_CHARS - len(suffix)].rstrip() + suffix


__all__ = [
    "MAX_EXPORT_CHARS",
    "MAX_HISTORY_MESSAGES",
    "MAX_INVESTIGATIONS_PER_REPOSITORY",
    "Histories",
    "InvestigationEntry",
    "api_history",
    "append_failure",
    "append_success",
    "clear_history",
    "export_markdown",
    "history_for",
    "mark_stale",
    "normalize_histories",
]
