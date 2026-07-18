"""Streamlit workspace for repository ingestion and cited codebase chat."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import urlsplit

import streamlit as st

from codebase_intelligence.config import Settings, get_settings
from codebase_intelligence.models import (
    ChatMessage,
    Citation,
    HealthResponse,
    JobRecord,
    JobStatus,
    QuestionResponse,
    RepositoryCreateResponse,
    RepositoryRecord,
    RepositoryStatus,
    StatusResponse,
)
from codebase_intelligence.ui.client import ApiClient, ApiError

APP_STYLES = """
<style>
  :root {
    --ci-ink: #172033;
    --ci-muted: #667085;
    --ci-line: #e2e7f0;
    --ci-surface: rgba(255, 255, 255, 0.92);
    --ci-accent: #6d5ef8;
  }

  [data-testid="stAppViewContainer"] {
    background:
      radial-gradient(circle at 8% 0%, rgba(109, 94, 248, 0.11), transparent 28rem),
      radial-gradient(circle at 92% 8%, rgba(40, 199, 164, 0.09), transparent 24rem),
      #f7f8fc;
  }

  [data-testid="stHeader"] { background: transparent; }
  [data-testid="stMainBlockContainer"] { max-width: 1180px; padding-top: 2rem; }
  [data-testid="stSidebar"] { border-right: 1px solid var(--ci-line); }
  [data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--ci-surface);
    box-shadow: 0 12px 32px rgba(23, 32, 51, 0.055);
  }
  [data-testid="stMetric"] {
    background: rgba(247, 248, 252, 0.72);
    border: 1px solid var(--ci-line);
    border-radius: 14px;
    padding: 0.7rem 0.85rem;
  }
  [data-testid="stChatMessage"] {
    background: rgba(255, 255, 255, 0.78);
    border: 1px solid var(--ci-line);
    border-radius: 18px;
    padding: 0.35rem 0.5rem;
  }
  [data-testid="stFileUploaderDropzone"] {
    background: rgba(109, 94, 248, 0.035);
    border-color: rgba(109, 94, 248, 0.32);
  }
  div.stButton > button, div.stFormSubmitButton > button { font-weight: 650; }
  code { white-space: pre-wrap !important; overflow-wrap: anywhere; }

  @media (max-width: 760px) {
    [data-testid="stMainBlockContainer"] { padding: 1rem 0.85rem 5rem; }
    [data-testid="stHorizontalBlock"] { gap: 0.6rem; }
    [data-testid="stMetric"] { padding: 0.55rem 0.65rem; }
    h1 { font-size: 2rem !important; }
  }
