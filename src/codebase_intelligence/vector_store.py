"""Repository-isolated Qdrant storage through the LlamaIndex vector-store contract."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.vector_stores import VectorStoreQuery
from llama_index.core.vector_stores.utils import metadata_dict_to_node
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from codebase_intelligence.config import Settings
from codebase_intelligence.models import (
    CodeChunk,
    RetrievalSignals,
    SourceFileSummary,
    SourceSection,
)
from codebase_intelligence.providers import create_embedding_model

_LEXEME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_QUERY_STOP_WORDS = {
    "does",
    "flow",
    "from",
    "have",
    "logic",
    "that",
    "the",
    "this",
    "what",
    "where",
    "which",
    "with",
    "work",
}
_SOURCE_SCROLL_PAGE_SIZE = 256
_MAX_SOURCE_POINTS = 10_000
_MAX_SOURCE_CHUNKS = 64
_MAX_SOURCE_LINES = 400


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk: CodeChunk
    score: float | None
    retrieval_signals: RetrievalSignals | None = None


class CodeVectorIndex:
    """A vector index whose public methods always require a repository ID."""

    def __init__(
        self,
        settings: Settings,
        *,
        embed_model: BaseEmbedding | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.settings = settings
        self._lock = RLock()
        self.embed_model = embed_model or create_embedding_model(settings)
        if client is not None:
            self.client = client
        elif settings.qdrant_url:
            api_key = (
                settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
            )
            self.client = QdrantClient(url=settings.qdrant_url, api_key=api_key)
        else:
            settings.qdrant_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(settings.qdrant_path))

    def collection_name(self, repository_id: str) -> str:
        opaque_id = "".join(character for character in repository_id.lower() if character.isalnum())
        if not opaque_id:
            raise ValueError("repository_id must contain at least one alphanumeric character")
        return f"{self.settings.qdrant_collection_prefix}_{opaque_id}"

    def has_collection(
        self,
        repository_id: str,
        *,
        collection_name: str | None = None,
    ) -> bool:
        target = collection_name or self.collection_name(repository_id)
        with self._lock:
            return self.client.collection_exists(target)

    def _store(
        self,
        repository_id: str,
        *,
        collection_name: str | None = None,
    ) -> QdrantVectorStore:
        return QdrantVectorStore(
            collection_name=collection_name or self.collection_name(repository_id),
            client=self.client,
            batch_size=self.settings.embedding_batch_size,
            flat_metadata=True,
        )

    @staticmethod
    def _embedding_text(chunk: CodeChunk) -> str:
        symbol = f"\nSymbol: {chunk.symbol}" if chunk.symbol else ""
        return (
            f"Path: {chunk.path}\nLanguage: {chunk.language}{symbol}\n"
            f"Lines: {chunk.start_line}-{chunk.end_line}\n\n{chunk.text}"
        )

    @staticmethod
    def _metadata(chunk: CodeChunk) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "chunk_id": chunk.id,
            "repository_id": chunk.repository_id,
            "path": chunk.path,
            "language": chunk.language,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "parser": chunk.parser,
            "content_hash": chunk.content_hash,
        }
        for key, value in (
            ("commit_sha", chunk.commit_sha),
            ("symbol", chunk.symbol),
            ("symbol_kind", chunk.symbol_kind),
        ):
            if value is not None:
                metadata[key] = value
        return metadata

    @staticmethod
    def _lexemes(text: str, *, remove_stop_words: bool = False) -> set[str]:
        terms = {
            token.casefold()[:4] if len(token) >= 4 else token.casefold()
            for token in _LEXEME_PATTERN.findall(text)
        }
        if remove_stop_words:
            terms -= {word[:4] if len(word) >= 4 else word for word in _QUERY_STOP_WORDS}
        return terms

    @classmethod
    def _retrieval_signals(cls, query: str, result: RetrievedChunk) -> RetrievalSignals:
        """Return the semantic and lexical components used for stable ranking."""

        query_terms = cls._lexemes(query, remove_stop_words=True)
        chunk = result.chunk
        if query_terms:
            path_overlap = len(query_terms & cls._lexemes(chunk.path)) / len(query_terms)
            symbol_overlap = len(query_terms & cls._lexemes(chunk.symbol or "")) / len(query_terms)
            content_overlap = len(query_terms & cls._lexemes(chunk.text)) / len(query_terms)
        else:
            path_overlap = symbol_overlap = content_overlap = 0.0
        semantic = None if result.score is None else max(-1.0, min(1.0, result.score))
        combined = (
            (semantic or 0.0) + (2.5 * path_overlap) + symbol_overlap + (0.75 * content_overlap)
        )
        return RetrievalSignals(
            semantic_score=semantic,
            combined_score=combined,
            path_overlap=path_overlap,
            symbol_overlap=symbol_overlap,
            content_overlap=content_overlap,
        )

    @staticmethod
    def _chunk_from_node(repository_id: str, node: BaseNode) -> CodeChunk:
        metadata = node.metadata
        if metadata.get("repository_id") != repository_id:
            raise RuntimeError("Qdrant returned a node outside the requested repository scope")
        return CodeChunk(
            id=str(metadata["chunk_id"]),
            repository_id=repository_id,
            commit_sha=metadata.get("commit_sha"),
            path=str(metadata["path"]),
            language=str(metadata["language"]),
            symbol=metadata.get("symbol"),
            symbol_kind=metadata.get("symbol_kind"),
            start_line=int(metadata["start_line"]),
            end_line=int(metadata["end_line"]),
            parser=str(metadata["parser"]),
            text=node.get_content(),
            content_hash=str(metadata["content_hash"]),
        )

    def _scroll_chunks(
        self,
        repository_id: str,
        *,
        collection_name: str,
        path: str | None = None,
        max_points: int = _MAX_SOURCE_POINTS,
    ) -> tuple[list[CodeChunk], bool]:
        """Read bounded, repository-filtered redacted nodes from one active collection."""

        conditions = [
            models.FieldCondition(
                key="repository_id",
                match=models.MatchValue(value=repository_id),
            )
        ]
        if path is not None:
            conditions.append(
                models.FieldCondition(key="path", match=models.MatchValue(value=path))
            )
        query_filter = models.Filter(must=conditions)
        offset: models.ExtendedPointId | None = None
        chunks: list[CodeChunk] = []
        truncated = False
        with self._lock:
            if not self.client.collection_exists(collection_name):
                return [], False
            while len(chunks) < max_points:
                page_limit = min(_SOURCE_SCROLL_PAGE_SIZE, max_points - len(chunks))
                records, next_offset = self.client.scroll(
                    collection_name=collection_name,
                    scroll_filter=query_filter,
                    limit=page_limit,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload
                    if payload is None:
                        continue
                    node = metadata_dict_to_node(dict(payload))
                    chunks.append(self._chunk_from_node(repository_id, node))
                if next_offset is None:
                    break
                offset = next_offset
            else:
                truncated = True
        return chunks, truncated

    def index(self, repository_id: str, chunks: Iterable[CodeChunk]) -> str:
        """Build a versioned collection in bounded batches without touching the active one."""

        collection = f"{self.collection_name(repository_id)}_{uuid4().hex[:12]}"
        with self._lock:
            self.client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=self.settings.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )
            try:
                indexed_any = False
                for chunk_batch in batched(chunks, self.settings.embedding_batch_size):
                    if any(chunk.repository_id != repository_id for chunk in chunk_batch):
                        raise ValueError("every chunk must belong to the requested repository")
                    indexed_any = True
                    texts = [self._embedding_text(chunk) for chunk in chunk_batch]
                    embeddings = self.embed_model.get_text_embedding_batch(
                        texts,
                        show_progress=False,
                    )
                    nodes: list[BaseNode] = []
                    for chunk, embedding in zip(chunk_batch, embeddings, strict=True):
                        node = TextNode(
                            id_=str(uuid5(NAMESPACE_URL, f"{repository_id}:{chunk.id}")),
                            text=chunk.text,
                            metadata=self._metadata(chunk),
                            embedding=embedding,
                        )
                        nodes.append(node)
                    self._store(repository_id, collection_name=collection).add(nodes)
                if not indexed_any:
                    raise ValueError("at least one chunk is required")
            except Exception:
                if self.client.collection_exists(collection):
                    self.client.delete_collection(collection)
                raise
        return collection

    def search(
        self,
        repository_id: str,
        query: str,
        *,
        top_k: int,
        collection_name: str | None = None,
    ) -> list[RetrievedChunk]:
        if top_k < 1 or top_k > self.settings.max_top_k:
            raise ValueError(f"top_k must be between 1 and {self.settings.max_top_k}")
        candidate_k = min(100, max(top_k, top_k * 4, self.settings.max_top_k))
        target = collection_name or self.collection_name(repository_id)
        with self._lock:
            if not self.client.collection_exists(target):
                return []
            query_embedding = self.embed_model.get_query_embedding(query)
            result = self._store(repository_id, collection_name=target).query(
                VectorStoreQuery(query_embedding=query_embedding, similarity_top_k=candidate_k)
            )
        nodes = result.nodes or []
        similarities: list[float | None] = (
            list(result.similarities) if result.similarities is not None else [None] * len(nodes)
        )
        retrieved: list[RetrievedChunk] = []
        for node, score in zip(nodes, similarities, strict=True):
            chunk = self._chunk_from_node(repository_id, node)
            base_result = RetrievedChunk(chunk=chunk, score=score)
            retrieved.append(
                RetrievedChunk(
                    chunk=chunk,
                    score=score,
                    retrieval_signals=self._retrieval_signals(query, base_result),
                )
            )
        ranked = sorted(
            enumerate(retrieved),
            key=lambda item: (
                -(
                    item[1].retrieval_signals.combined_score
                    if item[1].retrieval_signals is not None
                    else 0.0
                ),
                item[0],
            ),
        )
        return [result for _, result in ranked[:top_k]]

    def list_sources(
        self,
        repository_id: str,
        *,
        collection_name: str,
        query: str | None = None,
        language: str | None = None,
        limit: int = 200,
    ) -> tuple[list[SourceFileSummary], int]:
        """List file summaries reconstructed from the active redacted index."""

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        chunks, _ = self._scroll_chunks(
            repository_id,
            collection_name=collection_name,
        )
        normalized_language = language.casefold() if language else None
        candidates = [
            chunk
            for chunk in chunks
            if normalized_language is None or chunk.language.casefold() == normalized_language
        ]
        normalized_query = query.casefold().strip() if query else ""
        if normalized_query:
            matching_paths = {
                chunk.path
                for chunk in candidates
                if normalized_query
                in f"{chunk.path}\n{chunk.symbol or ''}\n{chunk.text}".casefold()
            }
            candidates = [chunk for chunk in candidates if chunk.path in matching_paths]

        grouped: dict[str, list[CodeChunk]] = {}
        for chunk in candidates:
            grouped.setdefault(chunk.path, []).append(chunk)
        summaries: list[SourceFileSummary] = []
        for path, file_chunks in grouped.items():
            symbols = {chunk.symbol for chunk in file_chunks if chunk.symbol}
            summaries.append(
                SourceFileSummary(
                    path=path,
                    language=file_chunks[0].language,
                    chunk_count=len(file_chunks),
                    symbol_count=len(symbols),
                    start_line=min(chunk.start_line for chunk in file_chunks),
                    end_line=max(chunk.end_line for chunk in file_chunks),
                )
            )
        summaries.sort(key=lambda source: (source.path.casefold(), source.path))
        return summaries[:limit], len(summaries)

    def get_source(
        self,
        repository_id: str,
        *,
        collection_name: str,
        path: str,
    ) -> tuple[list[SourceSection], bool] | None:
        """Return bounded redacted sections for one exact repository-relative path."""

        chunks, scroll_truncated = self._scroll_chunks(
            repository_id,
            collection_name=collection_name,
            path=path,
            max_points=_MAX_SOURCE_CHUNKS + 1,
        )
        if not chunks:
            return None
        chunks.sort(key=lambda chunk: (chunk.start_line, chunk.end_line, chunk.id))
        selected: list[CodeChunk] = []
        selected_lines = 0
        for chunk in chunks[:_MAX_SOURCE_CHUNKS]:
            line_count = chunk.end_line - chunk.start_line + 1
            if selected and selected_lines + line_count > _MAX_SOURCE_LINES:
                break
            selected.append(chunk)
            selected_lines += line_count
        sections = [
            SourceSection(
                chunk_id=chunk.id,
                path=chunk.path,
                language=chunk.language,
                symbol=chunk.symbol,
                symbol_kind=chunk.symbol_kind,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                parser=chunk.parser,
                content=chunk.text,
            )
            for chunk in selected
        ]
        truncated = scroll_truncated or len(selected) < len(chunks)
        return sections, truncated

    def delete(
        self,
        repository_id: str,
        *,
        collection_name: str | None = None,
    ) -> bool:
        collection = collection_name or self.collection_name(repository_id)
        with self._lock:
            if not self.client.collection_exists(collection):
                return False
            self.client.delete_collection(collection)
            return True

    def repository_collections(self, repository_id: str) -> list[str]:
        """List physical collections for a repository across configured-prefix changes."""

        opaque_id = "".join(character for character in repository_id.lower() if character.isalnum())
        if not opaque_id:
            raise ValueError("repository_id must contain at least one alphanumeric character")
        repository_marker = f"_{opaque_id}_"
        with self._lock:
            collections = self.client.get_collections().collections
        return sorted(
            collection.name for collection in collections if repository_marker in collection.name
        )

    def healthcheck(self) -> bool:
        with self._lock:
            self.client.get_collections()
        return True

    def close(self) -> None:
        with self._lock:
            self.client.close()
