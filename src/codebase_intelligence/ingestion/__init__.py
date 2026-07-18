"""Public secure-ingestion API."""

from codebase_intelligence.ingestion.chunker import ChunkingError, ChunkLimitError, CodeChunker
from codebase_intelligence.ingestion.file_filter import (
    FileFilter,
    RepositoryScanner,
    ScanError,
    ScanLimitError,
    ScanResult,
    SourceFile,
)
from codebase_intelligence.ingestion.language_registry import (
    DEFAULT_LANGUAGE_REGISTRY,
    DEFAULT_LANGUAGE_SPECS,
    TEXT_LANGUAGE,
    LanguageRegistry,
    LanguageSpec,
)
from codebase_intelligence.ingestion.redaction import (
    RedactionResult,
    SecretRedactionError,
    redact_secrets,
)
from codebase_intelligence.ingestion.source_loader import (
    ArchiveLimitError,
    GitHubDownload,
    GitHubRepository,
    GitHubRequestError,
    GitHubSourceLoader,
    IngestionError,
    InvalidSourceError,
    SafeArchiveExtractor,
    UnsafeArchiveError,
)

__all__ = [
    "DEFAULT_LANGUAGE_REGISTRY",
    "DEFAULT_LANGUAGE_SPECS",
    "TEXT_LANGUAGE",
    "ArchiveLimitError",
    "ChunkLimitError",
    "ChunkingError",
    "CodeChunker",
    "FileFilter",
    "GitHubDownload",
    "GitHubRepository",
    "GitHubRequestError",
    "GitHubSourceLoader",
    "IngestionError",
    "InvalidSourceError",
    "LanguageRegistry",
    "LanguageSpec",
    "RedactionResult",
    "RepositoryScanner",
    "SafeArchiveExtractor",
    "ScanError",
    "ScanLimitError",
    "ScanResult",
    "SecretRedactionError",
    "SourceFile",
    "UnsafeArchiveError",
    "redact_secrets",
]
