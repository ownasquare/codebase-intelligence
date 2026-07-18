from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from codebase_intelligence.config import Settings
from codebase_intelligence.ingestion.source_loader import (
    ArchiveLimitError,
    GitHubRepository,
    GitHubRequestError,
    GitHubSourceLoader,
    InvalidSourceError,
    SafeArchiveExtractor,
    UnsafeArchiveError,
)

COMMIT_SHA = "a" * 40


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
    }
    values.update(overrides)
    return Settings(**values)


def make_zip(
    path: Path,
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for member, content in members:
            archive.writestr(member, content)
    return path


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/repo",
        "https://gitlab.com/acme/repo",
        "https://github.com/acme/repo/issues/1",
        "https://github.com/acme/repo?token=secret",
        "https://user:secret@github.com/acme/repo",
        "https://github.com:443/acme/repo",
        "https://github.com/acme%2Frepo/project",
        " https://github.com/acme/repo",
    ],
)
def test_github_repository_rejects_noncanonical_sources(url: str) -> None:
    with pytest.raises(InvalidSourceError):
        GitHubRepository.parse(url)


def test_github_repository_canonicalizes_git_suffix_and_trailing_slash() -> None:
    source = GitHubRepository.parse("https://github.com/Acme/Code.git/")

    assert source.owner == "Acme"
    assert source.repository == "Code"
    assert source.canonical_url == "https://github.com/Acme/Code"


@pytest.mark.asyncio
async def test_download_resolves_commit_streams_archive_and_strips_redirect_auth(
    tmp_path: Path,
) -> None:
    archive_path = make_zip(tmp_path / "source.zip", [("repo/src/app.py", b"print('ok')\n")])
    archive_bytes = archive_path.read_bytes()
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/repos/acme/repo/commits/main":
            return httpx.Response(200, json={"sha": COMMIT_SHA}, request=request)
        if request.url.path == f"/repos/acme/repo/zipball/{COMMIT_SHA}":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://codeload.github.com/acme/repo/legacy.zip/{COMMIT_SHA}"
                },
                request=request,
            )
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=archive_bytes, request=request)
        raise AssertionError(f"unexpected request path: {request.url.path}")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        loader = GitHubSourceLoader(settings(tmp_path), client)
        destination = tmp_path / "download.zip"
        result = await loader.download(
            "https://github.com/acme/repo",
            "main",
            destination,
            token="github-private-token-value",
        )
    finally:
        await client.aclose()

    assert result.path == destination
    assert result.commit_sha == COMMIT_SHA
    assert result.canonical_url == "https://github.com/acme/repo"
    assert destination.read_bytes() == archive_bytes
    assert seen[0][0].startswith("https://api.github.com/repos/acme/repo/")
    assert seen[0][1] == "Bearer github-private-token-value"
    assert seen[-1][0].startswith("https://codeload.github.com/acme/repo/")
    assert seen[-1][1] is None


@pytest.mark.asyncio
async def test_download_uses_default_branch_before_resolving_commit(tmp_path: Path) -> None:
    archive_path = make_zip(tmp_path / "source.zip", [("repo/main.py", b"pass\n")])
    archive_bytes = archive_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo":
            return httpx.Response(200, json={"default_branch": "trunk"}, request=request)
        if request.url.path == "/repos/acme/repo/commits/trunk":
            return httpx.Response(200, json={"sha": COMMIT_SHA}, request=request)
        if request.url.path == f"/repos/acme/repo/zipball/{COMMIT_SHA}":
            return httpx.Response(200, content=archive_bytes, request=request)
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        result = await GitHubSourceLoader(settings(tmp_path), client).download(
            "https://github.com/acme/repo",
            None,
            tmp_path / "download.zip",
        )

    assert result.commit_sha == COMMIT_SHA
    assert result.requested_ref is None


