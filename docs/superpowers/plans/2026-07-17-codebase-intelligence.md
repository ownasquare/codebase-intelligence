# Codebase Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure, production-shaped local application that ingests a GitHub repository or ZIP archive, creates Tree-sitter-aware code chunks, indexes them with LlamaIndex and Qdrant, and answers repository questions with file-and-line citations through FastAPI and Streamlit.

**Architecture:** FastAPI is the system of record and exposes versioned repository, job, retrieval, and health endpoints. A durable SQLite manifest coordinates a separate worker, while Qdrant stores one isolated vector collection per repository; Streamlit talks only to FastAPI. Repository bytes are treated as untrusted data and pass through bounded download, safe archive extraction, file filtering, secret redaction, Tree-sitter parsing, and deterministic chunk metadata before any provider call.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLite, Tree-sitter Language Pack, LlamaIndex, Qdrant, OpenAI or Voyage AI embeddings, OpenAI grounded answer synthesis, Streamlit, httpx, uv, pytest, Ruff, mypy, Bandit, pip-audit, Docker Compose.

---

## File map

- `src/codebase_intelligence/config.py`: typed environment configuration and safety limits.
- `src/codebase_intelligence/models.py`: API and domain contracts for repositories, jobs, questions, citations, and health.
- `src/codebase_intelligence/database.py`: SQLite schema, transactions, manifest state, and durable job claims.
- `src/codebase_intelligence/repository.py`: repository/job persistence operations and legal state transitions.
- `src/codebase_intelligence/security.py`: API-key comparison, safe filenames, error redaction, and untrusted-content guards.
- `src/codebase_intelligence/ingestion/source_loader.py`: GitHub-only archive download and bounded ZIP extraction.
- `src/codebase_intelligence/ingestion/file_filter.py`: gitignore-aware file discovery, binary/vendor/secret exclusions, and limits.
- `src/codebase_intelligence/ingestion/language_registry.py`: extension-to-Tree-sitter language mapping.
- `src/codebase_intelligence/ingestion/chunker.py`: symbol-aware Tree-sitter chunks with line ranges and fallback chunks.
- `src/codebase_intelligence/providers.py`: OpenAI/Voyage embedding factories and OpenAI/extractive answer providers.
- `src/codebase_intelligence/vector_store.py`: repository-isolated Qdrant collections via the LlamaIndex Qdrant adapter.
- `src/codebase_intelligence/rag_service.py`: ingestion, retrieval, grounding prompt, citation validation, and deletion orchestration.
- `src/codebase_intelligence/job_service.py`: durable enqueue, lease, retry, progress, and cancellation logic.
- `src/codebase_intelligence/worker.py`: long-running ingestion worker entry point.
- `src/codebase_intelligence/container.py`: dependency composition shared by API and worker.
- `src/codebase_intelligence/api/app.py`: FastAPI application factory, lifespan, middleware, and exception mapping.
- `src/codebase_intelligence/api/routes/*.py`: focused health, repository, job, query, and status endpoints.
- `src/codebase_intelligence/ui/client.py`: typed, timeout-bounded FastAPI client.
- `src/codebase_intelligence/ui/app.py`: repository onboarding, progress, chat, and source explorer UI.
- `tests/`: deterministic unit, API, integration, UI, evaluation, and security regression suites.
- `docs/`: architecture, API, operations, security, completion, and handoff records.

