from __future__ import annotations

from codebase_intelligence.ingestion.redaction import redact_secrets


def test_private_key_block_is_redacted_with_line_count_preserved() -> None:
    source = (
        "before\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "sensitive-private-key-material\n"
        "-----END PRIVATE KEY-----\n"
        "after\n"
    )

    result = redact_secrets(source)

    assert "sensitive-private-key-material" not in result.text
    assert "[REDACTED:PRIVATE_KEY]" in result.text
    assert result.text.count("\n") == source.count("\n")
    assert result.redaction_count == 1


def test_incomplete_private_key_fails_closed_by_redacting_the_remainder() -> None:
    source = "safe\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\nstill-secret\n"

    result = redact_secrets(source)

    assert "secret" not in result.text
    assert result.text.count("\n") == source.count("\n")
    assert result.changed


def test_assignments_headers_urls_and_known_tokens_are_redacted() -> None:
    github_token = "ghp_" + ("A" * 36)
    source = (
        'api_key = "example-super-secret-value"\n'
        "Authorization: Bearer header-secret-value\n"
        "endpoint = https://user:url-password@example.test/path\n"
        f"github = {github_token}\n"
    )

    result = redact_secrets(source)

    for secret in (
        "example-super-secret-value",
        "header-secret-value",
        "url-password",
        github_token,
    ):
        assert secret not in result.text
    assert result.redaction_count == 4
    assert result.text.count("\n") == source.count("\n")


def test_unlabelled_high_entropy_secret_is_redacted() -> None:
    candidate = "AbCDefghijklmnopqrstuvwxyz0123456789_+/="

    result = redact_secrets(f"value = {candidate}\n")

    assert candidate not in result.text
    assert "[REDACTED:HIGH_ENTROPY]" in result.text


def test_redaction_is_idempotent() -> None:
    first = redact_secrets('password = "secret-value"\n')
    second = redact_secrets(first.text)

    assert second.text == first.text
    assert second.redaction_count == 0
