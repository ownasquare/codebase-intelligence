"""Calm repository workbench for investigation and indexed-source review."""

from __future__ import annotations

from contextlib import suppress
from typing import Any, cast
from urllib.parse import urlsplit

import streamlit as st

from codebase_intelligence.config import Settings, get_settings
from codebase_intelligence.models import (
    Citation,
    HealthResponse,
    JobRecord,
    JobStatus,
    RepositoryCreateResponse,
    RepositoryRecord,
    RepositoryStatus,
    SourceDetailResponse,
    SourceFileSummary,
    StatusResponse,
)
from codebase_intelligence.ui.client import ApiClient, ApiError
from codebase_intelligence.ui.design import APP_STYLES
from codebase_intelligence.ui.investigation import (
    InvestigationEntry,
    api_history,
    append_failure,
    append_success,
    clear_history,
    export_markdown,
    history_for,
    mark_stale,
    normalize_histories,
)

WORKSPACE_VIEWS = ("Ask", "Source", "Repository")
SAMPLE_QUESTIONS = (
    "Where is authentication enforced?",
    "How does the payment flow work?",
    "Which configuration controls external services?",
)
ACTIVE_REPOSITORY_STATUSES = {RepositoryStatus.QUEUED, RepositoryStatus.INDEXING}


def _api_client(settings: Settings) -> ApiClient:
    existing = st.session_state.get("_api_client")
    if existing is not None:
        return cast(ApiClient, existing)
    api_key = settings.api_key.get_secret_value() if settings.api_key is not None else None
    client = ApiClient(settings.api_base_url, api_key=api_key)
    st.session_state["_api_client"] = client
    return client


def _prepare_transient_state() -> None:
    token_key = st.session_state.pop("_clear_github_token", None)
    if isinstance(token_key, str):
        st.session_state.pop(token_key, None)
    question_key = st.session_state.pop("_clear_question", None)
    if isinstance(question_key, str):
        st.session_state.pop(question_key, None)


def _set_notice(kind: str, message: str) -> None:
    st.session_state["_notice"] = {"kind": kind, "message": message}


def _render_notice() -> None:
    notice = st.session_state.pop("_notice", None)
    if not isinstance(notice, dict):
        return
    message = notice.get("message")
    if not isinstance(message, str):
        return
    if notice.get("kind") == "success":
        st.success(message)
    elif notice.get("kind") == "warning":
        st.warning(message)
    else:
        st.error(message)


def _friendly_error(error: Exception) -> str:
    if isinstance(error, ApiError):
        return error.message
    return "Something unexpected interrupted the request. Please try again."


def _safe_repository_error(message: str | None) -> str:
    return ApiError(message or "Indexing stopped before this repository became ready.").message


def _display_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "configured service"
    port = f":{parsed.port}" if parsed.port is not None else ""
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "http"
    return f"{scheme}://{host}{port}"


def _service_label(
    health: HealthResponse | None,
    status: StatusResponse | None,
) -> str:
    if health is not None and health.status == "ok":
        return "Connected"
    if health is not None or status is not None:
        return "Needs attention"
    return "Unavailable"


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


def _remember_job(created: RepositoryCreateResponse, *, reset_view: bool = True) -> None:
    st.session_state["_pending_repository_id"] = created.repository_id
    st.session_state["selected_repository_id"] = created.repository_id
    if reset_view:
        st.session_state["workspace_view"] = "Ask"


def _repository_changed() -> None:
    chosen_id = st.session_state.get("repository_selector")
    if isinstance(chosen_id, str):
        st.session_state["selected_repository_id"] = chosen_id
    st.session_state["workspace_view"] = "Ask"


def _prefill_question(question_key: str, question: str) -> None:
    st.session_state[question_key] = question


