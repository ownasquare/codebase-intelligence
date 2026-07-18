# Codebase Intelligence Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task.

**Goal:** Turn the Phase 1 demo into a calm, practical repository workbench where a developer can select a repository, investigate a question, inspect the exact indexed source behind an answer, and export or clear the investigation without navigating an AI-styled chat interface.

**Architecture:** Keep SQLite as the repository/job system of record and use the repository's atomically published Qdrant collection as the versioned source catalog. Reconstruct only the redacted `TextNode` payloads from the active collection for source browsing; never expose raw uploaded snapshots. Add a narrow source-explorer service and protected FastAPI endpoints, enrich citations with explainable retrieval signals, and reorganize Streamlit into stable Investigate, Explore, Overview, and Manage views.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, LlamaIndex, Qdrant, Streamlit 1.59, pytest, Streamlit AppTest, Ruff, mypy, Bandit, and Playwright/in-app browser proof.

---

## Product and safety decisions

- The returning-user experience starts with the selected repository and its workbench. Import controls are secondary and collapsed once any repository exists.
- The visual language is neutral graphite/slate with one restrained blue-teal action color, compact spacing, modest radii, and standard developer-tool typography. It contains no gradients, glowing cards, robot avatars, chat bubbles, or decorative AI motifs.
- The source explorer reads the active published Qdrant collection. This avoids a second source catalog, schema migration, duplicated chunks, and time-of-check/time-of-use drift between citations and previews.
- Source previews contain the already-redacted indexed chunks only. They never read or serve the raw repository snapshot.
- Retrieval details are named “signals” or “match reasons,” not “confidence.” The semantic score and deterministic path/symbol/content overlaps explain ranking but do not claim probability or correctness.
- Investigation history remains local to the Streamlit session in Phase 2. Failed attempts are visible but never sent back to the answer API as conversational context.
- Call graphs, code execution, multi-repository search, durable conversations, a React rewrite, paid-provider acceptance, and hosted deployment are out of scope for this phase.

## Task 1: Add explainable retrieval contracts

**Files:**

- Modify: `src/codebase_intelligence/models.py`
- Modify: `src/codebase_intelligence/vector_store.py`
- Modify: `src/codebase_intelligence/rag_service.py`
- Modify: `tests/integration/test_vector_store.py`
- Modify: `tests/integration/test_rag_service.py`

- [x] Write failing tests that assert each retrieved chunk has a bounded semantic signal, a combined ranking value, and path/symbol/content overlap values.
- [x] Write a failing RAG test that asserts those signals are copied into each citation while older citation payloads without signals remain valid.
- [x] Run `uv run pytest tests/integration/test_vector_store.py tests/integration/test_rag_service.py -q`; expect failures for missing signal fields.
- [x] Add a `RetrievalSignals` Pydantic model with optional semantic score plus combined, path, symbol, and content values.
- [x] Add optional `retrieval_signals` to `Citation` so the API change remains additive.
- [x] Replace the opaque hybrid-score helper with a helper that calculates and returns both the combined sort value and its component signals.
- [x] Keep the current ranking weights and ordering stable unless a test exposes a defect.
- [x] Populate citation signals in `RAGService` without changing repository scoping, citation validation, or extractive fallback behavior.
- [x] Re-run the focused tests; expect all to pass.

The core calculation should remain explicit and testable:

```python
combined = semantic + (2.5 * path_overlap) + symbol_overlap + (0.75 * content_overlap)
signals = RetrievalSignals(
    semantic_score=semantic,
    combined_score=combined,
    path_overlap=path_overlap,
    symbol_overlap=symbol_overlap,
    content_overlap=content_overlap,
)
```

## Task 2: Build a redacted active-index source explorer

**Files:**

- Modify: `src/codebase_intelligence/models.py`
- Modify: `src/codebase_intelligence/vector_store.py`
- Create: `src/codebase_intelligence/source_service.py`
- Create: `tests/integration/test_source_service.py`
- Modify: `tests/integration/test_vector_store.py`

