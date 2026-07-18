"""Line-preserving, fail-closed secret redaction for provider-bound source text."""

from __future__ import annotations

import re
from dataclasses import dataclass


class SecretRedactionError(RuntimeError):
    """Raised when source text cannot be proven safe to release downstream."""


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    redaction_count: int
    categories: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.redaction_count > 0


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_INCOMPLETE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_KNOWN_TOKEN = re.compile(
    r"(?x)\b(?:"
    r"github_pat_[A-Za-z0-9_]{20,255}|"
    r"gh[pousr]_[A-Za-z0-9]{20,255}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,255}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,50}|"
    r"xox[baprs]-[0-9A-Za-z-]{10,255}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")\b"
)
_AUTHORIZATION = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic|token)\s+)(?P<secret>[^\s,;]+)"
)
_CREDENTIAL_URL = re.compile(r"(?i)(?P<prefix>https?://[^/@\s:]+:)(?P<secret>[^@/\s]+)(?=@)")
_QUOTED_ASSIGNMENT = re.compile(
    r"(?im)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|secret|token)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<secret>[^\"'\r\n]+)(?P=quote)"
)
_UNQUOTED_ASSIGNMENT = re.compile(
    r"(?im)(?P<prefix>\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|secret|token)\s*[:=]\s*)"
    r"(?P<secret>[^\s,;#\"']+)"
)
_HIGH_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9_])")


def _line_preserving_marker(value: str, category: str) -> str:
    return f"[REDACTED:{category.upper()}]" + ("\n" * value.count("\n"))


def _looks_secret_like(candidate: str) -> bool:
    if candidate.startswith("[REDACTED:"):
        return False
    categories = sum(
        (
            any(character.islower() for character in candidate),
            any(character.isupper() for character in candidate),
            any(character.isdigit() for character in candidate),
            any(character in "+/=_-" for character in candidate),
        )
    )
    return categories >= 3 and len(set(candidate)) >= 12


def redact_secrets(text: str) -> RedactionResult:
    """Redact likely credentials while preserving the exact source line count.

    The function either returns fully processed text or raises
    :class:`SecretRedactionError`; callers must not fall back to the original input.
    """

    if not isinstance(text, str):
        raise SecretRedactionError("Secret redaction requires decoded text.")
    original_line_count = text.count("\n")
    redacted = text
    categories: list[str] = []
    count = 0

    def replace_whole(category: str):  # type: ignore[no-untyped-def]
        def callback(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            categories.append(category)
            return _line_preserving_marker(match.group(0), category)

        return callback

    def replace_group(category: str):  # type: ignore[no-untyped-def]
        def callback(match: re.Match[str]) -> str:
            nonlocal count
            if match.group("secret").startswith("[REDACTED:"):
                return match.group(0)
            count += 1
            categories.append(category)
            prefix = match.group("prefix")
            quote = match.groupdict().get("quote", "")
            return f"{prefix}{quote}[REDACTED:{category.upper()}]{quote}"

        return callback

    try:
        redacted = _PRIVATE_KEY.sub(replace_whole("private_key"), redacted)
        # A truncated PEM block is unsafe even without its closing marker.
        redacted = _INCOMPLETE_PRIVATE_KEY.sub(replace_whole("private_key"), redacted)
        redacted = _AUTHORIZATION.sub(replace_group("authorization"), redacted)
        redacted = _CREDENTIAL_URL.sub(replace_group("url_credential"), redacted)
        redacted = _QUOTED_ASSIGNMENT.sub(replace_group("assignment"), redacted)
        redacted = _UNQUOTED_ASSIGNMENT.sub(replace_group("assignment"), redacted)
        redacted = _KNOWN_TOKEN.sub(replace_whole("known_token"), redacted)

        def replace_entropy(match: re.Match[str]) -> str:
            nonlocal count
            candidate = match.group(0)
            if not _looks_secret_like(candidate):
                return candidate
            count += 1
            categories.append("high_entropy")
            return "[REDACTED:HIGH_ENTROPY]"

        redacted = _HIGH_ENTROPY_CANDIDATE.sub(replace_entropy, redacted)
    except (IndexError, KeyError, re.error) as error:
        raise SecretRedactionError("Secret redaction failed; source text was withheld.") from error
    if redacted.count("\n") != original_line_count:
        raise SecretRedactionError("Secret redaction failed; source text was withheld.")
    return RedactionResult(
        text=redacted,
        redaction_count=count,
        categories=tuple(categories),
    )


__all__ = ["RedactionResult", "SecretRedactionError", "redact_secrets"]
