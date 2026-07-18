"""Tree-sitter symbol chunking with deterministic, line-accurate fallback."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.file_filter import SourceFile
from codebase_intelligence.ingestion.language_registry import LanguageRegistry, LanguageSpec
from codebase_intelligence.ingestion.redaction import RedactionResult, redact_secrets
from codebase_intelligence.models import CodeChunk


class ChunkingError(RuntimeError):
    """Raised when a selected source file changes or cannot be chunked safely."""


class ChunkLimitError(ChunkingError):
    """Raised when one file would exceed the configured chunk bound."""


@dataclass(frozen=True, slots=True)
class ChunkFileResult:
    """Chunks and redaction evidence produced by one TOCTOU-checked file read."""

    chunks: list[CodeChunk]
    redaction_count: int


@dataclass(frozen=True, slots=True)
class _SymbolCandidate:
    node: Node
    symbol: str
    kind: str
    start_line: int
    end_line: int


class CodeChunker:
    """Create deterministic ``CodeChunk`` records without executing source code."""

    def __init__(self, settings: Settings, registry: LanguageRegistry | None = None) -> None:
        self._settings = settings
        self._registry = registry or LanguageRegistry()

    @staticmethod
    def _end_line(node: Node) -> int:
        end_row, end_column = node.end_point
        return max(node.start_point.row + 1, end_row + (1 if end_column > 0 else 0))

    @staticmethod
    def _symbol_name(node: Node, content: bytes) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            name_node = node.child_by_field_name("declarator")
        if name_node is None:
            name_node = node.child_by_field_name("type")
        if name_node is None:
            return None
        identifier_types = {
            "constant",
            "field_identifier",
            "identifier",
            "name",
            "operator_name",
            "property_identifier",
            "type_identifier",
        }
        if name_node.type not in identifier_types:
            stack = [name_node]
            identifier = None
            while stack:
                candidate = stack.pop()
                if candidate.type in identifier_types:
                    identifier = candidate
                    break
                stack.extend(reversed(candidate.named_children))
            if identifier is None:
                return None
            name_node = identifier
        raw_name = content[name_node.start_byte : name_node.end_byte]
        try:
            name = raw_name.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        compact = " ".join(name.split())
        if not compact or len(compact) > 200 or "[REDACTED:" in compact:
            return None
        return compact

    def _symbol_candidates(
        self,
        root: Node,
        content: bytes,
        spec: LanguageSpec,
    ) -> list[_SymbolCandidate]:
        candidates: list[_SymbolCandidate] = []
        seen_ranges: set[tuple[int, int, str]] = set()
        stack = [root]
        visited = 0
        visit_limit = max(50_000, self._settings.max_chunks * 20)
        while stack:
            node = stack.pop()
            visited += 1
            if visited > visit_limit:
                return []
            kind = spec.kind_for(node.type)
            if kind is not None:
                if kind == "function":
                    ancestor = node.parent
                    while ancestor is not None:
                        ancestor_kind = spec.kind_for(ancestor.type)
                        if ancestor_kind in {"class", "implementation", "object", "trait"}:
                            kind = "method"
                            break
                        if ancestor_kind in {"function", "method"}:
                            break
                        ancestor = ancestor.parent
                symbol = self._symbol_name(node, content)
                range_key = (node.start_byte, node.end_byte, kind)
                if symbol is not None and range_key not in seen_ranges:
                    seen_ranges.add(range_key)
                    candidates.append(
                        _SymbolCandidate(
                            node=node,
                            symbol=symbol,
                            kind=kind,
                            start_line=node.start_point.row + 1,
                            end_line=self._end_line(node),
                        )
                    )
            stack.extend(reversed(node.named_children))
        candidates.sort(
            key=lambda candidate: (
                candidate.start_line,
                candidate.end_line,
                candidate.kind,
                candidate.symbol,
            )
        )
        return candidates

    def _segments(self, text: str, start_line: int) -> list[tuple[str, int, int]]:
        if not text or not text.strip():
            return []
        line_parts = text.splitlines(keepends=True)
        if not line_parts:
            line_parts = [text]
        if (
            len(line_parts) <= self._settings.chunk_lines
            and len(text) <= self._settings.chunk_max_chars
        ):
            return [(text, start_line, start_line + len(line_parts) - 1)]

        segments: list[tuple[str, int, int]] = []
        index = 0
        while index < len(line_parts):
            if len(line_parts[index]) > self._settings.chunk_max_chars:
                long_line = line_parts[index]
                for offset in range(0, len(long_line), self._settings.chunk_max_chars):
                    piece = long_line[offset : offset + self._settings.chunk_max_chars]
                    if piece:
                        line_number = start_line + index
                        segments.append((piece, line_number, line_number))
                index += 1
                continue
            end = min(index + self._settings.chunk_lines, len(line_parts))
            while (
                end > index + 1
                and len("".join(line_parts[index:end])) > self._settings.chunk_max_chars
            ):
                end -= 1
            piece = "".join(line_parts[index:end])
            if piece:
                segments.append((piece, start_line + index, start_line + end - 1))
            if end >= len(line_parts):
                break
            overlap = min(self._settings.chunk_line_overlap, max(end - index - 1, 0))
            index = max(index + 1, end - overlap)
        return segments

    @staticmethod
    def _chunk_id(
        repository_id: str,
        commit_sha: str | None,
        path: str,
        start_line: int,
        end_line: int,
        symbol: str | None,
        symbol_kind: str | None,
        content_hash: str,
    ) -> str:
        identity = "\x00".join(
            (
                repository_id,
                commit_sha or "",
                path,
                str(start_line),
                str(end_line),
                symbol or "",
                symbol_kind or "",
                content_hash,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _make_chunk(
        self,
        *,
        repository_id: str,
        commit_sha: str | None,
        path: str,
        language: str,
        symbol: str | None,
        symbol_kind: str | None,
        start_line: int,
        end_line: int,
        parser: str,
        text: str,
    ) -> CodeChunk:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return CodeChunk(
            id=self._chunk_id(
                repository_id,
                commit_sha,
                path,
                start_line,
                end_line,
                symbol,
                symbol_kind,
                content_hash,
            ),
            repository_id=repository_id,
            commit_sha=commit_sha,
            path=path,
            language=language,
            symbol=symbol,
            symbol_kind=symbol_kind,
            start_line=start_line,
            end_line=end_line,
            parser=parser,
            text=text,
            content_hash=content_hash,
        )

    def _fallback_chunks(
        self,
        *,
        path: str,
        text: str,
        language: str,
        repository_id: str,
        commit_sha: str | None,
    ) -> list[CodeChunk]:
        return [
            self._make_chunk(
                repository_id=repository_id,
                commit_sha=commit_sha,
                path=path,
                language=language,
                symbol=None,
                symbol_kind=None,
                start_line=start_line,
                end_line=end_line,
                parser="fallback",
                text=segment,
            )
            for segment, start_line, end_line in self._segments(text, 1)
        ]

    def _chunk_redacted_text(
        self,
        *,
        path: str,
        redaction: RedactionResult,
        spec: LanguageSpec,
        repository_id: str,
        commit_sha: str | None,
    ) -> list[CodeChunk]:
        text = redaction.text
        parser = self._registry.parser_for(spec)
        if parser is None:
            chunks = self._fallback_chunks(
                path=path,
                text=text,
                language=spec.name,
                repository_id=repository_id,
                commit_sha=commit_sha,
            )
        else:
            content = text.encode("utf-8")
            try:
                tree = parser.parse(content)
                candidates = self._symbol_candidates(tree.root_node, content, spec)
            except (OSError, ValueError):
                candidates = []
            if not candidates:
                chunks = self._fallback_chunks(
                    path=path,
                    text=text,
                    language=spec.name,
                    repository_id=repository_id,
                    commit_sha=commit_sha,
                )
            else:
                chunks = []
                for candidate in candidates:
                    symbol_text = content[
                        candidate.node.start_byte : candidate.node.end_byte
                    ].decode("utf-8")
                    for segment, start_line, end_line in self._segments(
                        symbol_text,
                        candidate.start_line,
                    ):
                        chunks.append(
                            self._make_chunk(
                                repository_id=repository_id,
                                commit_sha=commit_sha,
                                path=path,
                                language=spec.name,
                                symbol=candidate.symbol,
                                symbol_kind=candidate.kind,
                                start_line=start_line,
                                end_line=min(end_line, candidate.end_line),
                                parser="tree_sitter",
                                text=segment,
                            )
                        )
        if len(chunks) > self._settings.max_chunks:
            raise ChunkLimitError("The source file produces too many chunks.")
        return chunks

    def chunk_file(
        self,
        source_file: SourceFile,
        repository_id: str,
        commit_sha: str | None,
    ) -> list[CodeChunk]:
        """Read, redact, and chunk a scanner-selected immutable regular file."""

        return self.chunk_file_result(source_file, repository_id, commit_sha).chunks

    def chunk_file_result(
        self,
        source_file: SourceFile,
        repository_id: str,
        commit_sha: str | None,
    ) -> ChunkFileResult:
        """Read, redact, and chunk a scanner-selected immutable regular file."""

        try:
            before = source_file.path.lstat()
            resolved_path = source_file.path.resolve(strict=True)
        except OSError as error:
            raise ChunkingError("The source file is unavailable.") from error
        if (
            source_file.path.is_symlink()
            or resolved_path != source_file.path
            or not stat.S_ISREG(before.st_mode)
            or before.st_size != source_file.size
            or before.st_size > self._settings.max_file_bytes
        ):
            raise ChunkingError("The source file changed after scanning.")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source_file.path, flags)
        except OSError as error:
            raise ChunkingError("The source file could not be opened safely.") from error
        try:
            stream = os.fdopen(descriptor, "rb")
        except OSError as error:
            os.close(descriptor)
            raise ChunkingError("The source file could not be opened safely.") from error
        try:
            with stream:
                opened = os.fstat(stream.fileno())
                before_identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                )
                opened_identity = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                )
                if not stat.S_ISREG(opened.st_mode) or opened_identity != before_identity:
                    raise ChunkingError("The source file changed while it was opened.")
                content = stream.read(self._settings.max_file_bytes + 1)
                after = os.fstat(stream.fileno())
        except OSError as error:
            raise ChunkingError("The source file could not be decoded safely.") from error
        if len(content) > self._settings.max_file_bytes or opened_identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ChunkingError("The source file changed while it was read.")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ChunkingError("The source file could not be decoded safely.") from error
        spec = self._registry.get(source_file.language)
        if spec.name == "text":
            spec = self._registry.detect(source_file.relative_path)
        redaction = redact_secrets(text)
        return ChunkFileResult(
            chunks=self._chunk_redacted_text(
                path=source_file.relative_path,
                redaction=redaction,
                spec=spec,
                repository_id=repository_id,
                commit_sha=commit_sha,
            ),
            redaction_count=redaction.redaction_count,
        )

    def chunk(
        self,
        path: str | Path,
        text: str,
        repository_id: str = "repository",
        commit_sha: str | None = None,
        language: str | None = None,
    ) -> list[CodeChunk]:
        """Convenience API for already-decoded text, primarily tests and tools."""

        path_text = Path(path).as_posix()
        spec = self._registry.get(language) if language else self._registry.detect(path_text)
        return self._chunk_redacted_text(
            path=path_text,
            redaction=redact_secrets(text),
            spec=spec,
            repository_id=repository_id,
            commit_sha=commit_sha,
        )


__all__ = ["ChunkFileResult", "ChunkLimitError", "ChunkingError", "CodeChunker"]