### Task 1: Scaffold the package and typed configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `src/codebase_intelligence/__init__.py`
- Create: `src/codebase_intelligence/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write configuration tests**

```python
def test_data_paths_are_derived_from_data_dir(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.database_path == tmp_path / "manifest.sqlite3"
    assert settings.repositories_dir == tmp_path / "repositories"


def test_production_provider_requires_credential(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedding_provider="voyage")
    assert settings.embedding_ready is False
```

- [ ] **Step 2: Run the focused tests and confirm they fail before implementation**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: collection or import failure because `Settings` does not exist.

- [ ] **Step 3: Implement immutable Pydantic settings**

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEBASE_INTEL_", env_file=".env")
    data_dir: Path = Path(".data")
    embedding_provider: Literal["voyage", "openai", "deterministic"] = "voyage"
    voyage_embedding_model: str = "voyage-code-3"
    openai_embedding_model: str = "text-embedding-3-small"
    answer_provider: Literal["openai", "extractive"] = "openai"
    openai_chat_model: str = "gpt-5-mini"
    max_archive_bytes: int = 100 * 1024 * 1024
    max_extracted_bytes: int = 500 * 1024 * 1024
    max_files: int = 20_000
    max_file_bytes: int = 2 * 1024 * 1024
```

- [ ] **Step 4: Lock dependencies and rerun configuration tests**

Run: `uv lock && uv sync --all-groups && uv run pytest tests/unit/test_config.py -q`

Expected: all configuration tests pass on Python 3.12.

### Task 2: Build durable manifest and job state

**Files:**
- Create: `src/codebase_intelligence/models.py`
- Create: `src/codebase_intelligence/database.py`
- Create: `src/codebase_intelligence/repository.py`
- Create: `src/codebase_intelligence/job_service.py`
- Test: `tests/unit/test_repository.py`
- Test: `tests/integration/test_job_service.py`

- [ ] **Step 1: Write transition and lease tests**

```python
def test_only_one_worker_can_claim_a_queued_job(store: RepositoryStore) -> None:
    job = store.enqueue_ingestion(repository_id="repo-1", payload={"kind": "zip"})
    first = store.claim_next_job(worker_id="worker-a")
    second = store.claim_next_job(worker_id="worker-b")
    assert first is not None and first.id == job.id
    assert second is None
```

- [ ] **Step 2: Create the WAL-enabled SQLite schema**

```sql
CREATE TABLE repositories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_url TEXT,
  source_ref TEXT,
  commit_sha TEXT,
  collection_name TEXT,
  stats_json TEXT NOT NULL,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- [ ] **Step 3: Implement atomic claims, progress, completion, failure, and stale-lease recovery**

Run: `uv run pytest tests/unit/test_repository.py tests/integration/test_job_service.py -q`

Expected: transition and concurrent-claim tests pass without provider calls.

### Task 3: Securely acquire and inspect untrusted repositories

**Files:**
- Create: `src/codebase_intelligence/security.py`
- Create: `src/codebase_intelligence/ingestion/source_loader.py`
- Create: `src/codebase_intelligence/ingestion/file_filter.py`
- Test: `tests/unit/ingestion/test_source_loader.py`
- Test: `tests/unit/ingestion/test_file_filter.py`

- [ ] **Step 1: Write hostile-input tests**

```python
@pytest.mark.parametrize("member", ["../escape.py", "/absolute.py", "repo/../../escape.py"])
def test_archive_rejects_path_traversal(member: str, tmp_path: Path) -> None:
    archive = zip_with_member(tmp_path, member, b"print('unsafe')")
    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor(max_files=10, max_bytes=1024).extract(archive, tmp_path / "out")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/repo",
    "https://gitlab.com/acme/repo",
    "https://github.com/acme/repo/issues/1",
])
def test_github_source_rejects_non_repository_urls(url: str) -> None:
    with pytest.raises(InvalidSourceError):
        GitHubRepository.parse(url)
```

- [ ] **Step 2: Implement GitHub URL normalization**

Accept only `github.com/<owner>/<repo>` and construct requests against fixed `api.github.com` endpoints. Pass optional private-repository tokens only in an authorization header, never in URLs, SQLite, logs, or exception strings.

- [ ] **Step 3: Implement bounded streaming download and safe extraction**

Reject oversized downloads, encrypted entries, symlinks, devices, absolute paths, traversal, excessive path depth, too many entries, oversized members, and archives whose expanded total exceeds the configured limit.

- [ ] **Step 4: Implement gitignore-aware file selection**

Exclude `.git`, dependency/build/cache/vendor trees, binaries, generated maps, key/certificate material, `.env*`, and files above the per-file limit. Apply repository `.gitignore` rules using `pathspec` without executing repository code or hooks.

- [ ] **Step 5: Run the security-focused suite**

Run: `uv run pytest tests/unit/ingestion/test_source_loader.py tests/unit/ingestion/test_file_filter.py -q`

Expected: every hostile archive and URL case is rejected while a bounded GitHub archive succeeds.

### Task 4: Parse code and create line-accurate semantic chunks

**Files:**
- Create: `src/codebase_intelligence/ingestion/language_registry.py`
- Create: `src/codebase_intelligence/ingestion/chunker.py`
- Create: `src/codebase_intelligence/ingestion/redaction.py`
- Test: `tests/unit/ingestion/test_chunker.py`
- Test: `tests/unit/ingestion/test_redaction.py`
- Create: `tests/fixtures/sample_repo/`

- [ ] **Step 1: Write Python, TypeScript, fallback, and redaction tests**

```python
def test_python_symbols_keep_exact_lines(chunker: CodeChunker) -> None:
    chunks = chunker.chunk(Path("auth.py"), "def authenticate(user):\n    return user.active\n")
    symbol = next(chunk for chunk in chunks if chunk.symbol == "authenticate")
    assert (symbol.start_line, symbol.end_line) == (1, 2)
    assert symbol.language == "python"


