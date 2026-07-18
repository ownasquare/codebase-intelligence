from pathlib import Path

import pytest
from pydantic import ValidationError

from codebase_intelligence.config import Settings
from codebase_intelligence.models import GitHubRepositoryRequest


def test_runtime_paths_are_derived_from_data_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)

    assert settings.database_path == tmp_path / "manifest.sqlite3"
    assert settings.repositories_dir == tmp_path / "repositories"
    assert settings.staging_dir == tmp_path / "staging"
    assert settings.qdrant_path == tmp_path / "qdrant"


def test_provider_readiness_is_truthful_without_credentials(tmp_path: Path) -> None:
    voyage = Settings(
        data_dir=tmp_path,
        embedding_provider="voyage",
        answer_provider="openai",
        voyage_api_key=None,
        openai_api_key=None,
    )
    demo = Settings(
        data_dir=tmp_path,
        embedding_provider="deterministic",
        answer_provider="extractive",
    )

    assert voyage.embedding_ready is False
    assert voyage.answer_ready is False
    assert demo.embedding_ready is True
    assert demo.answer_ready is True


def test_invalid_cors_protocol_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(allowed_origins=["file:///tmp/app"])


def test_ensure_directories_uses_private_runtime_roots(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "runtime")
    settings.ensure_directories()

    assert settings.repositories_dir.is_dir()
    assert settings.staging_dir.is_dir()
    assert settings.qdrant_path.is_dir()


def test_github_display_name_is_normalized_before_download() -> None:
    request = GitHubRepositoryRequest(
        url="https://github.com/example/repository",
        name="  Example   Repository  ",
    )
    assert request.name == "Example Repository"

    with pytest.raises(ValidationError):
        GitHubRepositoryRequest(
            url="https://github.com/example/repository",
            name="   ",
        )
