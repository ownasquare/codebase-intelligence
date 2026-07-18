# Contributing

Thanks for helping make Codebase Intelligence easier to trust, use, and extend.

Repository content is always untrusted data. Changes must keep retrieval repository-scoped and
preserve file and line metadata from ingestion through citations.

## Set up a development checkout

Install Python 3.12, [uv](https://docs.astral.sh/uv/), Make, and Git.

```bash
make sync
make demo
```

`make demo` starts the credential-free app in one terminal. Open <http://127.0.0.1:8501> and use
`Ctrl+C` to stop it.

Do not add credentials, private archives, proprietary snippets, provider responses, or
unsanitized logs to fixtures or commits.

## Choose the right extension point

Read [Extending Codebase Intelligence](docs/development/extending.md) before adding a language,
provider, source type, route, or interface feature. External entry-point plugins are not supported
yet; extension seams are explicit in-tree contracts.

Keep the main product path simple:

1. add a repository;
2. ask a question; and
3. open cited source.

Put advanced or destructive controls behind clear secondary disclosure in the interface.

## Work in small, testable changes

1. Read the nearest architecture, API, operations, or security document.
2. Add a focused failing test for behavior changes.
3. Implement the smallest complete typed change.
4. Run focused tests while iterating.
5. Run `make check` before requesting review.
6. Update public documentation when behavior or contracts change.

Examples of focused checks:

```bash
uv run pytest tests/unit/test_providers.py
uv run pytest tests/api
uv run pytest -m ui
```

The default suite is deterministic and does not call paid providers. Tests marked `live` require
explicit opt-in and must not run in the default CI path.

## Required gates

```bash
make check
```

This runs Ruff lint and formatting checks, strict mypy, Bandit, dependency audit, deterministic
tests, and branch coverage. Streamlit AppTest covers interface state and interaction separately
from the source coverage denominator.

For container changes, also run:

```bash
docker compose config --quiet
docker build --tag codebase-intelligence:local .
```

Do not hide or weaken a warning to make a gate pass. Document a genuine upstream exception
narrowly.

## Design and security rules

- Target Python 3.12 and keep public interfaces typed.
- Never execute imported repository code, hooks, builds, package managers, or tests.
- Never perform an unscoped vector search.
- Keep archive, file, chunk, question, and response limits bounded.
- Keep blocking filesystem, parser, provider, and Qdrant work off the async event loop.
- Do not mutate LlamaIndex global provider settings.
- Build a new versioned collection before publishing it as active.
- Query only the persisted active collection after its fingerprint and physical presence pass.
- Keep repository/job lifecycle mutations atomic where documented.
- Preserve original path and line metadata through redaction, indexing, retrieval, and rendering.
- Return bounded, sanitized errors without remote bodies or internal stack traces.

## API compatibility

Routes live under `/api/v1`. Additive response fields need safe defaults. A breaking route, state,
error, or persistence change needs an explicit versioning and migration decision, client updates,
and an updated [API reference](docs/api/reference.md).

The authenticated schema at `/api/v1/openapi.json` is a contract aid, not a replacement for
behavior tests.

## Pull request checklist

- [ ] Focused tests fail before and pass after the change.
- [ ] `make check` passes.
- [ ] Container validation passes when packaging changed.
- [ ] No credential or private source appears in the diff or artifacts.
- [ ] Security boundaries and resource limits remain intact.
- [ ] API, client, and interface behavior stay synchronized.
- [ ] User-facing and extension documentation is current.
- [ ] New dependencies are necessary, locked, licensed appropriately, and audited.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
