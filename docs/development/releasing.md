# Release Process

This is the maintainer checklist for a Codebase Intelligence release. The project follows semantic
versioning while in beta: minor versions may deliberately advance documented extension or index
contracts, and breaking user-facing changes must be called out.

## Prepare

1. Start from a clean release branch and review every included change.
2. Choose the version and update it consistently in:
   - `pyproject.toml`;
   - `src/codebase_intelligence/__init__.py`;
   - API examples and supported-version documentation; and
   - the dependency lock with `uv lock`.
3. Add a dated entry to `CHANGELOG.md` and a plain-language note under `docs/releases/`.
4. Document migration or reindex requirements.
5. Confirm no internal handoff, local path, private source, credential, or runtime log is tracked.

## Validate

```bash
make check
uv build
docker compose config --quiet
docker build --tag codebase-intelligence:release .
```

Also verify:

- `codebase-intelligence --version` from a clean install of the built wheel;
- `codebase-intelligence demo` starts the credential-free API and interface;
- `Ctrl+C` stops both demo processes;
- one clean repository reaches Ready;
- a question returns citations that open in **Source**;
- desktop and narrow layouts complete that same core flow; and
- the release docs contain no broken relative links.

Paid providers, private GitHub access, Docker runtime, hosted environments, and production should
be claimed only when they were separately exercised.

## Publish

1. Review the final diff and artifact contents.
2. Commit the exact release files.
3. Create an annotated tag named `vX.Y.Z` at the validated commit.
4. Push the commit and tag to the intended remote.
5. Create the forge release from the matching release note.
6. Publish packages or images only to registries configured and owned by the project.

There is no automatic public package-registry publication contract in this repository yet. Do not
imply PyPI or container-registry availability until that pipeline exists and its artifacts are
verified.

## After publication

- Install from the published artifact in a clean environment.
- Repeat the credential-free first question.
- Verify the release tag and displayed application version match.
- Record any provider, container, hosted, or production proof separately.
- If validation fails, mark the release clearly and follow the rollback section in the
  [operations runbook](../operations/runbook.md).
