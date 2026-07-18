from __future__ import annotations

from pathlib import Path

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.chunker import CodeChunker
from codebase_intelligence.ingestion.file_filter import RepositoryScanner
from codebase_intelligence.ingestion.language_registry import LanguageRegistry


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "embedding_provider": "deterministic",
        "answer_provider": "extractive",
        "max_archive_bytes": 64 * 1024,
        "max_extracted_bytes": 128 * 1024,
        "max_file_bytes": 16 * 1024,
        "max_files": 50,
        "max_indexable_bytes": 128 * 1024,
        "chunk_lines": 10,
        "chunk_line_overlap": 2,
        "chunk_max_chars": 500,
    }
    values.update(overrides)
    return Settings(**values)


def test_python_symbol_has_exact_name_lines_and_text(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\n"
        "\n"
        "def authenticate(user):\n"
        "    active = user.active\n"
        "    return active\n"
    )

    chunks = CodeChunker(settings(tmp_path)).chunk("auth.py", source)
    symbol = next(chunk for chunk in chunks if chunk.symbol == "authenticate")

    assert (symbol.start_line, symbol.end_line) == (3, 5)
    assert symbol.language == "python"
    assert symbol.symbol_kind == "function"
    assert symbol.parser == "tree_sitter"
    assert symbol.text == ("def authenticate(user):\n    active = user.active\n    return active")


def test_class_and_method_are_separate_semantic_chunks(tmp_path: Path) -> None:
    source = "class AuthService:\n    def login(self, user):\n        return user.active\n"

    chunks = CodeChunker(settings(tmp_path)).chunk("auth.py", source)
    symbols = {(chunk.symbol, chunk.symbol_kind) for chunk in chunks}

    assert ("AuthService", "class") in symbols
    assert ("login", "method") in symbols
    assert all(chunk.parser == "tree_sitter" for chunk in chunks)


def test_redaction_cannot_hide_later_top_level_python_symbols(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/sample_repo/src/auth.py")

    chunks = CodeChunker(settings(tmp_path)).chunk("auth.py", fixture.read_text())
    symbols = {
        (chunk.symbol, chunk.symbol_kind, chunk.start_line, chunk.end_line) for chunk in chunks
    }

    assert ("User", "class", 7, 9) in symbols
    assert ("AuthenticationError", "class", 12, 13) in symbols
    assert ("authenticate_bearer_token", "function", 16, 24) in symbols
    authentication = next(chunk for chunk in chunks if chunk.symbol == "authenticate_bearer_token")
    assert "token: str" in authentication.text


def test_original_structure_keeps_nested_methods_distinct_and_redacted(tmp_path: Path) -> None:
    secret = "example-super-sensitive-api-value"
    source = (
        "class AuthService:\n"
        "    def verify(self, token: str) -> bool:\n"
        f'        api_key = "{secret}"\n'
        "        return bool(token and api_key)\n"
    )

    chunks = CodeChunker(settings(tmp_path)).chunk("auth.py", source)
    identities = [
        (chunk.symbol, chunk.symbol_kind, chunk.start_line, chunk.end_line) for chunk in chunks
    ]

    assert identities.count(("AuthService", "class", 1, 4)) == 1
    assert identities.count(("verify", "method", 2, 4)) == 1
    assert all(secret not in chunk.text for chunk in chunks)
    assert all("[REDACTED:ASSIGNMENT]" in chunk.text for chunk in chunks)


def test_typescript_function_uses_language_pack_parser(tmp_path: Path) -> None:
    source = "export function capturePayment(amount: number): boolean {\n  return amount > 0;\n}\n"

    chunks = CodeChunker(settings(tmp_path)).chunk("payments.ts", source)
    function = next(chunk for chunk in chunks if chunk.symbol == "capturePayment")

    assert function.language == "typescript"
    assert function.parser == "tree_sitter"
    assert (function.start_line, function.end_line) == (1, 3)


def test_c_declarator_name_is_extracted_from_nested_tree_node(tmp_path: Path) -> None:
    source = "int authenticate(int active) {\n  return active;\n}\n"

    chunks = CodeChunker(settings(tmp_path)).chunk("auth.c", source)
    function = next(chunk for chunk in chunks if chunk.symbol == "authenticate")

    assert function.language == "c"
    assert function.symbol_kind == "function"
    assert function.parser == "tree_sitter"
    assert (function.start_line, function.end_line) == (1, 3)


def test_prose_uses_deterministic_overlapping_line_fallback(tmp_path: Path) -> None:
    source = "".join(f"line {line}\n" for line in range(1, 26))

    chunks = CodeChunker(settings(tmp_path)).chunk("README.md", source)

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [
        (1, 10),
        (9, 18),
        (17, 25),
    ]
    assert all(chunk.parser == "fallback" for chunk in chunks)
    assert chunks[0].text.startswith("line 1\n")
    assert chunks[-1].text.endswith("line 25\n")


def test_single_long_line_is_split_without_inventing_line_numbers(tmp_path: Path) -> None:
    source = ("x" * 1200) + "\n"

    chunks = CodeChunker(settings(tmp_path)).chunk("notes.txt", source)

    assert len(chunks) == 3
    assert all((chunk.start_line, chunk.end_line) == (1, 1) for chunk in chunks)
    assert all(len(chunk.text) <= 500 for chunk in chunks)


def test_chunk_text_is_redacted_before_it_can_leave_ingestion(tmp_path: Path) -> None:
    secret = "example-super-sensitive-api-value"
    source = f'def configured_client():\n    api_key = "{secret}"\n    return api_key\n'

    chunks = CodeChunker(settings(tmp_path)).chunk("client.py", source)

    assert chunks
    assert all(secret not in chunk.text for chunk in chunks)
    assert any("[REDACTED:ASSIGNMENT]" in chunk.text for chunk in chunks)
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 3


def test_chunk_file_uses_scanner_contract_and_stable_ids(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("def authenticate():\n    return True\n")
    configured = settings(tmp_path)
    source_file = RepositoryScanner(configured).scan(root).files[0]
    chunker = CodeChunker(configured)

    first = chunker.chunk_file(source_file, "repo-1", "b" * 40)
    second = chunker.chunk_file(source_file, "repo-1", "b" * 40)

    assert first == second
    assert first[0].repository_id == "repo-1"
    assert first[0].commit_sha == "b" * 40
    assert first[0].path == "auth.py"
    assert len(first[0].id) == 64
    assert len(first[0].content_hash) == 64


def test_language_registry_covers_required_codebase_languages() -> None:
    registry = LanguageRegistry()
    expected = {
        "main.py": "python",
        "app.js": "javascript",
        "app.ts": "typescript",
        "app.tsx": "tsx",
        "Main.java": "java",
        "Main.kt": "kotlin",
        "main.go": "go",
        "main.rs": "rust",
        "main.c": "c",
        "main.cpp": "cpp",
        "Main.cs": "csharp",
        "main.rb": "ruby",
        "main.php": "php",
        "main.swift": "swift",
        "main.scala": "scala",
        "main.sh": "bash",
        "query.sql": "sql",
        "index.html": "html",
        "style.css": "css",
        "data.json": "json",
        "config.yaml": "yaml",
        "config.toml": "toml",
        "README.md": "markdown",
        "Dockerfile.prod": "dockerfile",
        "main.tf": "terraform",
    }

    assert {path: registry.language_for_path(path) for path in expected} == expected
