from __future__ import annotations

from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.file_filter import RepositoryScanner, ScanLimitError


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "embedding_provider": "deterministic",
        "answer_provider": "extractive",
        "max_archive_bytes": 64 * 1024,
        "max_extracted_bytes": 128 * 1024,
        "max_file_bytes": 4 * 1024,
        "max_files": 50,
        "max_indexable_bytes": 64 * 1024,
    }
    values.update(overrides)
    return Settings(**values)


def test_scanner_applies_gitignore_and_security_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "nested").mkdir()
    (root / "node_modules/pkg").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "src/app.py").write_text("def app():\n    return True\n")
    (root / "ignored.py").write_text("raise RuntimeError\n")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "nested/.gitignore").write_text("*.tmp\n!keep.tmp\n")
    (root / "nested/drop.tmp").write_text("drop\n")
    (root / "nested/keep.tmp").write_text("keep\n")
    (root / "node_modules/pkg/index.js").write_text("export default 1\n")
    (root / "build/output.py").write_text("print('generated')\n")
    (root / ".env.production").write_text("API_KEY=do-not-index\n")
    (root / "package-lock.json").write_text("{}\n")
    (root / "bundle.min.js").write_text("const x=1;\n")
    (root / "image.bin").write_bytes(b"\x00\x01\x02")
    (root / "large.py").write_text("x" * 5000)

    result = RepositoryScanner(settings(tmp_path)).scan(root)
    selected = {source.relative_path for source in result.files}

    assert "src/app.py" in selected
    assert "nested/keep.tmp" in selected
    assert "ignored.py" not in selected
    assert "nested/drop.tmp" not in selected
    assert "node_modules/pkg/index.js" not in selected
    assert "build/output.py" not in selected
    assert ".env.production" not in selected
    assert "package-lock.json" not in selected
    assert "bundle.min.js" not in selected
    assert "image.bin" not in selected
    assert "large.py" not in selected
    assert result.skip_reasons["gitignore"] == 2
    assert result.skip_reasons["secret_file"] == 1
    assert result.skip_reasons["lockfile"] == 1
    assert result.skip_reasons["minified"] == 1
    assert result.skip_reasons["binary_extension"] == 1
    assert result.skip_reasons["too_large"] == 1


def test_scanner_detects_languages_and_returns_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate():\n    return True\n")
    (root / "app.tsx").write_text("export function App() { return <main />; }\n")
    (root / "main.tf").write_text('resource "x" "y" {}\n')
    (root / "Dockerfile").write_text("FROM scratch\n")

    result = RepositoryScanner(settings(tmp_path)).scan(root)
    by_path = {source.relative_path: source for source in result.files}

    assert by_path["auth.py"].language == "python"
    assert by_path["app.tsx"].language == "tsx"
    assert by_path["main.tf"].language == "terraform"
    assert by_path["Dockerfile"].language == "dockerfile"
    assert all(source.path.is_absolute() for source in result.files)
    assert result.file_count == 4
    assert result.indexed_bytes == sum(source.size for source in result.files)


def test_scanner_skips_symlinks_instead_of_following_them(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 'outside'\n")
    (root / "linked.py").symlink_to(outside)
    (root / "safe.py").write_text("SAFE = True\n")

    result = RepositoryScanner(settings(tmp_path)).scan(root)

    assert [source.relative_path for source in result.files] == ["safe.py"]
    assert result.skip_reasons["symlink"] == 1


def test_scanner_rejects_excess_indexable_bytes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "one.py").write_text("a" * 700)
    (root / "two.py").write_text("b" * 700)

    with pytest.raises(ScanLimitError, match="too much"):
        RepositoryScanner(settings(tmp_path, max_file_bytes=1024, max_indexable_bytes=1024)).scan(
            root
        )


def test_scanner_rejects_excess_indexable_file_count(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "one.py").write_text("one = 1\n")
    (root / "two.py").write_text("two = 2\n")

    with pytest.raises(ScanLimitError, match="too many"):
        RepositoryScanner(settings(tmp_path, max_files=1)).scan(root)