- [x] Write failing vector-store tests for scrolling an active collection, reconstructing LlamaIndex nodes, grouping chunks by repository-relative path, text/path/symbol filtering, language filtering, deterministic ordering, exact-path lookup, and bounded result sizes.
- [x] Write failing service tests for missing repositories, non-ready repositories, stale embedding fingerprints, missing physical collections, cross-repository isolation, and successful list/detail responses.
- [x] Run `uv run pytest tests/integration/test_vector_store.py tests/integration/test_source_service.py -q`; expect missing implementation failures.
- [x] Add `SourceFileSummary`, `SourceSection`, `SourceListResponse`, and `SourceDetailResponse` models. Include repository ID, active collection name, path, language, line bounds, symbol metadata, parser, indexed content, counts, and a truncation flag where applicable.
- [x] Add a bounded Qdrant scroll helper that uses payload filters for the requested repository and reconstructs nodes with LlamaIndex's `metadata_dict_to_node`.
- [x] Add vector-index methods to list file summaries and fetch one exact repository-relative path from the active collection.
- [x] Ensure source ordering is path-first for lists and start-line-first for detail. Deduplicate repeated symbol names in summaries.
- [x] Implement `SourceExplorerService` to enforce repository readiness, active collection publication, embedding fingerprint compatibility, and physical collection existence before delegating to the vector index.
- [x] Return only indexed node content. Do not open `source_snapshot_path` from this service.
- [x] Re-run the focused tests; expect all to pass.

Expected service boundary:

```python
files = explorer.list_sources(repository_id, query=query, language=language, limit=200)
source = explorer.get_source(repository_id, path="src/auth.py")
```

## Task 3: Publish protected explorer endpoints and client methods

**Files:**

- Modify: `src/codebase_intelligence/container.py`
- Create: `src/codebase_intelligence/api/routes/explorer.py`
- Modify: `src/codebase_intelligence/api/routes/__init__.py`
- Modify: `src/codebase_intelligence/api/app.py`
- Modify: `src/codebase_intelligence/ui/client.py`
- Create: `tests/api/test_explorer.py`
- Modify: `tests/ui/test_client.py`

- [x] Write failing API tests for authorization, query/language/limit validation, a ready repository list response, exact source detail, missing source, non-ready repository, and repository isolation.
- [x] Write failing client tests for list parameters, exact path encoding through query parameters, response parsing, timeout/network failures, and non-2xx errors.
- [x] Run `uv run pytest tests/api/test_explorer.py tests/ui/test_client.py -q`; expect route/client failures.
- [x] Wire one `SourceExplorerService` into `AppContainer` using the existing repository store, vector index, and settings.
- [x] Add protected `GET /repositories/{repository_id}/sources` with optional `q`, `language`, and bounded `limit` query parameters.
- [x] Add protected `GET /repositories/{repository_id}/source` with required `path` as a query parameter. A query parameter avoids ambiguous catch-all route behavior and lets httpx encode nested paths safely.
- [x] Map domain errors through the project's existing API error envelope and status-code conventions.
- [x] Add typed `ApiClient.list_sources(...)` and `ApiClient.get_source(...)` methods using httpx query parameters rather than manual URL concatenation.
- [x] Re-run the focused tests; expect all to pass.

## Task 4: Make investigation state safe, bounded, and exportable

**Files:**

- Create: `src/codebase_intelligence/ui/investigation.py`
- Create: `tests/ui/test_investigation.py`
- Modify: `src/codebase_intelligence/ui/app.py`

- [x] Write failing pure-function tests that successful question/answer pairs are included in API history, error entries are excluded, repository histories are isolated, and history is bounded per repository.
- [x] Write failing tests for deterministic Markdown export with repository metadata, questions, answers, and citations while omitting internal exception details.
- [x] Run `uv run pytest tests/ui/test_investigation.py -q`; expect module import failure.
- [x] Introduce small typed helpers for history initialization, append, clear, API-safe projection, stale marking after a reindex, and Markdown export.
- [x] Preserve current Streamlit session behavior while removing history-shaping logic from the page renderer.
- [x] Mark prior findings stale when a reindex begins so old evidence is not presented as current without context.
- [x] Re-run focused tests; expect all to pass.

## Task 5: Redesign Streamlit as a repository workbench

**Files:**

- Create: `src/codebase_intelligence/ui/design.py`
- Rewrite: `src/codebase_intelligence/ui/app.py`
- Modify: `.streamlit/config.toml`
- Modify: `tests/ui/conftest.py`
- Modify: `tests/ui/test_app.py`

