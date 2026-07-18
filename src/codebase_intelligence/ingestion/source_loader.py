"""Secure GitHub acquisition and ZIP extraction for untrusted repositories."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from codebase_intelligence.config import Settings

GITHUB_API_ORIGIN = "https://api.github.com"
_GITHUB_HOST = "github.com"
_GITHUB_ARCHIVE_HOST = "codeload.github.com"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}")
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$")
_NESTED_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".7z",
    ".rar",
    ".jar",
    ".war",
    ".ear",
    ".whl",
    ".egg",
)
_NESTED_ARCHIVE_MAGIC = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
)


class IngestionError(RuntimeError):
    """Base class for safe, user-presentable ingestion failures."""


class InvalidSourceError(IngestionError, ValueError):
    """Raised when a source is not an exact GitHub repository URL."""


class GitHubRequestError(IngestionError):
    """Raised for bounded GitHub API or archive request failures."""


class ArchiveLimitError(IngestionError):
    """Raised when an archive exceeds a configured resource limit."""


class UnsafeArchiveError(IngestionError):
    """Raised when ZIP metadata or content is unsafe to extract."""


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    """An exact, validated ``github.com/<owner>/<repository>`` identity."""

    owner: str
    repository: str

    @classmethod
    def parse(cls, url: str) -> GitHubRepository:
        """Parse only canonicalizable HTTPS GitHub repository URLs.

        Credentials, ports, percent-encoding, query strings, fragments, and paths
        below a repository are rejected instead of being silently discarded.
        """

        if not isinstance(url, str) or not url or url != url.strip():
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        if any(ord(character) < 32 or ord(character) == 127 for character in url):
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        if "\\" in url or "%" in url:
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.") from error
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.hostname.lower() != _GITHUB_HOST
            or parts.username is not None
            or parts.password is not None
            or port is not None
            or parts.query
            or parts.fragment
        ):
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        path = parts.path
        if path.startswith("//"):
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        segments = path.strip("/").split("/")
        if len(segments) != 2 or any(not segment for segment in segments):
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        owner, repository = segments
        if repository.lower().endswith(".git"):
            repository = repository[:-4]
        if (
            _OWNER_PATTERN.fullmatch(owner) is None
            or _REPOSITORY_PATTERN.fullmatch(repository) is None
            or repository in {".", ".."}
        ):
            raise InvalidSourceError("Enter an HTTPS github.com owner/repository URL.")
        return cls(owner=owner, repository=repository)

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def name(self) -> str:
        return self.repository

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"

    def api_url(self, suffix: str = "") -> str:
        owner = quote(self.owner, safe="")
        repository = quote(self.repository, safe="")
        base = f"{GITHUB_API_ORIGIN}/repos/{owner}/{repository}"
        return f"{base}/{suffix.lstrip('/')}" if suffix else base


@dataclass(frozen=True, slots=True)
class GitHubDownload:
    """Resolved immutable identity and local archive path for a GitHub download."""

    path: Path
    canonical_url: str
    owner: str
    repository: str
    requested_ref: str | None
    commit_sha: str

    @property
    def archive_path(self) -> Path:
        return self.path


class GitHubSourceLoader:
    """Resolve a GitHub ref to a commit and stream its ZIP within strict bounds."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @asynccontextmanager
    async def _client_scope(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        timeout = httpx.Timeout(self._settings.github_download_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield client

    @staticmethod
    def _headers(token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "codebase-intelligence/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token is not None:
            if (
                not token
                or len(token) > 1000
                or any(ord(character) < 32 or ord(character) == 127 for character in token)
            ):
                raise InvalidSourceError("The GitHub credential is invalid.")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _validated_ref(ref: str) -> str:
        if (
            not ref
            or len(ref) > 255
            or ref != ref.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in ref)
            or "\\" in ref
        ):
            raise InvalidSourceError("The GitHub ref is invalid.")
        return ref

    @staticmethod
    def _validate_request_url(
        url: str,
        repository: GitHubRepository,
        *,
        archive_request: bool,
    ) -> None:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise GitHubRequestError("GitHub returned an unsafe redirect.") from error
        hostname = parts.hostname.lower() if parts.hostname else ""
        allowed_hosts = {"api.github.com"}
        if archive_request:
            allowed_hosts.add(_GITHUB_ARCHIVE_HOST)
        if (
            parts.scheme.lower() != "https"
            or hostname not in allowed_hosts
            or parts.username is not None
            or parts.password is not None
            or port is not None
            or parts.query
            or parts.fragment
            or "\\" in parts.path
            or "%" in parts.path
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
        ):
            raise GitHubRequestError("GitHub returned an unsafe redirect.")
        path_segments = [segment for segment in parts.path.split("/") if segment]
        if hostname == "api.github.com":
            expected = ["repos", repository.owner, repository.repository]
        else:
            expected = [repository.owner, repository.repository]
        if len(path_segments) < len(expected) or any(
            actual.casefold() != wanted.casefold()
            for actual, wanted in zip(path_segments[: len(expected)], expected, strict=True)
        ):
            raise GitHubRequestError("GitHub returned an unsafe redirect.")

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str],
        repository: GitHubRepository,
        *,
        archive_request: bool,
    ) -> httpx.Response:
        current_url = url
        current_headers = dict(headers)
        for redirect_count in range(4):
            self._validate_request_url(
                current_url,
                repository,
                archive_request=archive_request,
            )
            request = client.build_request("GET", current_url, headers=current_headers)
            try:
                response = await client.send(request, stream=True, follow_redirects=False)
            except httpx.HTTPError as error:
                raise GitHubRequestError("The GitHub request could not be completed.") from error
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not location or redirect_count == 3:
                raise GitHubRequestError("GitHub returned an invalid redirect chain.")
            current_url = urljoin(current_url, location)
            # Credentials never cross a redirect, including same-origin redirects.
            current_headers.pop("Authorization", None)
            current_headers.pop("Cookie", None)
            current_headers.pop("Proxy-Authorization", None)
        raise GitHubRequestError("GitHub returned an invalid redirect chain.")

    @staticmethod
    async def _read_json(response: httpx.Response) -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            status_code = response.status_code
            await response.aclose()
            raise GitHubRequestError(f"GitHub returned HTTP {status_code}.")
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    raise GitHubRequestError("GitHub returned an oversized metadata response.")
        finally:
            await response.aclose()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise GitHubRequestError("GitHub returned invalid metadata.") from error
        if not isinstance(payload, dict):
            raise GitHubRequestError("GitHub returned invalid metadata.")
        return payload

    async def _resolve_with_client(
        self,
        client: httpx.AsyncClient,
        repository: GitHubRepository,
        ref: str | None,
        headers: Mapping[str, str],
    ) -> tuple[str, str]:
        resolved_ref = ref
        if resolved_ref is None:
            response = await self._request(
                client,
                repository.api_url(),
                headers,
                repository,
                archive_request=False,
            )
            metadata = await self._read_json(response)
            default_branch = metadata.get("default_branch")
            if not isinstance(default_branch, str):
                raise GitHubRequestError("GitHub did not return a default branch.")
            resolved_ref = self._validated_ref(default_branch)
        else:
            resolved_ref = self._validated_ref(resolved_ref)
        encoded_ref = quote(resolved_ref, safe="")
        response = await self._request(
            client,
            repository.api_url(f"commits/{encoded_ref}"),
            headers,
            repository,
            archive_request=False,
        )
        metadata = await self._read_json(response)
        commit_sha = metadata.get("sha")
        if not isinstance(commit_sha, str) or _COMMIT_PATTERN.fullmatch(commit_sha) is None:
            raise GitHubRequestError("GitHub did not return a valid commit identifier.")
        return resolved_ref, commit_sha.lower()

    async def resolve_commit(
        self,
        url: str,
        ref: str | None = None,
        token: str | None = None,
    ) -> str:
        """Resolve a validated repository/ref to an immutable 40-character SHA."""

        repository = GitHubRepository.parse(url)
        headers = self._headers(token)
        async with self._client_scope() as client:
            _, commit_sha = await self._resolve_with_client(
                client,
                repository,
                ref,
                headers,
            )
        return commit_sha

    async def download(
        self,
        url: str,
        ref: str | None,
        destination: Path,
        token: str | None = None,
    ) -> GitHubDownload:
        """Resolve and stream a repository archive without leaking credentials."""

        repository = GitHubRepository.parse(url)
        headers = self._headers(token)
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise GitHubRequestError("The archive destination already exists.")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        async with self._client_scope() as client:
            _resolved_ref, commit_sha = await self._resolve_with_client(
                client,
                repository,
                ref,
                headers,
            )
            archive_url = repository.api_url(f"zipball/{commit_sha}")
            response = await self._request(
                client,
                archive_url,
                headers,
                repository,
                archive_request=True,
            )
            if not 200 <= response.status_code < 300:
                status_code = response.status_code
                await response.aclose()
                raise GitHubRequestError(f"GitHub returned HTTP {status_code}.")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    await response.aclose()
                    raise GitHubRequestError(
                        "GitHub returned an invalid archive length."
                    ) from error
                if declared_length < 0 or declared_length > self._settings.max_archive_bytes:
                    await response.aclose()
                    raise ArchiveLimitError("The repository archive is too large.")
            written = 0
            created = False
            completed = False
            try:
                with destination.open("xb") as archive_file:
                    created = True
                    os.chmod(destination, 0o600)
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        written += len(chunk)
                        if written > self._settings.max_archive_bytes:
                            raise ArchiveLimitError("The repository archive is too large.")
                        archive_file.write(chunk)
                if written == 0:
                    raise GitHubRequestError("GitHub returned an empty archive.")
                completed = True
            except (OSError, httpx.HTTPError) as error:
                raise GitHubRequestError("The repository archive could not be stored.") from error
            finally:
                await response.aclose()
                if created and not completed:
                    destination.unlink(missing_ok=True)
        return GitHubDownload(
            path=destination,
            canonical_url=repository.canonical_url,
            owner=repository.owner,
            repository=repository.repository,
            requested_ref=ref,
            commit_sha=commit_sha,
        )