def _submit_github(
    client: ApiClient,
    *,
    url: str,
    ref: str,
    token: str,
    token_key: str,
) -> None:
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
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    finally:
        st.session_state["_clear_github_token"] = token_key
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
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _render_import(client: ApiClient, settings: Settings, *, key_prefix: str) -> None:
    github_tab, upload_tab = st.tabs(["GitHub", "ZIP upload"])
    token_key = f"{key_prefix}_github_token"
    with github_tab:
        with st.form(f"{key_prefix}_github_form", border=False):
            url = st.text_input(
                "GitHub repository URL",
                key=f"{key_prefix}_github_url",
                placeholder="https://github.com/owner/repository",
                help="Paste the HTTPS address of the repository to index.",
            )
            ref = st.text_input(
                "Branch, tag, or commit (optional)",
                key=f"{key_prefix}_github_ref",
                placeholder="main",
                help="Leave blank to use the repository's default branch.",
            )
            token = st.text_input(
                "Private repository token (optional)",
                key=token_key,
                type="password",
                help="Sent for this import only, then cleared from the page.",
            )
            submitted = st.form_submit_button(
                "Add GitHub repository",
                type="primary",
                use_container_width=True,
            )
        if submitted:
            _submit_github(
                client,
                url=url,
                ref=ref,
                token=token,
                token_key=token_key,
            )
    with upload_tab:
        with st.form(f"{key_prefix}_zip_form", border=False):
            archive = st.file_uploader(
                "Repository ZIP",
                type=["zip"],
                accept_multiple_files=False,
                help=(
                    "Upload a repository archive. Maximum compressed size: "
                    f"{_format_bytes(settings.max_archive_bytes)}."
                ),
            )
            name = st.text_input(
                "Display name (optional)",
                key=f"{key_prefix}_zip_name",
                placeholder="my-service",
                help="A short name shown in this workspace.",
            )
            uploaded = st.form_submit_button(
                "Add ZIP repository",
                type="primary",
                use_container_width=True,
            )
        if uploaded:
            _submit_zip(
                client,
                archive,
                name,
                max_archive_bytes=settings.max_archive_bytes,
            )


def _connection_state(client: ApiClient) -> tuple[HealthResponse | None, StatusResponse | None]:
    health: HealthResponse | None = None
    status: StatusResponse | None = None
    with suppress(Exception):
        health = client.health()
    with suppress(Exception):
        status = client.status()
    return health, status


def _render_sidebar(
    client: ApiClient,
    settings: Settings,
    repositories: list[RepositoryRecord],
    health: HealthResponse | None,
    status: StatusResponse | None,
) -> RepositoryRecord | None:
    selected: RepositoryRecord | None = None
    with st.sidebar:
        st.markdown("### Repositories")
        if repositories:
            repository_by_id = {repository.id: repository for repository in repositories}
            pending_id = st.session_state.pop("_pending_repository_id", None)
            if isinstance(pending_id, str) and pending_id in repository_by_id:
                st.session_state["selected_repository_id"] = pending_id
                st.session_state["repository_selector"] = pending_id

            selected_id = st.session_state.get("selected_repository_id")
            if not isinstance(selected_id, str) or selected_id not in repository_by_id:
                selected_id = repositories[0].id
                st.session_state["selected_repository_id"] = selected_id
            selector_value = st.session_state.get("repository_selector")
            if not isinstance(selector_value, str) or selector_value not in repository_by_id:
                st.session_state["repository_selector"] = selected_id
            options = [repository.id for repository in repositories]
            chosen_id = st.selectbox(
                "Repository",
                options,
                format_func=lambda item: repository_by_id[item].name,
                key="repository_selector",
                help="Switching repositories returns you to Ask.",
                on_change=_repository_changed,
            )
            st.session_state["selected_repository_id"] = chosen_id
            selected = repository_by_id[chosen_id]
            with st.expander("Add repository", expanded=False, type="compact"):
                _render_import(client, settings, key_prefix="sidebar")
        else:
            st.caption("No repositories indexed yet.")

        st.divider()
        with st.expander("System", expanded=False, type="compact"):
            st.caption(
                f"{_service_label(health, status)} · {_display_origin(settings.api_base_url)}"
            )
            if status is None:
                st.caption("Runtime details are unavailable.")
            else:
                st.caption(f"Version {status.version} · {status.environment}")
                st.caption(
                    f"Embeddings: {status.embedding.provider} · Answers: {status.answer.provider}"
                )
                worker = "inline" if status.inline_worker else "background"
                st.caption(f"Qdrant: {status.qdrant_mode} · Worker: {worker}")
    return selected