</style>
"""

SAMPLE_QUESTIONS: tuple[str, ...] = (
    "Where is the authentication logic?",
    "How does the payment flow work?",
    "Which configuration controls external services?",
)
ACTIVE_REPOSITORY_STATUSES = {RepositoryStatus.QUEUED, RepositoryStatus.INDEXING}


def _prepare_sensitive_state() -> None:
    """Clear the one-use GitHub token before its widget is instantiated again."""

    if st.session_state.pop("_clear_github_token", False):
        st.session_state.pop("github_token", None)


def _api_client(settings: Settings) -> ApiClient:
    existing = st.session_state.get("_api_client")
    if existing is not None:
        return cast(ApiClient, existing)
    api_key = settings.api_key.get_secret_value() if settings.api_key is not None else None
    client = ApiClient(settings.api_base_url, api_key=api_key)
    st.session_state["_api_client"] = client
    return client


def _set_notice(kind: str, message: str) -> None:
    st.session_state["_notice"] = {"kind": kind, "message": message}


def _render_notice() -> None:
    notice = st.session_state.pop("_notice", None)
    if not isinstance(notice, dict):
        return
    message = notice.get("message")
    if not isinstance(message, str):
        return
    kind = notice.get("kind")
    if kind == "success":
        st.success(message, icon="✅")
    elif kind == "warning":
        st.warning(message, icon="⚠️")
    else:
        st.error(message, icon="🚫")


def _friendly_error(error: Exception) -> str:
    if isinstance(error, ApiError):
        return error.message
    return "Something unexpected interrupted the request. Please try again."


def _safe_repository_error(message: str | None) -> str:
    if not message:
        return "Indexing stopped before this repository became ready."
    return ApiError(message).message


def _display_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "configured service"
    port = f":{parsed.port}" if parsed.port is not None else ""
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "http"
    return f"{scheme}://{host}{port}"


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _status_label(status: RepositoryStatus) -> str:
    return {
        RepositoryStatus.QUEUED: "Queued",
        RepositoryStatus.INDEXING: "Indexing",
        RepositoryStatus.READY: "Ready",
        RepositoryStatus.FAILED: "Needs attention",
        RepositoryStatus.DELETING: "Deleting",
    }[status]


def _remember_job(created: RepositoryCreateResponse) -> None:
    jobs = dict(st.session_state.get("repository_jobs", {}))
    jobs[created.repository_id] = created.job_id
    st.session_state["repository_jobs"] = jobs
    st.session_state["selected_repository_id"] = created.repository_id


def _job_for_repository(client: ApiClient, repository_id: str) -> JobRecord | None:
    jobs = st.session_state.get("repository_jobs", {})
    if not isinstance(jobs, dict):
        return None
    job_id = jobs.get(repository_id)
    if not isinstance(job_id, str):
        return None
    try:
        return client.get_job(job_id)
    except ApiError:
        return None


def _render_sidebar(client: ApiClient, settings: Settings) -> None:
    health: HealthResponse | None = None
    status: StatusResponse | None = None
    health_error: str | None = None
    try:
        health = client.health()
    except Exception as exc:  # Streamlit must remain usable while the API boots.
        health_error = _friendly_error(exc)
    try:
        status = client.status()
    except Exception:
        status = None

    with st.sidebar:
        st.markdown("### Workspace")
        if health is not None and health.status == "ok":
            st.success("API connected", icon="✅")
        elif health is not None:
            st.warning("API is degraded", icon="⚠️")
        else:
            st.error("API offline", icon="❌")
            if health_error:
                st.caption(health_error)

        st.caption(f"Service: {_display_origin(settings.api_base_url)}")
        st.caption(
            "API authentication: "
            + ("configured" if settings.api_key is not None else "not configured")
        )

        if status is not None:
            st.divider()
            st.markdown("#### Retrieval stack")
            st.write(f"**Embeddings** · {status.embedding.provider}")
            st.caption(f"{status.embedding.model} · {status.embedding.mode} mode")
            st.write(f"**Answers** · {status.answer.provider}")
            st.caption(f"{status.answer.model} · {status.answer.mode} mode")
            st.write(f"**Vector store** · Qdrant {status.qdrant_mode}")
            worker_label = "Inline worker" if status.inline_worker else "Background worker"
            st.caption(worker_label)

        st.divider()
        if st.button("Refresh workspace", use_container_width=True, icon="🔄"):
            st.rerun()
        st.caption("Repository status refreshes automatically while this page is open.")


def _submit_github(client: ApiClient, url: str, ref: str, token: str) -> None:
    try:
        if not url.strip():
            raise ApiError("Enter a GitHub repository URL.", code="missing_url")
        created = client.create_github_repository(
            url=url,
            ref=ref or None,
            token=token or None,
        )
        _remember_job(created)
        _set_notice("success", "Repository accepted. Indexing has started.")
    except Exception as exc:
        _set_notice("error", _friendly_error(exc))
    finally:
        st.session_state["_clear_github_token"] = True
    st.rerun()


def _submit_zip(
    client: ApiClient,
    uploaded_file: Any,
    name: str,
    *,
    max_archive_bytes: int,
) -> None:
    try:
        if uploaded_file is None:
            raise ApiError("Choose a ZIP archive before starting the index.", code="missing_file")
        content = uploaded_file.getvalue()
        if not content:
            raise ApiError("The selected ZIP archive is empty.", code="empty_archive")
        if len(content) > max_archive_bytes:
            raise ApiError(
                f"The selected archive is larger than {_format_bytes(max_archive_bytes)}.",
                code="archive_too_large",
            )
        created = client.upload_repository(
            filename=uploaded_file.name,
            content=content,
            name=name or None,
        )
        _remember_job(created)
        _set_notice("success", "Archive accepted. Indexing has started.")
    except Exception as exc:
        _set_notice("error", _friendly_error(exc))
    st.rerun()


def _render_onboarding(client: ApiClient, settings: Settings) -> None:
    st.subheader("Add a codebase", anchor=False)
    st.caption(
        "Index a public or private GitHub repository, or upload a ZIP. Repository code is "
        "treated as untrusted data and never executed."
    )
    github_tab, upload_tab = st.tabs(["GitHub repository", "ZIP upload"])

    with github_tab:
        with st.form("github_repository_form", border=True):
            url = st.text_input(
                "GitHub repository URL",
                key="github_url",
                placeholder="https://github.com/owner/repository",
            )
            ref = st.text_input(
                "Branch, tag, or commit (optional)",
                key="github_ref",
                placeholder="main",
            )
            token = st.text_input(
                "Private repository token (optional)",
                key="github_token",
                type="password",
                help=(
                    "Sent once in the X-GitHub-Token request header. It is cleared immediately "
                    "after submission and is not saved by this app."
                ),
            )
            submitted = st.form_submit_button(
                "Index GitHub repository",
                type="primary",
                use_container_width=True,
                icon=":material/add:",
            )
        if submitted:
            _submit_github(client, url, ref, token)

    with upload_tab:
        with st.form("zip_repository_form", border=True):
            archive = st.file_uploader(
                "Repository ZIP",
                type=["zip"],
                accept_multiple_files=False,
                help=f"Maximum compressed size: {_format_bytes(settings.max_archive_bytes)}.",
            )
            name = st.text_input(
                "Display name (optional)",
                key="zip_repository_name",
                placeholder="my-service",
            )
            uploaded = st.form_submit_button(
                "Index ZIP archive",
                type="primary",
                use_container_width=True,
                icon="⬆️",
            )
        if uploaded:
            _submit_zip(
                client,
                archive,
                name,
                max_archive_bytes=settings.max_archive_bytes,
            )


def _render_repository_card(repository: RepositoryRecord, *, selected: bool) -> None:
    with st.container(border=True):
        st.caption(f"{repository.source_kind.value.upper()} · {_status_label(repository.status)}")
        st.markdown(f"#### {repository.name}")
        if repository.source_ref:
            st.caption(f"Ref: {repository.source_ref}")
        if repository.status == RepositoryStatus.READY:
            left, right = st.columns(2)
            left.metric("Files", f"{repository.stats.file_count:,}")
            right.metric("Chunks", f"{repository.stats.chunk_count:,}")
        elif repository.status in ACTIVE_REPOSITORY_STATUSES:
            st.progress(0, text="Preparing repository…")
        elif repository.status == RepositoryStatus.FAILED:
            st.error("Indexing failed")
        else:
            st.info("Removal in progress")
        if st.button(
            "Selected" if selected else "Open repository",
            key=f"select_repository_{repository.id}",
            type="primary" if selected else "secondary",
            disabled=selected,
            use_container_width=True,
        ):
            st.session_state["selected_repository_id"] = repository.id
            st.rerun()


def _render_repository_stats(repository: RepositoryRecord) -> None:
    stats = repository.stats
    columns = st.columns(4)
    columns[0].metric("Files", f"{stats.file_count:,}")
    columns[1].metric("Code chunks", f"{stats.chunk_count:,}")
    columns[2].metric("Indexed", _format_bytes(stats.indexed_bytes))
    columns[3].metric("Redactions", f"{stats.redaction_count:,}")
    parser_total = stats.tree_sitter_file_count + stats.fallback_file_count
    if parser_total:
        st.caption(
            f"Tree-sitter parsed {stats.tree_sitter_file_count:,} files · "
            f"Fallback parsed {stats.fallback_file_count:,} · "
            f"Skipped {stats.skipped_file_count:,}"
        )
    if stats.languages:
        language_summary = " · ".join(
            f"{language} {count:,}"
            for language, count in sorted(
                stats.languages.items(), key=lambda item: item[1], reverse=True
            )[:8]
        )
        st.caption(f"Languages · {language_summary}")


def _render_active_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    job = _job_for_repository(client, repository.id)
    progress = job.progress if job is not None else 0
    stage = job.stage.value.replace("_", " ").title() if job is not None else "Preparing"
    st.info("This repository is being indexed. You can keep this page open while it progresses.")
    st.progress(progress, text=f"{stage} · {progress}%")
    st.caption(f"Current stage · {stage} · {progress}% complete")
    if job is not None and job.status == JobStatus.FAILED:
        st.error(_safe_repository_error(job.error_message))
    else:
        st.caption("Status is checked every few seconds. Questions unlock when indexing is ready.")


def _reindex_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    try:
        created = client.reindex_repository(repository.id)
        _remember_job(created)
        _set_notice("success", "A fresh index has been queued.")
    except Exception as exc:
        _set_notice("error", _friendly_error(exc))
    st.rerun()


def _delete_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    try:
        client.delete_repository(repository.id)
        st.session_state.pop(f"confirm_delete_{repository.id}", None)
        st.session_state.pop("selected_repository_id", None)
        histories = dict(st.session_state.get("chat_histories", {}))
        histories.pop(repository.id, None)
        st.session_state["chat_histories"] = histories
        _set_notice("success", f"{repository.name} was removed from this workspace.")
    except Exception as exc:
        _set_notice("error", _friendly_error(exc))
    st.rerun()


def _render_repository_actions(client: ApiClient, repository: RepositoryRecord) -> None:
    reindex_column, delete_column = st.columns(2)
    if reindex_column.button(
        "Reindex repository",
        key=f"reindex_{repository.id}",
        use_container_width=True,
        icon="🔄",
    ):
        _reindex_repository(client, repository)
    if delete_column.button(
        "Delete repository",
        key=f"delete_{repository.id}",
        use_container_width=True,
        icon="🗑️",
    ):
        st.session_state[f"confirm_delete_{repository.id}"] = True

    if st.session_state.get(f"confirm_delete_{repository.id}"):
        with st.container(border=True):
            st.warning(
                "Delete this repository and its vector index? This cannot be undone.",
                icon="⚠️",
            )
            confirm_column, cancel_column = st.columns(2)
            if confirm_column.button(
                "Delete permanently",
                key=f"confirm_delete_button_{repository.id}",
                type="primary",
                use_container_width=True,
            ):
                _delete_repository(client, repository)
            if cancel_column.button(
                "Keep repository",
                key=f"cancel_delete_{repository.id}",
                use_container_width=True,
            ):
                st.session_state.pop(f"confirm_delete_{repository.id}", None)
                st.rerun()


def _chat_history(repository_id: str) -> list[dict[str, Any]]:
    histories = dict(st.session_state.get("chat_histories", {}))
    entries = histories.get(repository_id)
    if not isinstance(entries, list):
        entries = []
        histories[repository_id] = entries
        st.session_state["chat_histories"] = histories
    return entries


def _save_chat_history(repository_id: str, entries: list[dict[str, Any]]) -> None:
    histories = dict(st.session_state.get("chat_histories", {}))
    histories[repository_id] = entries
    st.session_state["chat_histories"] = histories


def _history_for_api(entries: Iterable[dict[str, Any]]) -> list[ChatMessage]:
    history: list[ChatMessage] = []
    for entry in list(entries)[-12:]:
        role = entry.get("role")
        content = entry.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            history.append(ChatMessage(role=role, content=content[:8000]))
    return history[-12:]


def _render_citation(citation: Citation, index: int) -> None:
    with st.container(border=True):
        path_column, score_column = st.columns([4, 1])
        path_column.markdown(f"**{index}. {citation.path}**")
        if citation.score is not None:
            score_column.caption(f"Score {citation.score:.3f}")
        symbol = f" · {citation.symbol}" if citation.symbol else ""
        path_column.caption(
            f"{citation.language}{symbol} · lines {citation.start_line}-{citation.end_line}"
        )
        st.code(citation.excerpt, language=citation.language or None, line_numbers=False)
        if citation.permalink:
            parsed = urlsplit(citation.permalink)
            if parsed.scheme == "https" and parsed.hostname in {"github.com", "www.github.com"}:
                st.link_button("Open exact source", citation.permalink, icon="↗️")


def _render_chat_entry(entry: dict[str, Any]) -> None:
    role = entry.get("role")
    content = entry.get("content")
    if role not in {"user", "assistant"} or not isinstance(content, str):
        return
    with st.chat_message(role):
        if entry.get("error"):
            st.error(content)
        else:
            st.markdown(content)
        mode = entry.get("answer_mode")
        if role == "assistant" and isinstance(mode, str):
            st.caption(f"Answer mode · {mode}")
        raw_citations = entry.get("citations", [])
        if role == "assistant" and isinstance(raw_citations, list) and raw_citations:
            st.markdown("**Sources**")
            for index, raw_citation in enumerate(raw_citations, start=1):
                try:
                    citation = Citation.model_validate(raw_citation)
                except ValueError:
                    continue
                _render_citation(citation, index)


def _ask_repository_question(
    client: ApiClient,
    repository: RepositoryRecord,
    question: str,
) -> None:
    cleaned_question = question.strip()
    if not cleaned_question:
        return
    entries = _chat_history(repository.id)
    api_history = _history_for_api(entries)
    entries.append({"role": "user", "content": cleaned_question})
    try:
        with st.spinner("Reading the most relevant symbols and files…"):
            response: QuestionResponse = client.ask_question(
                repository.id,
                question=cleaned_question,
                top_k=8,
                history=api_history,
            )
        entries.append(
            {
                "role": "assistant",
                "content": response.answer,
                "answer_mode": response.answer_mode,
                "citations": [citation.model_dump(mode="json") for citation in response.citations],
            }
        )
    except Exception as exc:
        entries.append(
            {
                "role": "assistant",
                "content": _friendly_error(exc),
                "error": True,
            }
        )
    _save_chat_history(repository.id, entries)
    st.rerun()


def _render_repository_chat(client: ApiClient, repository: RepositoryRecord) -> None:
    st.divider()
    st.subheader("Ask this codebase", anchor=False)
    st.caption(
        "Answers are grounded in the selected repository. Open any source to verify the result."
    )
    entries = _chat_history(repository.id)
    if not entries:
        st.info("Start with a sample question or ask about a symbol, feature, or execution flow.")
    for entry in entries:
        _render_chat_entry(entry)

    st.caption("Try a sample question")
    sample_columns = st.columns(3)
    selected_sample: str | None = None
    for column, sample in zip(sample_columns, SAMPLE_QUESTIONS, strict=True):
        if column.button(
            sample,
            key=f"sample_{repository.id}_{sample}",
            use_container_width=True,
        ):
            selected_sample = sample

    typed_question = st.chat_input(
        "Ask where something lives or how a flow works…",
        key=f"chat_input_{repository.id}",
        max_chars=4000,
    )
    question = selected_sample or typed_question
    if question:
        _ask_repository_question(client, repository, question)


def _render_selected_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    st.divider()
    title_column, status_column = st.columns([4, 1])
    title_column.subheader(repository.name, anchor=False)
    status_column.caption(_status_label(repository.status))
    if repository.source_url:
        title_column.caption(repository.source_url)
    if repository.commit_sha:
        title_column.caption(f"Indexed commit · {repository.commit_sha[:12]}")

    if repository.status in ACTIVE_REPOSITORY_STATUSES:
        _render_active_repository(client, repository)
        return
    if repository.status == RepositoryStatus.FAILED:
        st.error(_safe_repository_error(repository.error_message), icon="🚫")
        st.caption("Review the repository source or credentials, then start a fresh index.")
        _render_repository_actions(client, repository)
        return
    if repository.status == RepositoryStatus.DELETING:
        st.info("The repository and its vector collection are being removed.")
        return

    _render_repository_stats(repository)
    _render_repository_actions(client, repository)
    _render_repository_chat(client, repository)


@st.fragment(run_every=3.0)
def _repository_workspace(client: ApiClient) -> None:
    st.subheader("Your repositories", anchor=False)
    try:
        with st.spinner("Loading repository workspace…"):
            repositories = client.list_repositories()
    except Exception as exc:
        st.error(_friendly_error(exc), icon="🚫")
        st.caption("Repository actions will return when the API connection is restored.")
        return

    if not repositories:
        st.info(
            "No repositories are indexed yet. Add a GitHub repository or ZIP above to begin.",
            icon="👋",
        )
        return

    repository_by_id = {repository.id: repository for repository in repositories}
    selected_id = st.session_state.get("selected_repository_id")
    if not isinstance(selected_id, str) or selected_id not in repository_by_id:
        selected_id = repositories[0].id
        st.session_state["selected_repository_id"] = selected_id

    columns = st.columns(min(3, len(repositories)))
    for index, repository in enumerate(repositories):
        with columns[index % len(columns)]:
            _render_repository_card(repository, selected=repository.id == selected_id)

    _render_selected_repository(client, repository_by_id[selected_id])


def main() -> None:
    st.set_page_config(
        page_title="Codebase Intelligence",
        page_icon="⌘",
        layout="wide",
        initial_sidebar_state="auto",
    )
    st.html(APP_STYLES)
    _prepare_sensitive_state()

    settings = get_settings()
    client = _api_client(settings)
    _render_sidebar(client, settings)

    st.caption("CODEBASE INTELLIGENCE · CITED CODE RAG")
    st.title("Understand any codebase, with evidence.")
    st.markdown(
        "Trace authentication, payments, configuration, and execution flows across an entire "
        "repository. Every answer links back to the exact files, symbols, and lines used."
    )
    _render_notice()
    _render_onboarding(client, settings)
    st.divider()
    _repository_workspace(client)


if __name__ == "__main__":
    main()