- [x] Extend the fake API with source-list/detail behavior and deterministic fixtures.
- [x] Write failing AppTest scenarios for the empty state, returning workspace with collapsed import controls, compact repository selection, Investigate/Explore/Overview/Manage navigation, successful finding, failed finding excluded from later API history, clear, Markdown export, reindex stale state, source filtering, source detail, citation-to-explorer navigation, loading, offline, delete, and mobile-safe content labels.
- [x] Run `uv run pytest tests/ui/test_app.py tests/ui/test_investigation.py tests/ui/test_client.py -q`; expect UI contract failures.
- [x] Move page CSS into `ui/design.py`. Use neutral surfaces, restrained action color, visible focus rings, readable code blocks with horizontal overflow, 44-pixel minimum primary touch targets, and no gradient/shadow-heavy/AI-chat styling.
- [x] Replace the oversized marketing hero with a compact product bar containing the product name, connection state, and selected repository context.
- [x] Render a ready repository workbench before import controls. Keep “Add repository” expanded only when no repository exists.
- [x] Replace repository cards with one searchable native repository selector and remove duplicated repository identity/stat blocks.
- [x] Add stable Investigate, Explore, Overview, and Manage views. Keep destructive/reindex controls out of the primary investigation flow.
- [x] Render investigation entries as neutral Question and Finding records, not `st.chat_message` bubbles. Use a form with a clear “Find evidence” action and short example prompts.
- [x] Render citations as compact collapsed evidence rows. Show path, line range, symbol, parser, and match reasons; hide the raw combined score.
- [x] Add “Open in explorer” on citations, set the selected path, and navigate to Explore.
- [x] In Explore, provide a source search, language filter, file selector, indexed/redacted notice, and line-aware source sections. Never render raw snapshots.
- [x] Add clear-history and Markdown download actions to Investigate.
- [x] Put runtime/provider details in a collapsed sidebar disclosure and poll only repositories with active indexing jobs. Do not refresh a ready repository's input form every three seconds.
- [x] Use accessible text labels for status; color must not be the only signal.
- [x] Re-run all UI tests; expect all to pass.

## Task 6: Validate the complete Phase 2 change

**Files:**

- Create: `tests/e2e/test_phase2_workbench.py` if the repository's Playwright harness is added during implementation; otherwise record in-app browser proof without introducing a redundant harness.
- Modify: `README.md`
- Modify: `docs/api/reference.md`
- Modify: `docs/architecture/overview.md`
- Modify: `docs/operations/runbook.md`
- Create: `docs/codebase-intelligence/2026-07-17-phase-2-completion.md`
- Modify: `docs/handoffs/2026-07-17-codex-codebase-intelligence.handoff.mdc`
- Modify: `docs/handoffs/2026-07-17-codebase-intelligence.precompact.handoff.mdc`

- [x] Run the focused backend and UI suites; expect all tests to pass.
- [x] Run `uv run pytest -q --cov=src/codebase_intelligence --cov-branch --cov-report=term-missing`; expect the complete deterministic suite and configured coverage gate to pass.
- [x] Run `uv run ruff check .` and `uv run ruff format --check .`; expect no diagnostics.
- [x] Run `uv run mypy src`; expect strict type checking to pass.
- [x] Run `uv run bandit -q -r src`; expect no findings.
- [x] Run the existing dependency/security/package validation commands from the runbook; preserve the distinction between local deterministic proof and external-provider proof.
- [x] Restart the local API so the new routes are loaded and let Streamlit reload the new page.
- [x] Exercise a bundled sample repository through the real local API: repository selection, investigation answer, collapsed citation, source search, citation-to-source navigation, clear/export, Overview, and Manage.
- [x] Capture desktop and 390×844 mobile browser proof. Verify the selected ready repository and primary question action are visible in the first viewport, no horizontal page overflow exists, keyboard focus is visible, and the browser console is clean.
- [x] Verify loading, empty, offline, error, and destructive confirmation states through AppTest even if they are not all practical in the real-browser fixture.
- [x] Update user, API, architecture, and operations documentation with the exact Phase 2 contracts and limitations.
- [x] Create the required completion document and refresh both `.mdc` handoff packages with exact commit/proof boundaries and remaining P3 follow-ups.
- [x] Run a final tracked secret review and `git diff --check`, inspect the full diff, then commit the Phase 2 implementation intentionally.

## Phase 2 acceptance checklist

- [x] A returning user sees the selected ready repository and question action without scrolling on desktop and mobile.
- [x] Import is a single secondary action and is collapsed when repositories exist.
- [x] Repository identity and statistics are not duplicated in card and detail layouts.
- [x] No gradient, robot avatar, bot bubble, purple glow, or confidence claim remains.
- [x] Questions and findings have clear visual hierarchy and citations are compact by default.
- [x] Every citation can open the matching repository-scoped indexed source.
- [x] Source previews are redacted-index content and cannot expose raw snapshots.
- [x] Polling cannot clear or rerender a ready repository's active question input.
- [x] Failed investigations are excluded from answer history; history is bounded, clearable, and exportable.
- [x] Empty, loading, ready, offline, error, stale-after-reindex, and delete confirmation states are covered.
- [x] Deterministic tests, branch coverage, static analysis, package checks, and desktop/mobile browser proof are green.
- [x] External provider, hosted, production, and multi-process Qdrant claims remain explicitly unproven unless separately exercised.
