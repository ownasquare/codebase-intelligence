"""Gitignore-aware, non-executing repository file discovery."""

from __future__ import annotations

import os
import stat
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pathspec.gitignore import GitIgnoreSpec

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.language_registry import LanguageRegistry


class ScanError(RuntimeError):
    """Raised when a repository cannot be scanned safely."""


class ScanLimitError(ScanError):
    """Raised when indexable repository content exceeds configured limits."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A regular UTF-8 source file selected for local parsing."""

    path: Path
    relative_path: str
    language: str
    size: int

    @property
    def size_bytes(self) -> int:
        return self.size


@dataclass(slots=True)
class ScanResult:
    """Selected files plus deterministic exclusion statistics."""

    files: list[SourceFile] = field(default_factory=list)
    skipped_file_count: int = 0
    skipped_bytes: int = 0
    indexed_bytes: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return self.indexed_bytes


_EXCLUDED_DIRECTORIES = {
    ".bzr",
    ".cache",
    ".git",
    ".gradle",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "bower_components",
    "build",
    "coverage",
    "deriveddata",
    "dist",
    "htmlcov",
    "node_modules",
    "pods",
    "site-packages",
    "target",
    "third_party",
    "vendor",
    "venv",
}
_BINARY_EXTENSIONS = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".bz2",
    ".class",
    ".db",
    ".dll",
    ".dmg",
    ".doc",
    ".docx",
    ".dylib",
    ".eot",
    ".exe",
    ".flac",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mov",
    ".mp3",
    ".mp4",
    ".npy",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".pkl",
    ".pyc",
    ".rar",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tiff",
    ".ttf",
    ".wav",
    ".wasm",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xz",
    ".zip",
}
_LOCKFILES = {
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "composer.lock",
    "flake.lock",
    "gemfile.lock",
    "go.sum",
    "gradle.lockfile",
    "package-lock.json",
    "packages.lock.json",
    "pipfile.lock",
    "podfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
_SECRET_FILENAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
}
_SECRET_SUFFIXES = {
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
    ".p7b",
    ".p7c",
    ".tfstate",
    ".tfvars",
}


