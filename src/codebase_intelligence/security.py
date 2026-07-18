"""Small security primitives shared by API and ingestion boundaries.

Repository URLs, provider failures, and uploaded filenames are untrusted input.  This
module deliberately exposes only conservative helpers that never need to inspect the
process environment or execute repository-controlled content.
"""

from __future__ import annotations

import hmac
import re
from pathlib import PurePath
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeFilenameError(ValueError):
    """Raised when an uploaded filename cannot safely be used as a basename."""


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_AUTHORIZATION = re.compile(r"(?im)^(authorization\s*:\s*)(?:bearer|basic|token)\s+[^\r\n]+$")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s:]+(?::[^/@\s]*)?@")
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:access_token|api[_-]?key|auth|authorization|key|password|secret|"
    r"signature|token)=)[^&#\s]*"
)
_KNOWN_TOKEN = re.compile(
    r"(?x)\b(?:"
    r"github_pat_[A-Za-z0-9_]{20,255}|"
    r"gh[pousr]_[A-Za-z0-9]{20,255}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,255}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,50}|"
    r"xox[baprs]-[0-9A-Za-z-]{10,255}"
    r")\b"
)


def constant_time_equal(candidate: str | bytes | None, expected: str | bytes | None) -> bool:
    """Compare authentication material without data-dependent early exits."""

    if candidate is None or expected is None:
        return False
    candidate_bytes = candidate.encode("utf-8") if isinstance(candidate, str) else candidate
    expected_bytes = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(candidate_bytes, expected_bytes)


def validate_safe_filename(filename: str, *, max_bytes: int = 255) -> str:
    """Return a safe basename or reject path-like/control-character input."""

    if not filename or _CONTROL_CHARACTERS.search(filename):
        raise UnsafeFilenameError("The upload filename is invalid.")
    if "/" in filename or "\\" in filename or PurePath(filename).name != filename:
        raise UnsafeFilenameError("The upload filename must be a basename.")
    if filename in {".", ".."} or len(filename.encode("utf-8")) > max_bytes:
        raise UnsafeFilenameError("The upload filename is invalid.")
    return filename


def redact_url(url: str) -> str:
    """Remove userinfo, query values, and fragments from a URL used for diagnostics."""

    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "[REDACTED_URL]"
        port = ""
        if parts.port is not None:
            port = f":{parts.port}"
        netloc = f"{parts.hostname}{port}"
        clean = SplitResult(parts.scheme, netloc, parts.path, "", "")
        return urlunsplit(clean)
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def redact_sensitive_text(value: object, *, max_length: int = 1000) -> str:
    """Best-effort diagnostic redaction for text that must cross a trust boundary.

    External-response bodies are intentionally not passed to this helper by the
    ingestion layer.  It is intended for locally generated exception messages and
    still truncates output to keep logs bounded.
    """

    text = str(value)
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", text)
    text = _KNOWN_TOKEN.sub("[REDACTED_TOKEN]", text)
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def safe_error_message(error: BaseException, *, default: str = "The operation failed.") -> str:
    """Return a bounded credential-redacted exception message.

    Empty exception messages deliberately collapse to a stable generic response.
    Callers handling remote systems should prefer their own generic status message
    instead of including response bodies.
    """

    message = redact_sensitive_text(error)
    return message if message.strip() else default


# Backwards-friendly aliases for middleware integrations.
secure_compare = constant_time_equal
sanitize_error = safe_error_message


__all__ = [
    "UnsafeFilenameError",
    "constant_time_equal",
    "redact_sensitive_text",
    "redact_url",
    "safe_error_message",
    "sanitize_error",
    "secure_compare",
    "validate_safe_filename",
]
