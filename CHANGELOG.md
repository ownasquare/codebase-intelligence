# Changelog

Notable changes to Codebase Intelligence are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-07-18

### Added

- A one-terminal, credential-free `make demo` workflow.
- A unified `codebase-intelligence` command for local service entry points.
- Getting-started, extension, release, support, and community contribution guides.
- Public documentation and packaging checks.

### Changed

- Simplified the workbench around **Ask**, **Source**, and **Repository**.
- Moved service detail and maintenance actions behind secondary disclosure.
- Made deterministic embeddings and extractive answers the fresh-install defaults.
- Clarified beta status, provider data flow, supported versions, and deployment boundaries.

### Fixed

- New repositories become the active workspace and open at the primary Ask view.
- Compose validation examples no longer render resolved configuration that may contain secrets.
- Package and API version references now agree.

## [0.2.0] - 2026-07-17

### Added

- Indexed-source browsing with repository-scoped path, symbol, language, and line metadata.
- Hybrid retrieval explanations and Markdown investigation export.
- Durable job leases, startup reconciliation, versioned collection publication, and deletion
  readback.
- Security regression coverage, container hardening, and operations documentation.

### Changed

- Advanced the parser and redaction structure contract; existing repositories require reindexing.

## [0.1.0] - 2026-07-17

### Added

- Initial FastAPI, Tree-sitter, LlamaIndex, Qdrant, and Streamlit implementation.
- GitHub and bounded ZIP ingestion.
- Repository-scoped retrieval with line-level citations.
- Deterministic local providers and optional Voyage/OpenAI integration.