@dataclass(frozen=True, slots=True)
class _ArchiveMember:
    info: zipfile.ZipInfo
    relative_path: PurePosixPath
    is_directory: bool


class SafeArchiveExtractor:
    """Preflight and manually extract ZIP files into a new directory."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
        max_file_bytes: int | None = None,
        max_archive_bytes: int | None = None,
        max_expansion_ratio: int | None = None,
        max_path_length: int | None = None,
        max_path_depth: int | None = None,
    ) -> None:
        configured = settings or Settings()
        self._max_files = max_files or configured.max_files
        self._max_extracted_bytes = max_bytes or configured.max_extracted_bytes
        self._max_file_bytes = max_file_bytes or configured.max_file_bytes
        self._max_archive_bytes = max_archive_bytes or configured.max_archive_bytes
        self._max_expansion_ratio = max_expansion_ratio or configured.max_archive_expansion_ratio
        self._max_path_length = max_path_length or configured.max_path_length
        self._max_path_depth = max_path_depth or configured.max_path_depth

    def _normalize_member(self, info: zipfile.ZipInfo) -> PurePosixPath:
        raw_name = getattr(info, "orig_filename", info.filename)
        if (
            not raw_name
            or "\x00" in raw_name
            or "\\" in raw_name
            or raw_name.startswith(("/", "//"))
            or _WINDOWS_DRIVE.match(raw_name)
        ):
            raise UnsafeArchiveError("The archive contains an unsafe path.")
        raw_parts = raw_name.split("/")
        normalized_parts: list[str] = []
        for part in raw_parts:
            if not part or part == ".":
                continue
            if part == ".." or _WINDOWS_DRIVE.match(part) or _WINDOWS_RESERVED.fullmatch(part):
                raise UnsafeArchiveError("The archive contains an unsafe path.")
            normalized_parts.append(unicodedata.normalize("NFC", part))
        if not normalized_parts:
            raise UnsafeArchiveError("The archive contains an unsafe path.")
        normalized = PurePosixPath(*normalized_parts)
        normalized_text = normalized.as_posix()
        if (
            len(normalized_parts) > self._max_path_depth
            or len(normalized_text) > self._max_path_length
            or len(normalized_text.encode("utf-8")) > self._max_path_length
        ):
            raise ArchiveLimitError("The archive contains a path outside configured limits.")
        return normalized

    @staticmethod
    def _entry_kind(info: zipfile.ZipInfo) -> tuple[bool, int]:
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        is_directory = info.is_dir()
        if info.flag_bits & 0x1:
            raise UnsafeArchiveError("Encrypted archive members are not supported.")
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise UnsafeArchiveError("The archive contains a link or special file.")
        if file_type == stat.S_IFDIR and not is_directory:
            raise UnsafeArchiveError("The archive contains inconsistent file metadata.")
        if is_directory and info.file_size != 0:
            raise UnsafeArchiveError("The archive contains inconsistent file metadata.")
        return is_directory, file_type

    def _preflight(self, archive: zipfile.ZipFile) -> list[_ArchiveMember]:
        infos = archive.infolist()
        if len(infos) > self._max_files:
            raise ArchiveLimitError("The archive contains too many entries.")
        members: list[_ArchiveMember] = []
        normalized_keys: set[str] = set()
        file_keys: set[str] = set()
        all_paths: list[tuple[str, bool]] = []
        total_uncompressed = 0
        total_compressed = 0
        for info in infos:
            relative_path = self._normalize_member(info)
            is_directory, _ = self._entry_kind(info)
            normalized_key = relative_path.as_posix().casefold()
            if normalized_key in normalized_keys:
                raise UnsafeArchiveError("The archive contains duplicate normalized paths.")
            normalized_keys.add(normalized_key)
            if not is_directory:
                if relative_path.as_posix().casefold().endswith(_NESTED_ARCHIVE_SUFFIXES):
                    raise UnsafeArchiveError("Nested archives are not supported.")
                if info.file_size < 0 or info.compress_size < 0:
                    raise UnsafeArchiveError("The archive contains invalid size metadata.")
                if info.file_size > self._max_file_bytes:
                    raise ArchiveLimitError("An archive member exceeds the per-file limit.")
                if info.file_size and info.compress_size == 0:
                    raise ArchiveLimitError("An archive member exceeds the expansion limit.")
                if info.file_size / max(info.compress_size, 1) > self._max_expansion_ratio:
                    raise ArchiveLimitError("An archive member exceeds the expansion limit.")
                total_uncompressed += info.file_size
                total_compressed += info.compress_size
                if total_uncompressed > self._max_extracted_bytes:
                    raise ArchiveLimitError("The expanded archive is too large.")
                file_keys.add(normalized_key)
            all_paths.append((normalized_key, is_directory))
            members.append(_ArchiveMember(info, relative_path, is_directory))
        if (
            total_uncompressed
            and total_uncompressed / max(total_compressed, 1) > self._max_expansion_ratio
        ):
            raise ArchiveLimitError("The archive exceeds the total expansion limit.")
        for normalized_key, _ in all_paths:
            parts = normalized_key.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in file_keys:
                    raise UnsafeArchiveError("The archive contains conflicting paths.")
        return members

    def extract(self, archive: Path, destination: Path) -> Path:
        """Return a new extraction root after complete metadata/content validation."""

        archive = Path(archive)
        destination = Path(destination)
        try:
            archive_stat = archive.lstat()
        except OSError as error:
            raise UnsafeArchiveError("The archive is unavailable.") from error
        if (
            archive.is_symlink()
            or not stat.S_ISREG(archive_stat.st_mode)
            or archive_stat.st_size > self._max_archive_bytes
        ):
            raise ArchiveLimitError("The archive is outside configured limits.")
        if destination.exists() or destination.is_symlink():
            raise UnsafeArchiveError("The extraction destination must not already exist.")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".codebase-extract-", dir=destination.parent))
        os.chmod(staging, 0o700)
        try:
            try:
                with zipfile.ZipFile(archive) as zip_archive:
                    members = self._preflight(zip_archive)
                    extracted_total = 0
                    for member in members:
                        target = staging.joinpath(*member.relative_path.parts)
                        if member.is_directory:
                            target.mkdir(mode=0o700, parents=True, exist_ok=True)
                            os.chmod(target, 0o700)
                            continue
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        for parent in (target.parent, *target.parent.parents):
                            if parent == staging.parent:
                                break
                            if parent.is_symlink():
                                raise UnsafeArchiveError(
                                    "The archive contains an unsafe extraction path."
                                )
                        member_total = 0
                        prefix = bytearray()
                        with (
                            zip_archive.open(member.info, "r") as source,
                            target.open("xb") as output,
                        ):
                            os.chmod(target, 0o600)
                            while True:
                                chunk = source.read(64 * 1024)
                                if not chunk:
                                    break
                                if len(prefix) < 512:
                                    prefix.extend(chunk[: 512 - len(prefix)])
                                member_total += len(chunk)
                                extracted_total += len(chunk)
                                if (
                                    member_total > member.info.file_size
                                    or member_total > self._max_file_bytes
                                    or extracted_total > self._max_extracted_bytes
                                ):
                                    raise ArchiveLimitError(
                                        "The expanded archive is outside configured limits."
                                    )
                                output.write(chunk)
                        if member_total != member.info.file_size:
                            raise UnsafeArchiveError("The archive contains invalid size metadata.")
                        if any(prefix.startswith(magic) for magic in _NESTED_ARCHIVE_MAGIC) or (
                            len(prefix) >= 262 and prefix[257:262] == b"ustar"
                        ):
                            raise UnsafeArchiveError("Nested archives are not supported.")
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as error:
                if isinstance(error, IngestionError):
                    raise
                raise UnsafeArchiveError("The ZIP archive is invalid or unsupported.") from error
            if destination.exists() or destination.is_symlink():
                raise UnsafeArchiveError("The extraction destination changed during extraction.")
            staging.rename(destination)
            os.chmod(destination, 0o700)
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "GITHUB_API_ORIGIN",
    "ArchiveLimitError",
    "GitHubDownload",
    "GitHubRepository",
    "GitHubRequestError",
    "GitHubSourceLoader",
    "IngestionError",
    "InvalidSourceError",
    "SafeArchiveExtractor",
    "UnsafeArchiveError",
]
