# Extending Codebase Intelligence

Codebase Intelligence has clear in-tree extension seams. It does **not** currently discover
third-party providers, parsers, sources, or interface modules through Python entry points.
Extensions require a repository change, tests, and documentation.

## Code map

| Area | Main location | Contract |
|---|---|---|
| Settings | `src/codebase_intelligence/config.py` | Typed environment and safety limits |
| Providers | `src/codebase_intelligence/providers.py` | Embedding factory and completion protocol |
| Languages | `src/codebase_intelligence/ingestion/language_registry.py` | Path detection and Tree-sitter metadata |
| Ingestion | `src/codebase_intelligence/ingestion/pipeline.py` | Source, extraction, scan, redact, chunk, index |
| Vector index | `src/codebase_intelligence/vector_store.py` | Repository-scoped Qdrant operations |
| API | `src/codebase_intelligence/api/routes/` | Versioned FastAPI routers |
| Interface | `src/codebase_intelligence/ui/` | Streamlit views, client, and presentation helpers |
| Domain models | `src/codebase_intelligence/models.py` | Persisted and HTTP data contracts |

Preserve the central invariants: imported code is never executed, every vector action is scoped to
one repository, and every finding retains a path and line range.

## Add or refine a language

`LanguageSpec` defines a public language name, Tree-sitter Language Pack parser name, file
extensions or exact filenames, whether semantic symbol chunking is enabled, and node-type to symbol
kind mappings.

1. Add or update a spec in `DEFAULT_LANGUAGE_SPECS`.
2. Confirm the parser name exists in the pinned `tree-sitter-language-pack` release.
3. Add detection cases near `tests/unit/ingestion/test_file_filter.py`.
4. Add representative symbol and fallback cases in
   `tests/unit/ingestion/test_chunker.py`.
5. Use small synthetic source; do not copy proprietary code.
6. If indexed structure changes, advance the index fingerprint contract and document that users
   must reindex.

Non-semantic formats still use deterministic line chunks. A missing parser falls back safely; it
must not make ingestion execute a language toolchain.

## Add an embedding provider

Provider selection is intentionally explicit, not dynamic.

1. Extend the typed provider choice and credential validation in
   `src/codebase_intelligence/config.py`.
2. Add the LlamaIndex adapter branch in `create_embedding_model()`.
3. Define its model and dimension behavior.
4. Include every index-shaping value in `index_fingerprint()`.
5. Expose non-secret readiness and mode through the status response.
6. Add factory, missing-credential, dimension, and fingerprint tests in
   `tests/unit/test_providers.py` and `tests/unit/test_config.py`.
7. Update `.env.example`, the provider table, security guidance, and dependency lock.

Do not mutate LlamaIndex global settings. Construct providers explicitly for the application
container, and sanitize upstream error details.

## Add an answer provider

Answer generation is separate from retrieval. Implement the typed `CompletionProvider` protocol,
then extend `create_completion_provider()` and the answer-provider setting.

The provider receives a bounded grounded prompt containing untrusted repository text. It must not
receive tools or execute code. `RAGService` validates returned citation IDs against the retrieved
set; preserve that check. Add tests for unavailable credentials, provider failure, invalid
citations, and extractive fallback behavior.

## Add a repository source type

The current public inputs are canonical GitHub repositories and ZIP uploads. The acquisition
boundary lives in `api/routes/repositories.py`; bounded archive handling and GitHub validation live
in `ingestion/source_loader.py`; durable submission lives in `IngestionService`.

For another source:

1. define a bounded request model and explicit allowlist;
2. acquire bytes before creating a token-free durable job;
3. stream into a size-limited staging file;
4. reuse `SafeArchiveExtractor` instead of extracting ad hoc;
5. never pass request credentials to the worker;
6. persist only a sanitized source identity; and
7. add SSRF, redirect, timeout, archive, credential, and cleanup tests.

Do not add an arbitrary URL fetcher. New remote sources need a fixed-host validation model as strict
as the GitHub loader.

## Add an API route

Create a focused router under `src/codebase_intelligence/api/routes/` and include it in the
protected `/api/v1` router in `api/app.py`. Only liveness, readiness, and the root service pointer
are intentionally outside shared API-key protection.

Use domain services from `AppContainer`, Pydantic request/response models, bounded inputs, and the
standard problem response. Update:

- API tests under `tests/api/`;
- the protected OpenAPI contract;
- `docs/api/reference.md`; and
- the Streamlit client when the interface consumes the route.

Breaking route, state, or error changes require an explicit API-version decision.

## Change the interface

`ui/app.py` owns the Streamlit composition, `ui/client.py` owns HTTP calls, `ui/design.py` owns
shared visual styling, and `ui/investigation.py` owns investigation history/export helpers.

Keep the primary sequence visible and short: **add repository → Ask → open citation in Source**.
Place metrics, reindex, delete, and service details behind secondary disclosure. Prefer concise
labels with Streamlit `help=` tooltips over permanent instructional paragraphs.

Add or update:

- client tests in `tests/ui/test_client.py`;
- state and interaction tests in `tests/ui/test_app.py`; and
- helper tests in `tests/ui/test_investigation.py`.

Use a real browser for desktop and narrow-layout proof after AppTest passes.

## Validate an extension

Run the narrowest relevant test while iterating, then:

```bash
make check
```

For packaging or service-topology changes, also run:

```bash
docker compose config --quiet
docker build --tag codebase-intelligence:local .
```

State which layers you actually verified. A local deterministic test does not prove paid-provider,
hosted, or production behavior.
