"""Repository-scoped retrieval and citation-validated answer synthesis."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import quote

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import (
    IndexMissingError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from codebase_intelligence.models import (
    Citation,
    QuestionRequest,
    QuestionResponse,
    RepositoryRecord,
    RepositoryStatus,
)
from codebase_intelligence.observability import get_logger
from codebase_intelligence.prompts import build_grounded_prompt
from codebase_intelligence.providers import CompletionProvider, index_fingerprint
from codebase_intelligence.vector_store import CodeVectorIndex, RetrievedChunk

_CITATION_PATTERN = re.compile(r"\[S(\d+)]")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP_WORDS = {
    "and",
    "are",
    "does",
    "flow",
    "for",
    "from",
    "how",
    "logic",
    "the",
    "this",
    "what",
    "where",
    "which",
    "with",
}
logger = get_logger(__name__)


class RepositoryLookup(Protocol):
    def get_repository(self, repository_id: str) -> RepositoryRecord | None: ...


class RAGService:
    """Answer questions without ever exposing an unscoped vector search."""

    def __init__(
        self,
        settings: Settings,
        repositories: RepositoryLookup,
        vector_index: CodeVectorIndex,
        completion_provider: CompletionProvider | None,
    ) -> None:
        self.settings = settings
        self.repositories = repositories
        self.vector_index = vector_index
        self.completion_provider = completion_provider

    async def ask(self, repository_id: str, request: QuestionRequest) -> QuestionResponse:
        repository = self.repositories.get_repository(repository_id)
        if repository is None:
            raise ResourceNotFoundError("Repository")
        if repository.status is not RepositoryStatus.READY:
            raise ResourceConflictError("Repository must be ready before it can answer questions.")
        if not repository.collection_name or repository.index_fingerprint != index_fingerprint(
            self.settings
        ):
            raise ResourceConflictError(
                "Repository index settings have changed; reindex before asking questions."
            )
        if not self.vector_index.has_collection(
            repository_id,
            collection_name=repository.collection_name,
        ):
            raise IndexMissingError

        top_k = min(request.top_k, self.settings.max_top_k)
        retrieved = await asyncio.to_thread(
            self.vector_index.search,
            repository_id,
            request.question,
            top_k=top_k,
            collection_name=repository.collection_name,
        )
        if not retrieved:
            return self._insufficient_response(repository_id, request.question)

        citations = self._build_citations(repository, retrieved)
        if self.settings.answer_provider == "extractive" or self.completion_provider is None:
            if not self._has_lexical_evidence(request.question, retrieved):
                return self._insufficient_response(repository_id, request.question)
            answer = self._extractive_answer(citations)
            mode = "extractive"
        else:
            prompt = build_grounded_prompt(request.question, retrieved, request.history)
            try:
                generated = await self.completion_provider.complete(prompt)
                answer = self._validate_or_fallback_answer(generated, citations)
                mode = "openai" if answer == generated else "extractive"
            except Exception as error:
                logger.warning(
                    "answer_provider_failed",
                    error_type=type(error).__name__,
                    repository_id=repository_id,
                )
                answer = self._extractive_answer(citations)
                mode = "extractive"

        return QuestionResponse(
            answer=answer,
            answer_mode=mode,
            citations=citations,
            repository_id=repository_id,
            question=request.question,
        )

    @staticmethod
    def _insufficient_response(repository_id: str, question: str) -> QuestionResponse:
        return QuestionResponse(
            answer="There is insufficient repository evidence to answer that question.",
            answer_mode="extractive",
            citations=[],
            repository_id=repository_id,
            question=question,
        )

    @staticmethod
    def _has_lexical_evidence(question: str, retrieved: Sequence[RetrievedChunk]) -> bool:
        query_terms = {
            term.lower()[:7]
            for term in _WORD_PATTERN.findall(question)
            if term.lower() not in _STOP_WORDS
        }
        if not query_terms:
            return True
        source_terms = {
            term.lower()[:7]
            for result in retrieved
            for term in _WORD_PATTERN.findall(
                f"{result.chunk.path} {result.chunk.symbol or ''} {result.chunk.text}"
            )
        }
        return bool(query_terms & source_terms)

    @staticmethod
    def _permalink(repository: RepositoryRecord, path: str, start: int, end: int) -> str | None:
        if not repository.source_url or not repository.commit_sha:
            return None
        if not repository.source_url.startswith("https://github.com/"):
            return None
        encoded_path = quote(path, safe="/")
        return (
            f"{repository.source_url.rstrip('/')}/blob/{repository.commit_sha}/{encoded_path}"
            f"#L{start}-L{end}"
        )

    def _build_citations(
        self,
        repository: RepositoryRecord,
        retrieved: Sequence[RetrievedChunk],
    ) -> list[Citation]:
        citations: list[Citation] = []
        seen: set[tuple[str, int, int]] = set()
        for number, result in enumerate(retrieved, start=1):
            chunk = result.chunk
            identity = (chunk.path, chunk.start_line, chunk.end_line)
            if identity in seen:
                continue
            seen.add(identity)
            excerpt = chunk.text.strip()
            if len(excerpt) > 1600:
                excerpt = f"{excerpt[:1597]}..."
            citations.append(
                Citation(
                    source_id=f"S{number}",
                    repository_id=repository.id,
                    commit_sha=chunk.commit_sha,
                    path=chunk.path,
                    language=chunk.language,
                    symbol=chunk.symbol,
                    symbol_kind=chunk.symbol_kind,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=result.score,
                    retrieval_signals=result.retrieval_signals,
                    excerpt=excerpt,
                    permalink=self._permalink(
                        repository, chunk.path, chunk.start_line, chunk.end_line
                    ),
                )
            )
        return citations

    @staticmethod
    def _extractive_answer(citations: Sequence[Citation]) -> str:
        if not citations:
            return "There is insufficient repository evidence to answer that question."
        lines = ["The strongest repository evidence is in these locations:"]
        for citation in citations[:5]:
            symbol = f" (`{citation.symbol}`)" if citation.symbol else ""
            lines.append(
                f"- `{citation.path}:{citation.start_line}-{citation.end_line}`{symbol} "
                f"[{citation.source_id}]"
            )
        lines.append(
            "Open the cited source sections to trace the implementation; synthesized explanation "
            "is disabled in extractive mode."
        )
        return "\n".join(lines)

    def _validate_or_fallback_answer(self, generated: str, citations: Sequence[Citation]) -> str:
        allowed = {citation.source_id for citation in citations}
        referenced = {f"S{number}" for number in _CITATION_PATTERN.findall(generated)}
        if not referenced or not referenced.issubset(allowed):
            return self._extractive_answer(citations)
        return generated.strip()[:12_000]