def _render_product_bar(repository: RepositoryRecord | None) -> None:
    identity, state = st.columns([4, 1])
    identity.title(
        "Codebase Intelligence",
        anchor=False,
        help="Ask questions about an indexed repository and verify every answer in source.",
    )
    if repository is None:
        identity.caption("Understand a repository through cited source.")
    else:
        context = repository.name
        if repository.source_ref:
            context += f" · {repository.source_ref}"
        identity.caption(context)
    if repository is not None:
        state.markdown(f"**{_status_label(repository.status)}**")
    st.divider()


def _latest_jobs(client: ApiClient, repository_id: str, *, limit: int = 5) -> list[JobRecord]:
    try:
        return client.list_jobs(repository_id=repository_id, limit=limit)
    except ApiError:
        return []


@st.fragment(run_every=3.0)
def _render_active_status(client: ApiClient, repository: RepositoryRecord) -> None:
    jobs = _latest_jobs(client, repository.id, limit=1)
    job = jobs[0] if jobs else None
    progress = job.progress if job is not None else 0
    stage = job.stage.value.replace("_", " ").title() if job is not None else "Preparing"
    st.progress(progress, text=f"{stage} · {progress}%")
    if job is not None and job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
        st.rerun()
    if job is not None and job.status is JobStatus.FAILED:
        st.error(_safe_repository_error(job.error_message))


def _histories() -> dict[str, list[InvestigationEntry]]:
    histories = normalize_histories(st.session_state.get("investigation_histories"))
    st.session_state["investigation_histories"] = histories
    return histories


def _save_histories(histories: object) -> None:
    st.session_state["investigation_histories"] = normalize_histories(histories)


def _run_question(client: ApiClient, repository: RepositoryRecord, question: str) -> bool:
    cleaned_question = question.strip()
    if not cleaned_question:
        _set_notice("warning", "Enter a question before finding evidence.")
        return False
    histories = _histories()
    entries = history_for(histories, repository.id)
    try:
        with st.spinner("Finding the strongest repository evidence…"):
            response = client.ask_question(
                repository.id,
                question=cleaned_question,
                top_k=8,
                history=api_history(entries),
            )
        _save_histories(append_success(histories, repository.id, response))
    except Exception as error:
        _save_histories(
            append_failure(
                histories,
                repository.id,
                question=cleaned_question,
                public_message=_friendly_error(error),
            )
        )
    return True


def _match_reasons(citation: Citation) -> list[str]:
    signals = citation.retrieval_signals
    if signals is None:
        return []
    reasons: list[str] = []
    if signals.path_overlap > 0:
        reasons.append("Path match")
    if signals.symbol_overlap > 0:
        reasons.append("Symbol match")
    if signals.content_overlap > 0:
        reasons.append("Content match")
    if signals.semantic_score is not None and signals.semantic_score > 0:
        reasons.append("Semantic match")
    return reasons


def _open_in_source(repository_id: str, path: str, start_line: int) -> None:
    st.session_state["workspace_view"] = "Source"
    st.session_state[f"source_file_{repository_id}"] = path
    st.session_state[f"source_line_{repository_id}"] = start_line
    st.session_state.pop(f"source_query_{repository_id}", None)
    st.session_state[f"source_language_{repository_id}"] = "All languages"


def _render_citation(
    repository: RepositoryRecord,
    citation: Citation,
    *,
    finding_index: int,
    citation_index: int,
) -> None:
    symbol = f" · {citation.symbol}" if citation.symbol else ""
    label = f"{citation_index}. {citation.path}:{citation.start_line}-{citation.end_line}{symbol}"
    with st.expander(label, expanded=False):
        detail = citation.language
        if citation.symbol_kind:
            detail += f" · {citation.symbol_kind.replace('_', ' ')}"
        st.caption(detail)
        reasons = _match_reasons(citation)
        if reasons:
            st.caption(" · ".join(reasons))
        st.code(citation.excerpt, language=citation.language or None, line_numbers=False)
        st.button(
            f"View evidence {finding_index}.{citation_index} in Source",
            key=(
                f"open_source_{repository.id}_{citation.source_id}_{finding_index}_{citation_index}"
            ),
            on_click=_open_in_source,
            args=(repository.id, citation.path, citation.start_line),
            use_container_width=True,
        )
        if citation.permalink:
            parsed = urlsplit(citation.permalink)
            if parsed.scheme == "https" and parsed.hostname in {"github.com", "www.github.com"}:
                st.link_button("Open on GitHub", citation.permalink, use_container_width=True)


