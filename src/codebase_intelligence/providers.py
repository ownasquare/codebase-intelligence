"""Explicit LlamaIndex embedding and answer-provider factories."""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from typing import Protocol

from llama_index.core.embeddings import BaseEmbedding
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.embeddings.voyageai import VoyageEmbedding
from llama_index.llms.openai import OpenAI
from pydantic import Field

from codebase_intelligence.config import Settings
from codebase_intelligence.exceptions import ProviderUnavailableError

_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


class DeterministicEmbedding(BaseEmbedding):
    """Stable lexical embedding for offline tests and an explicitly labeled demo mode."""

    dimension: int = Field(default=384, ge=64, le=2048)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = [token.lower() for token in _TOKEN_PATTERN.findall(text)]
        features = tokens + [f"{left}:{right}" for left, right in pairwise(tokens)]
        for feature in features:
            digest = hashlib.blake2b(
                feature.encode("utf-8"), digest_size=8, usedforsecurity=False
            ).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]


def create_embedding_model(settings: Settings) -> BaseEmbedding:
    """Build a repository-scoped embedding model without mutating LlamaIndex globals."""

    if not settings.embedding_ready:
        raise ProviderUnavailableError(settings.embedding_provider)
    if settings.embedding_provider == "voyage":
        voyage_key = settings.voyage_api_key
        if voyage_key is None:
            raise ProviderUnavailableError("voyage")
        return VoyageEmbedding(
            model_name=settings.voyage_embedding_model,
            voyage_api_key=voyage_key.get_secret_value(),
            output_dimension=settings.voyage_output_dimension,
            output_dtype="float",
            truncation=True,
            embed_batch_size=settings.embedding_batch_size,
        )
    if settings.embedding_provider == "openai":
        openai_key = settings.openai_api_key
        if openai_key is None:
            raise ProviderUnavailableError("openai")
        return OpenAIEmbedding(
            model=settings.openai_embedding_model,
            dimensions=settings.openai_embedding_dimension,
            api_key=openai_key.get_secret_value(),
            embed_batch_size=settings.embedding_batch_size,
        )
    return DeterministicEmbedding(
        model_name="deterministic-hash-v1",
        dimension=settings.deterministic_embedding_dimension,
        embed_batch_size=settings.embedding_batch_size,
    )


class CompletionProvider(Protocol):
    async def complete(self, prompt: str) -> str:
        """Return a grounded answer for an already-delimited prompt."""


class OpenAICompletionProvider:
    """LlamaIndex OpenAI completion adapter with no tools attached."""

    def __init__(self, settings: Settings) -> None:
        if not settings.answer_ready or settings.openai_api_key is None:
            raise ProviderUnavailableError("openai answer")
        self._llm = OpenAI(
            model=settings.openai_chat_model,
            api_key=settings.openai_api_key.get_secret_value(),
            temperature=0.1,
            timeout=settings.answer_timeout_seconds,
        )

    async def complete(self, prompt: str) -> str:
        response = await self._llm.acomplete(prompt)
        return str(response).strip()


def create_completion_provider(settings: Settings) -> CompletionProvider | None:
    if settings.answer_provider == "extractive":
        return None
    return OpenAICompletionProvider(settings)


def index_fingerprint(settings: Settings) -> str:
    """Fingerprint every setting whose change requires a full repository reindex."""

    model = (
        settings.voyage_embedding_model
        if settings.embedding_provider == "voyage"
        else settings.openai_embedding_model
        if settings.embedding_provider == "openai"
        else "deterministic-hash-v1"
    )
    contract = "|".join(
        [
            "index-v3",
            settings.embedding_provider,
            model,
            str(settings.embedding_dimension),
            "tree-sitter-language-pack-0.13.0",
            "secret-redaction-structure-v3",
            "hybrid-rerank-v1",
            settings.qdrant_collection_prefix,
            str(settings.chunk_lines),
            str(settings.chunk_line_overlap),
            str(settings.chunk_max_chars),
        ]
    )
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()