@pytest.mark.asyncio
async def test_download_rejects_unsafe_redirect_without_leaking_location(
    tmp_path: Path,
) -> None:
    leaked = "redirect-password-value"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": COMMIT_SHA}, request=request)
        return httpx.Response(
            302,
            headers={"location": f"https://user:{leaked}@example.test/archive.zip"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        with pytest.raises(GitHubRequestError) as caught:
            await GitHubSourceLoader(settings(tmp_path), client).download(
                "https://github.com/acme/repo",
                "main",
                tmp_path / "download.zip",
                token="private-token-value",
            )

    assert leaked not in str(caught.value)
    assert "private-token-value" not in str(caught.value)


@pytest.mark.asyncio
async def test_download_rejects_declared_oversize_before_writing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": COMMIT_SHA}, request=request)
        return httpx.Response(
            200,
            headers={"content-length": str(128 * 1024)},
            content=b"small",
            request=request,
        )

    destination = tmp_path / "download.zip"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        with pytest.raises(ArchiveLimitError):
            await GitHubSourceLoader(settings(tmp_path), client).download(
                "https://github.com/acme/repo",
                "main",
                destination,
            )
    assert not destination.exists()


@pytest.mark.parametrize(
    "member",
    [
        "../escape.py",
        "/absolute.py",
        "//server/share.py",
        "repo/../../escape.py",
        "C:/windows.py",
        "repo/C:/windows.py",
        "repo\\escape.py",
    ],
)
def test_extractor_rejects_unsafe_paths(member: str, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "unsafe.zip", [(member, b"pass\n")])

    with pytest.raises(UnsafeArchiveError):
        SafeArchiveExtractor(settings(tmp_path)).extract(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_extractor_rejects_duplicate_normalized_paths(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "duplicate.zip",
        [("repo/src/./app.py", b"one"), ("repo/src/app.py", b"two")],
    )

    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        SafeArchiveExtractor(settings(tmp_path)).extract(archive, tmp_path / "out")


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR])
def test_extractor_rejects_links_and_special_files(file_type: int, tmp_path: Path) -> None:
    member = zipfile.ZipInfo("repo/special")
    member.create_system = 3
    member.external_attr = (file_type | 0o600) << 16
    archive = make_zip(tmp_path / "special.zip", [(member, b"target")])

    with pytest.raises(UnsafeArchiveError, match="special"):
        SafeArchiveExtractor(settings(tmp_path)).extract(archive, tmp_path / "out")


def test_extractor_rejects_encrypted_metadata(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "encrypted.zip", [("repo/app.py", b"pass\n")])
    data = bytearray(archive.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    data[local + 6 : local + 8] = (1).to_bytes(2, "little")
    data[central + 8 : central + 10] = (1).to_bytes(2, "little")
    archive.write_bytes(data)

    with pytest.raises(UnsafeArchiveError, match="Encrypted"):
        SafeArchiveExtractor(settings(tmp_path)).extract(archive, tmp_path / "out")


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("repo/nested.zip", b"not-even-a-zip"),
        ("repo/payload.bin.txt", b"PK\x03\x04payload"),
        ("repo/payload.txt", (b"\x00" * 257) + b"ustar" + (b"\x00" * 250)),
    ],
)
def test_extractor_rejects_nested_archives(name: str, content: bytes, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "nested.zip", [(name, content)])

    with pytest.raises(UnsafeArchiveError, match="Nested"):
        SafeArchiveExtractor(settings(tmp_path)).extract(archive, tmp_path / "out")


def test_extractor_rejects_expansion_ratio(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "bomb.zip",
        [("repo/repeated.txt", b"A" * 10_000)],
        compression=zipfile.ZIP_DEFLATED,
    )

    with pytest.raises(ArchiveLimitError, match="expansion"):
        SafeArchiveExtractor(
            settings(tmp_path),
            max_expansion_ratio=2,
        ).extract(archive, tmp_path / "out")


def test_extractor_rejects_count_depth_and_path_limits(tmp_path: Path) -> None:
    count_archive = make_zip(
        tmp_path / "count.zip",
        [("repo/one.py", b"1"), ("repo/two.py", b"2")],
    )
    with pytest.raises(ArchiveLimitError, match="too many"):
        SafeArchiveExtractor(settings(tmp_path), max_files=1).extract(
            count_archive,
            tmp_path / "count-out",
        )

    depth_archive = make_zip(tmp_path / "depth.zip", [("a/b/c/file.py", b"pass")])
    with pytest.raises(ArchiveLimitError, match="path"):
        SafeArchiveExtractor(settings(tmp_path), max_path_depth=3).extract(
            depth_archive,
            tmp_path / "depth-out",
        )

    long_archive = make_zip(
        tmp_path / "long.zip",
        [(f"repo/{'a' * 125}.py", b"pass")],
    )
    with pytest.raises(ArchiveLimitError, match="path"):
        SafeArchiveExtractor(settings(tmp_path), max_path_length=128).extract(
            long_archive,
            tmp_path / "long-out",
        )


def test_extractor_writes_only_regular_private_files(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "safe.zip",
        [("repo/src/app.py", b"print('safe')\n"), ("repo/README.md", b"# Repo\n")],
    )

    destination = SafeArchiveExtractor(settings(tmp_path)).extract(
        archive,
        tmp_path / "out",
    )

    assert (destination / "repo/src/app.py").read_text() == "print('safe')\n"
    assert stat.S_IMODE((destination / "repo/src/app.py").stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
