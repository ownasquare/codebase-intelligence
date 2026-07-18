"""Typed configuration with conservative ingestion and provider defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from ``CODEBASE_INTEL_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="CODEBASE_INTEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_name: str = "Codebase Intelligence"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: Path = Path(".data")

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_base_url: str = "http://127.0.0.1:8000"
    ui_host: str = "127.0.0.1"
    ui_port: int = Field(default=8501, ge=1, le=65535)
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:8501", "http://localhost:8501"]
    )
    api_key: SecretStr | None = None

    embedding_provider: Literal["voyage", "openai", "deterministic"] = "deterministic"
    voyage_embedding_model: str = "voyage-code-3"
    voyage_output_dimension: int = Field(default=1024, ge=256, le=2048)
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimension: int = Field(default=1536, ge=256, le=3072)
    deterministic_embedding_dimension: int = Field(default=384, ge=64, le=2048)
    embedding_batch_size: int = Field(default=64, ge=1, le=256)
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    answer_provider: Literal["openai", "extractive"] = "extractive"
    openai_chat_model: str = "gpt-5-mini"
    answer_timeout_seconds: float = Field(default=90.0, ge=1.0, le=300.0)

    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_collection_prefix: str = "codebase_intel"

    inline_worker: bool = True
    worker_poll_seconds: float = Field(default=0.5, ge=0.1, le=30.0)
    worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)

    github_api_url: str = "https://api.github.com"
    github_download_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    max_archive_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_extracted_bytes: int = Field(default=500 * 1024 * 1024, ge=1024)
    max_archive_expansion_ratio: int = Field(default=100, ge=2, le=1000)
    max_files: int = Field(default=25_000, ge=1, le=200_000)
    max_file_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_indexable_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    max_chunks: int = Field(default=100_000, ge=1, le=1_000_000)
    max_path_length: int = Field(default=1024, ge=128, le=4096)
    max_path_depth: int = Field(default=40, ge=2, le=100)

    chunk_lines: int = Field(default=80, ge=10, le=500)
    chunk_line_overlap: int = Field(default=10, ge=0, le=100)
    chunk_max_chars: int = Field(default=6000, ge=500, le=40_000)
    default_top_k: int = Field(default=8, ge=1, le=20)
    max_top_k: int = Field(default=20, ge=1, le=100)
    max_question_chars: int = Field(default=4000, ge=100, le=20_000)

    @field_validator("qdrant_url", mode="before")
    @classmethod
    def empty_url_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("allowed_origins")
    @classmethod
    def origins_must_be_http(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("http://", "https://")):
                raise ValueError("allowed origins must use http or https")
        return values

    @property
    def database_path(self) -> Path:
        return self.data_dir / "manifest.sqlite3"

    @property
    def repositories_dir(self) -> Path:
        return self.data_dir / "repositories"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def qdrant_path(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def embedding_dimension(self) -> int:
        if self.embedding_provider == "voyage":
            return self.voyage_output_dimension
        if self.embedding_provider == "openai":
            return self.openai_embedding_dimension
        return self.deterministic_embedding_dimension

    @property
    def embedding_ready(self) -> bool:
        if self.embedding_provider == "voyage":
            return self.voyage_api_key is not None and bool(self.voyage_api_key.get_secret_value())
        if self.embedding_provider == "openai":
            return self.openai_api_key is not None and bool(self.openai_api_key.get_secret_value())
        return True

    @property
    def answer_ready(self) -> bool:
        if self.answer_provider == "extractive":
            return True
        return self.openai_api_key is not None and bool(self.openai_api_key.get_secret_value())

    def ensure_directories(self) -> None:
        """Create private runtime directories without touching repository source."""

        for path in (self.data_dir, self.repositories_dir, self.staging_dir, self.qdrant_path):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
