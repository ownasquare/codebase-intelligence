"""Release gates for the public documentation surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PRIVATE_DOC_DIRECTORIES = {"handoffs", "superpowers"}


def _public_markdown_files() -> list[Path]:
    top_level = [
        ROOT / name
        for name in (
            "README.md",
            "CHANGELOG.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "SUPPORT.md",
        )
        if (ROOT / name).exists()
    ]
    docs = [
        path
        for path in (ROOT / "docs").rglob("*.md")
        if PRIVATE_DOC_DIRECTORIES.isdisjoint(path.relative_to(ROOT / "docs").parts)
    ]
    github = list((ROOT / ".github").rglob("*.md"))
    return sorted({*top_level, *docs, *github})


def test_local_markdown_links_resolve() -> None:
    broken: list[str] = []
    for document in _public_markdown_files():
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {raw_target}")
    assert not broken, "Broken local documentation links:\n" + "\n".join(broken)


def test_public_docs_do_not_leak_internal_workspace_context() -> None:
    forbidden = ("/Users/", "/Volumes/", "Beladed", ".handoff.mdc", "Codex task")
    leaks: list[str] = []
    for document in _public_markdown_files():
        text = document.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                leaks.append(f"{document.relative_to(ROOT)} contains {marker!r}")
    assert not leaks, "Public documentation contains internal context:\n" + "\n".join(leaks)


def test_readme_is_a_concise_product_entry_point() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(readme.splitlines()) <= 180
    for expected in ("make demo", "Ask", "Source", "beta"):
        assert expected in readme


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (ROOT / "src/codebase_intelligence/__init__.py").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    version = project["project"]["version"]
    assert version == "0.3.0"
    assert f'__version__ = "{version}"' in package_init
    assert "0.3.x" in security


def test_example_environment_starts_without_paid_credentials() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CODEBASE_INTEL_EMBEDDING_PROVIDER=deterministic" in example
    assert "CODEBASE_INTEL_ANSWER_PROVIDER=extractive" in example


def test_compose_validation_examples_do_not_render_secrets() -> None:
    unsafe: list[str] = []
    for document in _public_markdown_files():
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            if "docker compose config" in line and "--quiet" not in line:
                unsafe.append(f"{document.relative_to(ROOT)}:{number}")
    assert not unsafe, "Use `docker compose config --quiet` at: " + ", ".join(unsafe)
