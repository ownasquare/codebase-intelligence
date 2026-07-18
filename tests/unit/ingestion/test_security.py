from __future__ import annotations

import pytest

from codebase_intelligence.security import (
    UnsafeFilenameError,
    constant_time_equal,
    redact_sensitive_text,
    redact_url,
    safe_error_message,
    validate_safe_filename,
)


def test_constant_time_equal_requires_two_matching_values() -> None:
    assert constant_time_equal("same", "same")
    assert not constant_time_equal("different", "same")
    assert not constant_time_equal(None, "same")


@pytest.mark.parametrize(
    "filename",
    ["../repo.zip", "/tmp/repo.zip", "folder/repo.zip", "folder\\repo.zip", "bad\x00.zip"],
)
def test_validate_safe_filename_rejects_path_input(filename: str) -> None:
    with pytest.raises(UnsafeFilenameError):
        validate_safe_filename(filename)


def test_validate_safe_filename_returns_plain_basename() -> None:
    assert validate_safe_filename("repository.zip") == "repository.zip"


def test_diagnostic_redaction_removes_credentials() -> None:
    token = "ghp_" + ("A" * 36)
    value = (
        f"Authorization: Bearer {token}\n"
        "https://user:password@example.test/path?access_token=query-secret"
    )

    redacted = redact_sensitive_text(value)

    assert token not in redacted
    assert "password" not in redacted
    assert "query-secret" not in redacted


def test_redact_url_removes_userinfo_query_and_fragment() -> None:
    clean = redact_url("https://user:password@example.test:8443/path?token=query-secret#fragment")

    assert clean == "https://example.test:8443/path"


def test_safe_error_message_is_bounded_and_redacted() -> None:
    token = "sk-proj-" + ("A" * 30)
    message = safe_error_message(RuntimeError(f"provider failed with {token}"))

    assert token not in message
    assert "[REDACTED_TOKEN]" in message