def test_private_key_block_is_redacted() -> None:
    result = redact_secrets("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
    assert "abc" not in result.text
    assert result.redaction_count == 1
```

- [ ] **Step 2: Implement the language registry**

Cover Python, JavaScript, TypeScript, TSX, Java, Kotlin, Go, Rust, C, C++, C#, Ruby, PHP, Swift, Scala, Bash, SQL, HTML, CSS, JSON, YAML, TOML, Markdown, Dockerfile, and Terraform.

- [ ] **Step 3: Implement Tree-sitter symbol chunks**

Use Tree-sitter byte and point ranges as the source of truth. Prefer complete class/function/method/interface/struct/module nodes; split oversized nodes by line windows while preserving symbol metadata, and use deterministic line chunks when a grammar is unavailable or the file is prose/configuration.

- [ ] **Step 4: Run parser tests**

Run: `uv run pytest tests/unit/ingestion/test_chunker.py tests/unit/ingestion/test_redaction.py -q`

Expected: supported languages report `parser=tree_sitter`, fallbacks report `parser=fallback`, and all citations map to the original lines.

### Task 5: Integrate LlamaIndex embeddings and repository-isolated Qdrant

**Files:**
- Create: `src/codebase_intelligence/providers.py`
- Create: `src/codebase_intelligence/vector_store.py`
- Test: `tests/fakes.py`
- Test: `tests/integration/test_vector_store.py`

- [ ] **Step 1: Write isolated indexing and deletion tests**

```python
def test_repository_collections_are_isolated(index: CodeVectorIndex) -> None:
    index.index("repo-a", [node("auth", "authenticate user")])
    index.index("repo-b", [node("billing", "capture payment")])
    assert all(hit.repository_id == "repo-a" for hit in index.search("repo-a", "user", 5))
    index.delete("repo-a")
    assert index.search("repo-b", "payment", 5)
```

- [ ] **Step 2: Implement provider factories**

Use `VoyageEmbedding(model_name="voyage-code-3")` for the default code-optimized provider and `OpenAIEmbedding(model="text-embedding-3-small")` as the alternative. Keep a deterministic LlamaIndex-compatible embedding only for tests and explicit local demonstration mode.

- [ ] **Step 3: Implement Qdrant storage through LlamaIndex**

Create one collection named from the repository UUID, store LlamaIndex `TextNode` payloads with repository/commit/path/symbol/language/line metadata, use cosine distance, and delete the whole collection when its repository is deleted or a build fails.

- [ ] **Step 4: Run Qdrant local-mode integration tests**

Run: `uv run pytest tests/integration/test_vector_store.py -q`

Expected: persistence, restart readback, repository isolation, and collection deletion pass in an isolated temporary directory.

### Task 6: Build grounded retrieval and cited answers

**Files:**
- Create: `src/codebase_intelligence/rag_service.py`
- Create: `src/codebase_intelligence/prompts.py`
- Test: `tests/integration/test_rag_service.py`
- Test: `tests/eval/golden_questions.json`
- Test: `tests/eval/test_retrieval_eval.py`

- [ ] **Step 1: Write grounded-answer and injection tests**

```python
def test_code_instructions_are_data_not_commands(rag: RAGService) -> None:
    answer = rag.ask("repo-1", "Where is authentication?", top_k=4)
    assert "ignore all previous instructions" not in answer.answer.lower()
    assert answer.citations
    assert answer.citations[0].path == "src/auth.py"
```

- [ ] **Step 2: Implement retrieval and synthesis**

Retrieve only from the selected repository collection, cap `top_k`, label contexts as untrusted, require source IDs in the answer, reject unknown citation IDs, and always return structured citations independently of model prose. The extractive mode must remain useful without an answer-model credential by returning ranked symbol/file evidence.

- [ ] **Step 3: Add golden retrieval questions**

Include authentication, payment flow, configuration, error handling, and deliberately unanswerable questions. Assert expected files appear within top-k and unanswerable requests do not invent a location.

- [ ] **Step 4: Run the offline RAG and evaluation suites**

Run: `uv run pytest tests/integration/test_rag_service.py tests/eval/test_retrieval_eval.py -q`

Expected: deterministic tests make no network calls and every golden expected path is retrieved.

### Task 7: Expose the complete FastAPI contract

**Files:**
- Create: `src/codebase_intelligence/container.py`
- Create: `src/codebase_intelligence/api/app.py`
- Create: `src/codebase_intelligence/api/dependencies.py`
- Create: `src/codebase_intelligence/api/routes/health.py`
- Create: `src/codebase_intelligence/api/routes/repositories.py`
- Create: `src/codebase_intelligence/api/routes/jobs.py`
- Create: `src/codebase_intelligence/api/routes/query.py`
- Create: `src/codebase_intelligence/api/routes/status.py`
- Test: `tests/api/test_api.py`

- [ ] **Step 1: Write API lifecycle tests**

```python
def test_zip_ingest_query_delete_lifecycle(client: TestClient, sample_repo_zip: bytes) -> None:
    created = client.post("/api/v1/repositories/upload", files={"file": ("repo.zip", sample_repo_zip)}).json()
    run_worker_until_idle(client.app.state.container)
    response = client.post(
        f"/api/v1/repositories/{created['repository_id']}/questions",
        json={"question": "Where is authentication?", "top_k": 5},
    )
    assert response.status_code == 200
    assert response.json()["citations"][0]["path"] == "src/auth.py"
    assert client.delete(f"/api/v1/repositories/{created['repository_id']}").status_code == 204
```

- [ ] **Step 2: Implement versioned routes and error mapping**

Provide live/readiness health, create-from-GitHub, create-from-ZIP, list/detail/delete/reindex repositories, list/detail jobs, question answering, and provider/status endpoints. Return `202` for queued work, `409` for illegal repository states, `413` for bounded-size violations, and RFC 9457-style problem details for errors.

- [ ] **Step 3: Add optional API-key middleware and request IDs**

Use constant-time comparison, exempt only health endpoints, set conservative CORS origins, and never log credentials, questions, repository contents, or provider payloads.

- [ ] **Step 4: Run API tests and inspect generated OpenAPI**

Run: `uv run pytest tests/api/test_api.py -q`

Expected: lifecycle, validation, auth, request-size, and error-contract tests pass.

### Task 8: Deliver the Streamlit product UI

**Files:**
- Create: `.streamlit/config.toml`
- Create: `src/codebase_intelligence/ui/client.py`
- Create: `src/codebase_intelligence/ui/app.py`
- Test: `tests/ui/test_client.py`
- Test: `tests/ui/test_app.py`

- [ ] **Step 1: Write client and Streamlit AppTest cases**

```python
def test_empty_state_guides_repository_ingestion(app_test: AppTest) -> None:
    app_test.run()
    assert app_test.get(".stMarkdown")[0].value
    assert any("GitHub" in tab.label for tab in app_test.tabs)
```

- [ ] **Step 2: Implement repository onboarding**

Provide GitHub URL/ref/token inputs and ZIP upload, explicit safety limits, queued-job progress, retryable errors, repository cards, indexing statistics, reindex, and confirmed delete.

- [ ] **Step 3: Implement cited repository chat**

Support sample questions, persistent per-repository history, answer-mode labels, source cards with score/path/symbol/lines/language/snippet, and exact GitHub permalinks when a commit SHA is known.

- [ ] **Step 4: Run Streamlit tests**

Run: `uv run pytest tests/ui -q`

Expected: empty, loading, ready, failed, chat, source, and delete-confirmation states render without a live provider.

### Task 9: Package operations, CI, and human documentation

**Files:**
- Create: `src/codebase_intelligence/worker.py`
- Create: `Makefile`
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `.github/workflows/ci.yml`
- Create: `README.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `LICENSE`
- Create: `docs/architecture/overview.md`
- Create: `docs/api/reference.md`
- Create: `docs/operations/runbook.md`
- Create: `docs/security/threat-model.md`

- [ ] **Step 1: Implement a non-root multi-stage image and private service topology**

Run API, worker, UI, and Qdrant as separate services. Bind only the UI by default; keep API and Qdrant on the private Compose network, use health checks, drop Linux capabilities, set `no-new-privileges`, and persist only named data volumes.

- [ ] **Step 2: Add deterministic developer commands**

`make sync`, `make test`, `make coverage`, `make lint`, `make typecheck`, `make security`, `make check`, `make api`, `make worker`, `make ui`, `make compose-up`, and `make compose-down` must map to documented commands.

- [ ] **Step 3: Add CI gates**

CI runs Ruff, formatting, strict mypy, Bandit, pip-audit, offline tests with branch coverage at or above 80%, and a Docker image build on Python 3.12.

- [ ] **Step 4: Document the full operator journey**

README and runbooks cover provider choice, local demo mode, public/private GitHub repositories, ZIP input, API/UI launch, worker lifecycle, reindex/delete, backup, upgrades, Qdrant server mode, limits, security boundaries, and troubleshooting.

### Task 10: Validate, visually prove, document, and commit

**Files:**
- Create: `docs/codebase-intelligence/2026-07-17-completion.md`
- Create: `docs/handoffs/2026-07-17-codex-codebase-intelligence.handoff.mdc`

- [ ] **Step 1: Run the full quality gate**

Run: `make check`

Expected: lint, formatting, mypy, Bandit, pip-audit, tests, and coverage all pass with no suppressed warnings.

- [ ] **Step 2: Build and smoke-test containers**

Run: `docker compose config && docker build -t codebase-intelligence:local .`

Expected: Compose validates and the non-root image builds successfully.

- [ ] **Step 3: Run localhost API and Streamlit proof**

Index the bundled sample repository in deterministic/extractive mode, ask the authentication and payment-flow questions, inspect every screenshot at desktop and mobile widths, verify health/readiness, and verify deletion readback.

- [ ] **Step 4: Record truthful proof boundaries**

The completion record separates offline deterministic tests, localhost mock-backed browser proof, optional paid-provider proof, container proof, hosted proof, and production proof. It lists exact changed files, validation commands, warning triage, and known follow-up.

- [ ] **Step 5: Commit coherent validated groups**

```bash
git add pyproject.toml uv.lock src tests
git commit -m "feat: build secure codebase intelligence engine"
git add README.md SECURITY.md CONTRIBUTING.md LICENSE docs Dockerfile docker-compose.yml Makefile .github .streamlit
git commit -m "docs: add operations and validation for codebase intelligence"
```

Expected: a clean local `main` branch. Push remains unclaimed until a remote is explicitly configured.

## Self-review

- Spec coverage: FastAPI, Tree-sitter, OpenAI/Voyage embeddings, Qdrant, LlamaIndex, Streamlit, GitHub ingestion, ZIP upload, authentication-location questions, payment-flow questions, citations, tests, deployment, and documentation each map to a task.
- Security coverage: fixed-host GitHub acquisition, SSRF resistance, archive safety, input limits, secret/binary/vendor filtering, prompt-injection isolation, repository isolation, optional API auth, private-service networking, deletion, and credential non-persistence are explicit.
- Type consistency: repository IDs, collection names, job IDs, citation fields, provider names, state names, and test method names use one vocabulary throughout.
- Placeholder scan: every step names exact behavior, files, commands, and expected evidence; no deferred implementation marker remains.