def _render_finding(
    repository: RepositoryRecord,
    entry: InvestigationEntry,
    index: int,
) -> None:
    with st.container(border=True):
        st.caption(f"Question {index}")
        st.markdown(entry["question"])
        st.divider()
        if entry["stale"]:
            st.warning("This finding belongs to an earlier repository index.")
        st.caption("Finding")
        if entry["error"]:
            st.error(entry["answer"])
            return
        st.markdown(entry["answer"])
        mode = "Generated explanation" if entry["answer_mode"] == "openai" else "Evidence only"
        st.caption(mode)
        if entry["citations"]:
            st.markdown("**Evidence**")
        for citation_index, raw_citation in enumerate(entry["citations"], start=1):
            try:
                citation = Citation.model_validate(raw_citation)
            except ValueError:
                continue
            _render_citation(
                repository,
                citation,
                finding_index=index,
                citation_index=citation_index,
            )


def _safe_export_filename(name: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in name)
    return f"{cleaned.strip('-') or 'repository'}-investigation.md"


def _render_ask(client: ApiClient, repository: RepositoryRecord) -> None:
    st.subheader(
        "Ask",
        anchor=False,
        help="Ask one focused question. Each finding includes the source used to support it.",
    )
    histories = _histories()
    entries = history_for(histories, repository.id)

    question_key = f"question_{repository.id}"
    with st.form(f"investigation_form_{repository.id}", border=True):
        question = st.text_area(
            "Question",
            key=question_key,
            placeholder="Where is authentication enforced?",
            height=92,
            max_chars=4000,
            help="Be specific about a feature, flow, symbol, or behavior.",
        )
        submitted = st.form_submit_button(
            "Find evidence",
            type="primary",
            use_container_width=True,
        )
    if submitted:
        if _run_question(client, repository, question):
            st.session_state["_clear_question"] = question_key
        st.rerun()

    with st.expander("Example questions", expanded=False, type="compact"):
        for sample in SAMPLE_QUESTIONS:
            st.button(
                sample,
                key=f"sample_{repository.id}_{sample}",
                on_click=_prefill_question,
                args=(question_key, sample),
                use_container_width=True,
            )

    for index, entry in enumerate(reversed(entries), start=1):
        _render_finding(repository, entry, index)

    if not entries:
        st.caption("Findings will appear here with links to the supporting source.")
        return

    with st.expander("Save or clear findings", expanded=False, type="compact"):
        actions = st.columns(2)
        markdown = export_markdown(repository, entries)
        actions[0].download_button(
            "Download Markdown",
            data=markdown,
            file_name=_safe_export_filename(repository.name),
            mime="text/markdown",
            use_container_width=True,
        )
        if actions[1].button(
            "Clear findings",
            key=f"clear_history_{repository.id}",
            use_container_width=True,
        ):
            _save_histories(clear_history(histories, repository.id))
            st.rerun()


def _source_languages(repository: RepositoryRecord) -> list[str]:
    return ["All languages", *sorted(repository.stats.languages)]


def _selected_file(
    repository: RepositoryRecord,
    files: list[SourceFileSummary],
) -> str | None:
    if not files:
        return None
    options = [item.path for item in files]
    key = f"source_file_{repository.id}"
    current = st.session_state.get(key)
    if not isinstance(current, str) or current not in options:
        st.session_state[key] = options[0]
    return st.selectbox(
        "Indexed file",
        options,
        key=key,
        help="Choose a file from the current filtered list.",
    )


def _render_source_detail(detail: SourceDetailResponse, *, target_line: int | None = None) -> None:
    st.markdown(f"### `{detail.path}`")
    st.caption("Indexed, redacted preview")
    if detail.truncated:
        st.warning("This preview was truncated to the configured indexed-section limit.")
    if not detail.sections:
        st.info("No indexed sections are available for this file.")
        return
    target_index = next(
        (
            index
            for index, section in enumerate(detail.sections)
            if target_line is not None and section.start_line <= target_line <= section.end_line
        ),
        0,
    )
    for index, section in enumerate(detail.sections):
        symbol = section.symbol or "Indexed section"
        lines = f"lines {section.start_line}-{section.end_line}"
        with st.expander(f"{symbol} · {lines}", expanded=index == target_index):
            details = [section.parser.replace("_", " ").title()]
            if section.symbol_kind:
                details.insert(0, section.symbol_kind.replace("_", " ").title())
            st.caption(" · ".join(details))
            st.code(section.content, language=section.language or None, line_numbers=False)


