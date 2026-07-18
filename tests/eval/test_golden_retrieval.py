from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.chunker import CodeChunker
from codebase_intelligence.ingestion.file_filter import RepositoryScanner
from codebase_intelligence.vector_store import CodeVectorIndex

pytestmark = pytest.mark.integration


def test_golden_codebase_questions_retrieve_expected_files(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        data_dir=tmp_path / "runtime",
        embedding_provider="deterministic",
        deterministic_embedding_dimension=128,
        answer_provider="extractive",
    )
    fixture_root = Path(__file__).parents[1] / "fixtures" / "sample_repo"
    scanner = RepositoryScanner(settings)
    chunker = CodeChunker(settings)
    scan = scanner.scan(fixture_root)
    chunks = [
        chunk
        for source_file in scan.files
        for chunk in chunker.chunk_file(source_file, "fixture", None)
    ]
    golden = json.loads(
        (Path(__file__).with_name("golden_questions.json")).read_text(encoding="utf-8")
    )

    index = CodeVectorIndex(settings)
    try:
        collection = index.index("fixture", chunks)
        for case in golden:
            results = index.search(
                "fixture",
                case["question"],
                top_k=8,
                collection_name=collection,
            )
            retrieved_paths = {result.chunk.path for result in results}
            assert results[0].chunk.path in case["expected_paths"]
            assert retrieved_paths.intersection(case["expected_paths"]), (
                case["question"],
                retrieved_paths,
            )
    finally:
        index.close()
