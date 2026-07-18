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
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from codebase_intelligence.config import Settings
from codebase_intelligence.models import CodeChunk
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


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk: CodeChunk
    score: float | None


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
    def _hybrid_rank(cls, query: str, result: RetrievedChunk) -> float:
        """Fuse semantic similarity with code-aware path/symbol lexical evidence."""

        query_terms = cls._lexemes(query, remove_stop_words=True)
        if not query_terms:
            return result.score or 0.0
        chunk = result.chunk
        path_terms = cls._lexemes(chunk.path)
        symbol_terms = cls._lexemes(chunk.symbol or "")
        content_terms = cls._lexemes(chunk.text)
        path_overlap = len(query_terms & path_terms) / len(query_terms)
        symbol_overlap = len(query_terms & symbol_terms) / len(query_terms)
        content_overlap = len(query_terms & content_terms) / len(query_terms)
        return (
            (result.score or 0.0) + (2.5 * path_overlap) + symbol_overlap + (0.75 * content_overlap)
        )

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
            metadata = node.metadata
            if metadata.get("repository_id") != repository_id:
                raise RuntimeError("Qdrant returned a node outside the requested repository scope")
            chunk = CodeChunk(
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
            retrieved.append(RetrievedChunk(chunk=chunk, score=score))
        ranked = sorted(
            enumerate(retrieved),
            key=lambda item: (-self._hybrid_rank(query, item[1]), item[0]),
        )
        return [result for _, result in ranked[:top_k]]

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