def _render_source(client: ApiClient, repository: RepositoryRecord) -> None:
    st.subheader(
        "Source",
        anchor=False,
        help="Search and inspect the redacted sections stored in the active index.",
    )
    filter_columns = st.columns([3, 2])
    query = filter_columns[0].text_input(
        "Search indexed files",
        key=f"source_query_{repository.id}",
        placeholder="auth, checkout, configuration…",
        help="Filter by file path or name.",
    )
    language_options = _source_languages(repository)
    language = filter_columns[1].selectbox(
        "Language",
        language_options,
        key=f"source_language_{repository.id}",
        help="Limit the file list to one language.",
    )
    try:
        sources = client.list_sources(
            repository.id,
            query=query or None,
            language=None if language == "All languages" else language,
            limit=200,
        )
    except Exception as error:
        st.error(_friendly_error(error))
        return
    if not sources.files:
        st.caption("No indexed files match these filters.")
        return
    selected_path = _selected_file(repository, sources.files)
    if selected_path is None:
        return
    summary = next(item for item in sources.files if item.path == selected_path)
    st.caption(
        f"{sources.total:,} matching files · {summary.language} · "
        f"{summary.chunk_count:,} indexed sections · {summary.symbol_count:,} symbols"
    )
    try:
        detail = client.get_source(repository.id, selected_path)
    except Exception as error:
        st.error(_friendly_error(error))
        return
    target_line = st.session_state.get(f"source_line_{repository.id}")
    _render_source_detail(
        detail,
        target_line=target_line if isinstance(target_line, int) else None,
    )


def _render_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    st.subheader(
        "Repository",
        anchor=False,
        help="Review what was indexed. Maintenance controls stay collapsed below.",
    )
    stats = repository.stats
    metrics = st.columns(4)
    metrics[0].metric(
        "Files",
        f"{stats.file_count:,}",
        help="Source files included in the active index.",
    )
    metrics[1].metric(
        "Sections",
        f"{stats.chunk_count:,}",
        help="Searchable source sections created during indexing.",
    )
    metrics[2].metric(
        "Indexed size",
        _format_bytes(stats.indexed_bytes),
        help="Total source content retained after indexing limits.",
    )
    metrics[3].metric(
        "Redactions",
        f"{stats.redaction_count:,}",
        help="Potential secrets removed before content was indexed.",
    )

    with st.container(border=True):
        st.markdown("**Repository source**")
        st.caption(repository.source_url or "Uploaded ZIP archive")
        if repository.source_ref:
            st.write(f"Reference · `{repository.source_ref}`")
        if repository.commit_sha:
            st.write(f"Indexed commit · `{repository.commit_sha[:12]}`")
        parser_total = stats.tree_sitter_file_count + stats.fallback_file_count
        if parser_total:
            st.write(
                f"Parser coverage · {stats.tree_sitter_file_count:,} structured · "
                f"{stats.fallback_file_count:,} fallback"
            )
        if stats.languages:
            language_summary = " · ".join(
                f"{language} {count:,}"
                for language, count in sorted(
                    stats.languages.items(), key=lambda item: item[1], reverse=True
                )
            )
            st.write(f"Languages · {language_summary}")

    with st.expander("Index history", expanded=False, type="compact"):
        jobs = _latest_jobs(client, repository.id)
        if not jobs:
            st.caption("No index activity is available.")
        for job in jobs:
            st.write(f"**{job.kind.value.title()}** · {_status_label_for_job(job)}")
            st.caption(
                f"{job.stage.value.replace('_', ' ').title()} · {job.progress}% · "
                f"attempt {job.attempt}"
            )

    _render_maintenance(client, repository)


def _status_label_for_job(job: JobRecord) -> str:
    return job.status.value.replace("_", " ").title()


