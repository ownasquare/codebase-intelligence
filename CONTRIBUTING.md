# Contributing

Thank you for improving Codebase Intelligence. Contributions should preserve its central contract: repository content is untrusted data, retrieval is repository-scoped, and every answer location is backed by structured source metadata.

## Development setup

Install Python 3.12 and uv, then run:

```bash
make sync
```

Use deterministic embeddings and extractive answers for development and tests unless a live-provider change explicitly needs separate opt-in validation:

```bash
CODEBASE_INTEL_EMBEDDING_PROVIDER=deterministic \
CODEBASE_INTEL_ANSWER_PROVIDER=extractive \
make test
```

Never add a real credential, private repository archive, proprietary snippet, provider response, or unsanitized runtime log to a test fixture or commit.

## Change workflow

1. Read the nearest architecture, API, operations, or security document before changing its contract.
2. Add a focused failing test for behavior changes, including hostile and failure cases at trust boundaries.
3. Implement the smallest complete change with typed public interfaces.
4. Run focused tests while iterating, then run `make check` before requesting review.
5. Update user, API, operations, threat-model, and completion documentation when behavior or evidence changes.
6. Keep local, container, hosted, provider, and production proof clearly separated.

## Quality gates

The required local gate is:

```bash
make check
```

It verifies Ruff lint and format, strict mypy, Bandit, pip-audit, deterministic tests, branch
coverage, and the configured 80% coverage floor. The coverage denominator intentionally omits
`src/codebase_intelligence/ui/app.py` because Streamlit AppTest executes it through an untraced
script-runner context. UI/client state and interaction tests are a separate required gate; browser
proof must state exactly which real and mock-backed states were exercised. Container changes must
also pass:

```bash
docker compose config
docker build --tag codebase-intelligence:local .
```

Do not disable, suppress, or weaken a warning merely to make a gate green. Document a genuine upstream false positive narrowly and add evidence for the exception.

## Test guidance

- Unit tests cover pure validation, parsing, filtering, redaction, provider factories, and state rules.
- Integration tests use temporary SQLite and embedded Qdrant storage.
- API tests use FastAPI's in-process client and deterministic providers.
- UI tests use Streamlit AppTest with a fake API client.
- Evaluation fixtures assert expected source paths and unanswerable behavior.
- Tests marked `live` are explicit, separately authorized checks; they must never run in the default CI suite.

Prefer synthetic repositories that are small enough to audit by inspection. Cover path traversal, archive bombs, unsupported languages, parser failure, secret-shaped text, prompt injection, cross-repository retrieval, stale leases, illegal state transitions, and deletion readback when touching those surfaces.

## Code style and design

- Target Python 3.12 and keep strict mypy clean.
- Keep modules focused and public interfaces typed.
- Use Pydantic models for API and persisted domain contracts.
- Keep blocking filesystem, parser, provider, and Qdrant work away from the async event loop.
- Never mutate LlamaIndex global provider settings.
- Never execute imported repository code, hooks, package managers, build scripts, or tests.
- Never perform an unscoped vector search; every collection operation requires a repository ID.
- Keep repository/job creation, reindex initiation, successful publication, and terminal processing
  failure atomic across their paired SQLite records. Preserve the partial unique constraint
  allowing only one queued/running job per repository.
- Build a fresh versioned Qdrant collection before publication. Never delete or overwrite the
  persisted active collection first, and keep failed reindex behavior on the last published version.
- Renew job leases through the lease-only path; do not replay stage/progress from a heartbeat.
- Query only the collection name persisted on a `ready` repository after verifying its persisted
  index fingerprint against current settings.
- Preserve original path and line metadata through redaction, chunking, indexing, retrieval, and citation rendering.
- Return bounded, sanitized problem details; do not expose remote bodies or internal stack traces.

## API compatibility

Routes live under `/api/v1`. Additive fields should have safe defaults. Breaking route, state, error, or persistence changes require an explicit versioning and migration decision plus updates to the Streamlit client and API reference.

Built-in Swagger, ReDoc, and the default public OpenAPI path remain disabled. Generated OpenAPI is
served by the protected `/api/v1/openapi.json` route and is a contract check, but it does not
replace tests for status codes, authentication exemptions, request limits, error sanitization, and
lifecycle behavior.

## Pull request checklist

- [ ] Tests fail before and pass after the change.
- [ ] `make check` passes without hidden warnings.
- [ ] Docker/Compose validation passes when packaging changes.
- [ ] No credential or private source is present in the diff or artifacts.
- [ ] Security boundaries and resource limits are preserved.
- [ ] API and UI behavior remain synchronized.
- [ ] Documentation records exact validation scope and unverified layers.
- [ ] New dependencies are necessary, locked, licensed appropriately, and audited.

By contributing, you agree that your contribution is licensed under the project's MIT License.