class RepositoryScanner:
    """Select bounded source files without importing or executing repository code."""

    def __init__(self, settings: Settings, registry: LanguageRegistry | None = None) -> None:
        self._settings = settings
        self._registry = registry or LanguageRegistry()

    @staticmethod
    def _is_secret_file(relative_path: PurePosixPath) -> bool:
        filename = relative_path.name.casefold()
        suffix = Path(filename).suffix.casefold()
        if filename.startswith(".env"):
            return True
        if filename in _SECRET_FILENAMES or suffix in _SECRET_SUFFIXES:
            return True
        if filename.startswith(("secret.", "secrets.")):
            return True
        parent_parts = {part.casefold() for part in relative_path.parts[:-1]}
        if ".aws" in parent_parts and filename == "credentials":
            return True
        if ".docker" in parent_parts and filename == "config.json":
            return True
        return ".kube" in parent_parts and filename == "config"

    @staticmethod
    def _is_excluded_file(relative_path: PurePosixPath) -> str | None:
        filename = relative_path.name.casefold()
        suffix = Path(filename).suffix.casefold()
        if RepositoryScanner._is_secret_file(relative_path):
            return "secret_file"
        if filename in _LOCKFILES:
            return "lockfile"
        if filename.endswith((".min.js", ".min.css", ".bundle.js", ".bundle.css")):
            return "minified"
        if filename.endswith((".map", ".generated.js", ".generated.ts")):
            return "generated"
        if suffix in _BINARY_EXTENSIONS:
            return "binary_extension"
        return None

    @staticmethod
    def _inspect_text(
        path: Path,
        expected_stat: os.stat_result,
        max_bytes: int,
    ) -> str | None:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ScanError("A repository file could not be read safely.") from error
        try:
            with os.fdopen(descriptor, "rb") as source:
                opened_stat = os.fstat(source.fileno())
                expected_identity = (
                    expected_stat.st_dev,
                    expected_stat.st_ino,
                    expected_stat.st_size,
                    expected_stat.st_mtime_ns,
                )
                opened_identity = (
                    opened_stat.st_dev,
                    opened_stat.st_ino,
                    opened_stat.st_size,
                    opened_stat.st_mtime_ns,
                )
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_identity != expected_identity
                    or opened_stat.st_size > max_bytes
                ):
                    raise ScanError("A repository file changed while it was inspected.")
                content = source.read(max_bytes + 1)
                after_stat = os.fstat(source.fileno())
                if (
                    len(content) > max_bytes
                    or (
                        after_stat.st_dev,
                        after_stat.st_ino,
                        after_stat.st_size,
                        after_stat.st_mtime_ns,
                    )
                    != opened_identity
                ):
                    raise ScanError("A repository file changed while it was inspected.")
        except BaseException:
            # ``os.fdopen`` owns the descriptor after construction; if construction
            # itself failed, closing an already-closed descriptor is harmlessly ignored.
            with suppress(OSError):
                os.close(descriptor)
            raise
        if b"\x00" in content:
            return None
        control_count = sum(
            byte < 32 and byte not in {9, 10, 12, 13} for byte in content[: 128 * 1024]
        )
        if content and control_count / min(len(content), 128 * 1024) > 0.05:
            return None
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _looks_minified(text: str, language: str) -> bool:
        if language not in {"javascript", "typescript", "tsx", "css", "html"}:
            return False
        sample = text[: 256 * 1024]
        lines = sample.splitlines() or [sample]
        nonempty = [line for line in lines if line.strip()]
        if not nonempty:
            return False
        longest = max(len(line) for line in nonempty)
        average = sum(len(line) for line in nonempty) / len(nonempty)
        return longest > 2000 or (longest > 1000 and average > 300)

    @staticmethod
    def _load_ignore_spec(directory: Path) -> GitIgnoreSpec | None:
        ignore_file = directory / ".gitignore"
        try:
            file_stat = ignore_file.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ScanError("A .gitignore file could not be inspected safely.") from error
        if not stat.S_ISREG(file_stat.st_mode) or ignore_file.is_symlink():
            return None
        if file_stat.st_size > 1024 * 1024:
            raise ScanLimitError("A .gitignore file exceeds the safety limit.")
        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
            return GitIgnoreSpec.from_lines(lines)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ScanError("A .gitignore file could not be processed safely.") from error

    @staticmethod
    def _is_ignored(
        relative_path: PurePosixPath,
        *,
        is_directory: bool,
        rules: list[tuple[PurePosixPath, GitIgnoreSpec]],
    ) -> bool:
        ignored = False
        for base, spec in rules:
            try:
                local_path = relative_path.relative_to(base)
            except ValueError:
                continue
            candidate = local_path.as_posix()
            if is_directory:
                candidate = f"{candidate}/"
            result = spec.check_file(candidate)
            if result.include is not None:
                ignored = bool(result.include)
        return ignored

    def scan(self, root: Path) -> ScanResult:
        """Return sorted regular UTF-8 files and skip statistics for ``root``."""

        root = Path(root)
        try:
            root_lstat = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise ScanError("The repository root is unavailable.") from error
        if root.is_symlink() or not stat.S_ISDIR(root_lstat.st_mode):
            raise ScanError("The repository root must be a real directory.")

        selected: list[SourceFile] = []
        skipped = Counter[str]()
        skipped_bytes = 0
        indexed_bytes = 0

        def record_skip(reason: str, size: int = 0) -> None:
            nonlocal skipped_bytes
            skipped[reason] += 1
            skipped_bytes += max(size, 0)

        def walk(
            directory: Path,
            relative_directory: PurePosixPath,
            inherited_rules: list[tuple[PurePosixPath, GitIgnoreSpec]],
        ) -> None:
            nonlocal indexed_bytes
            rules = list(inherited_rules)
            local_spec = self._load_ignore_spec(directory)
            if local_spec is not None:
                rules.append((relative_directory, local_spec))
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
            except OSError as error:
                raise ScanError("A repository directory could not be read safely.") from error
            for entry in entries:
                relative_path = relative_directory / entry.name
                relative_text = relative_path.as_posix()
                depth = len(relative_path.parts)
                if (
                    depth > self._settings.max_path_depth
                    or len(relative_text) > self._settings.max_path_length
                    or len(relative_text.encode("utf-8")) > self._settings.max_path_length
                ):
                    record_skip("unsafe_path")
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ScanError("A repository entry could not be inspected safely.") from error
                if entry.is_symlink():
                    record_skip("symlink", entry_stat.st_size)
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if entry.name.casefold() in _EXCLUDED_DIRECTORIES:
                        continue
                    if self._is_ignored(relative_path, is_directory=True, rules=rules):
                        continue
                    walk(Path(entry.path), relative_path, rules)
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    record_skip("special_file", entry_stat.st_size)
                    continue
                if self._is_ignored(relative_path, is_directory=False, rules=rules):
                    record_skip("gitignore", entry_stat.st_size)
                    continue
                exclusion = self._is_excluded_file(relative_path)
                if exclusion is not None:
                    record_skip(exclusion, entry_stat.st_size)
                    continue
                if entry_stat.st_size > self._settings.max_file_bytes:
                    record_skip("too_large", entry_stat.st_size)
                    continue
                absolute_path = Path(entry.path)
                text = self._inspect_text(
                    absolute_path,
                    entry_stat,
                    self._settings.max_file_bytes,
                )
                if text is None:
                    record_skip("binary_content", entry_stat.st_size)
                    continue
                language = self._registry.language_for_path(relative_path)
                if self._looks_minified(text, language):
                    record_skip("minified", entry_stat.st_size)
                    continue
                if len(selected) >= self._settings.max_files:
                    raise ScanLimitError("The repository contains too many indexable files.")
                if indexed_bytes + entry_stat.st_size > self._settings.max_indexable_bytes:
                    raise ScanLimitError("The repository contains too much indexable content.")
                try:
                    resolved_path = absolute_path.resolve(strict=True)
                    resolved_path.relative_to(resolved_root)
                except (OSError, ValueError) as error:
                    raise ScanError("A repository file escaped the repository root.") from error
                selected.append(
                    SourceFile(
                        path=resolved_path,
                        relative_path=relative_text,
                        language=language,
                        size=entry_stat.st_size,
                    )
                )
                indexed_bytes += entry_stat.st_size

        walk(resolved_root, PurePosixPath(), [])
        selected.sort(key=lambda source: source.relative_path.casefold())
        return ScanResult(
            files=selected,
            skipped_file_count=sum(skipped.values()),
            skipped_bytes=skipped_bytes,
            indexed_bytes=indexed_bytes,
            skip_reasons=dict(sorted(skipped.items())),
        )


# Semantic alias used by callers that think in filtering rather than scanning.
FileFilter = RepositoryScanner


__all__ = [
    "FileFilter",
    "RepositoryScanner",
    "ScanError",
    "ScanLimitError",
    "ScanResult",
    "SourceFile",
]