def _reindex_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    try:
        created = client.reindex_repository(repository.id)
        _remember_job(created, reset_view=False)
        histories = _histories()
        _save_histories(mark_stale(histories, repository.id))
        _set_notice("success", "A fresh index has been queued. Prior findings are marked stale.")
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _delete_repository(client: ApiClient, repository: RepositoryRecord) -> None:
    try:
        client.delete_repository(repository.id)
        st.session_state.pop(f"confirm_delete_{repository.id}", None)
        st.session_state.pop("selected_repository_id", None)
        _save_histories(clear_history(_histories(), repository.id))
        _set_notice("success", f"{repository.name} was removed from this workspace.")
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _render_maintenance(client: ApiClient, repository: RepositoryRecord) -> None:
    with st.expander("Maintenance", expanded=False, type="compact"):
        with st.container(border=True):
            st.markdown("**Refresh index**")
            if st.button(
                "Reindex repository",
                key=f"reindex_{repository.id}",
                help="Rebuild the index from the imported source.",
                use_container_width=True,
            ):
                _reindex_repository(client, repository)

        with st.container(border=True):
            st.markdown("**Remove repository**")
            if st.button(
                "Delete repository",
                key=f"delete_{repository.id}",
                help="Remove the imported source, job history, and vector index.",
                use_container_width=True,
            ):
                st.session_state[f"confirm_delete_{repository.id}"] = True
            if st.session_state.get(f"confirm_delete_{repository.id}"):
                st.warning("This permanently deletes the repository and cannot be undone.")
                confirm, cancel = st.columns(2)
                if confirm.button(
                    "Delete permanently",
                    key=f"confirm_delete_button_{repository.id}",
                    type="primary",
                    use_container_width=True,
                ):
                    _delete_repository(client, repository)
                if cancel.button(
                    "Keep repository",
                    key=f"cancel_delete_{repository.id}",
                    use_container_width=True,
                ):
                    st.session_state.pop(f"confirm_delete_{repository.id}", None)
                    st.rerun()


def _render_ready_workbench(client: ApiClient, repository: RepositoryRecord) -> None:
    current_view = st.session_state.get("workspace_view")
    if current_view not in WORKSPACE_VIEWS:
        st.session_state["workspace_view"] = "Ask"
    view = st.radio(
        "Workspace view",
        WORKSPACE_VIEWS,
        horizontal=True,
        label_visibility="collapsed",
        key="workspace_view",
    )
    st.divider()
    if view == "Ask":
        _render_ask(client, repository)
    elif view == "Source":
        _render_source(client, repository)
    else:
        _render_repository(client, repository)


def _render_repository_state(client: ApiClient, repository: RepositoryRecord) -> None:
    if repository.status in ACTIVE_REPOSITORY_STATUSES:
        st.subheader(repository.name, anchor=False)
        _render_active_status(client, repository)
        return
    if repository.status is RepositoryStatus.FAILED:
        st.error(_safe_repository_error(repository.error_message))
        st.caption("Review the source, then reindex when the issue is resolved.")
        _render_maintenance(client, repository)
        return
    if repository.status is RepositoryStatus.DELETING:
        st.info("The repository and its vector index are being removed.")
        return
    _render_ready_workbench(client, repository)


def _render_empty_state(client: ApiClient, settings: Settings) -> None:
    with st.container(border=True):
        st.subheader("Add your first repository", anchor=False)
        st.caption(
            "Import a GitHub repository or ZIP archive. Source is treated as untrusted data, "
            "redacted before indexing, and never executed."
        )
        _render_import(client, settings, key_prefix="empty")


def main() -> None:
    st.set_page_config(
        page_title="Codebase Intelligence",
        page_icon="⌘",
        layout="wide",
        initial_sidebar_state="auto",
    )
    st.html(APP_STYLES)
    _prepare_transient_state()

    settings = get_settings()
    client = _api_client(settings)
    health, status = _connection_state(client)
    try:
        with st.spinner("Loading workspace…"):
            repositories = client.list_repositories()
    except Exception as error:
        _render_product_bar(None)
        st.error("Workspace unavailable")
        st.caption(_friendly_error(error))
        if st.button("Refresh", type="primary"):
            st.rerun()
        return

    selected = _render_sidebar(client, settings, repositories, health, status)
    _render_product_bar(selected)
    _render_notice()
    if selected is None:
        _render_empty_state(client, settings)
        return
    _render_repository_state(client, selected)


if __name__ == "__main__":
    main()
