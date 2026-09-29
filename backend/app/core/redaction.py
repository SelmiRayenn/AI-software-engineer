from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "[TRUNCATED]"

_SENSITIVE_KEYS = {
    "access_token",
    "anthropic_api_key",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "azure_client_secret",
    "bearer_token",
    "client_secret",
    "cloudflare_api_token",
    "connection_string",
    "database_url",
    "db_url",
    "github_token",
    "google_api_key",
    "id_token",
    "openai_api_key",
    "password",
    "passwd",
    "private_key",
    "proxy_authorization",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "signing_key",
    "token",
    "webhook_secret",
}

_ASSIGNMENT_KEY = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|bearer[_-]?token|token|password|passwd|pwd|client[_-]?secret|"
    r"private[_-]?key|secret(?:[_-]?access[_-]?key)?|authorization|"
    r"proxy[_-]?authorization|database[_-]?url|db[_-]?url|connection[_-]?string)"
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_DATABASE_URL_RE = re.compile(
    r"(?i)\b(?P<scheme>(?:postgres(?:ql)?(?:\+[a-z0-9_]+)?|mysql(?:\+[a-z0-9_]+)?|"
    r"mariadb(?:\+[a-z0-9_]+)?|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://)"
    r"(?P<username>[^\s/:@]+):(?P<password>[^\s/@]+)@"
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?im)\b(?P<header>authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-(?:proj-|ant-api\d{2}-)?[A-Za-z0-9_-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{16,}|"
    r"xox[abprs]-[A-Za-z0-9-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")\b"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_ASSIGNMENT_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<value>\[[^\]\r\n]*\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)"
)
_SENSITIVE_QUERY_RE = re.compile(rf"(?i)(?P<prefix>[?&]{_ASSIGNMENT_KEY}=)[^&\s#]+")
_DOTENV_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:(?:export|EXPORT)\s+)?[A-Z][A-Z0-9_]{1,}\s*=\s*)"
    r"(?P<value>[^\r\n]*)$"
)


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(
        (
            "_api_key",
            "_access_token",
            "_auth_token",
            "_authorization",
            "_client_secret",
            "_database_url",
            "_db_url",
            "_password",
            "_private_key",
            "_secret",
            "_secret_access_key",
            "_token",
        )
    )


def redact_common_secrets(value: str) -> str:
    """Redact common credential shapes while retaining surrounding debug context."""

    redacted = _PRIVATE_KEY_RE.sub(REDACTION_MARKER, value)
    redacted = _DATABASE_URL_RE.sub(
        lambda match: f"{match.group('scheme')}{REDACTION_MARKER}@",
        redacted,
    )
    redacted = _BEARER_RE.sub(rf"\1{REDACTION_MARKER}", redacted)
    redacted = _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, redacted)
    redacted = _SENSITIVE_QUERY_RE.sub(rf"\g<prefix>{REDACTION_MARKER}", redacted)
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(rf"\g<prefix>{REDACTION_MARKER}", redacted)
    redacted = _DOTENV_RE.sub(rf"\g<prefix>{REDACTION_MARKER}", redacted)
    return _AUTHORIZATION_HEADER_RE.sub(
        lambda match: f"{match.group('header')}: {REDACTION_MARKER}", redacted
    )


def redact_and_truncate(value: str, *, max_chars: int) -> str:
    redacted = redact_common_secrets(value)
    if len(redacted) <= max_chars:
        return redacted
    omitted = len(redacted) - max_chars
    return f"{redacted[:max_chars]}\n{TRUNCATION_MARKER} {omitted} characters omitted"


def redact_structured_value(
    value: Any,
    *,
    max_string_chars: int = 16_000,
    max_depth: int = 10,
    max_items: int = 200,
    _depth: int = 0,
) -> Any:
    """Recursively redact and bound JSON-like values used in logs and exports."""

    if _depth >= max_depth:
        return f"{TRUNCATION_MARKER} maximum nesting depth reached"
    if isinstance(value, str):
        return redact_and_truncate(value, max_chars=max_string_chars)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        items = list(value.items())
        for key, nested in items[:max_items]:
            safe_key = redact_common_secrets(key) if isinstance(key, str) else key
            result[safe_key] = (
                REDACTION_MARKER
                if is_sensitive_key(str(key))
                else redact_structured_value(
                    nested,
                    max_string_chars=max_string_chars,
                    max_depth=max_depth,
                    max_items=max_items,
                    _depth=_depth + 1,
                )
            )
        if len(items) > max_items:
            result[TRUNCATION_MARKER] = f"{len(items) - max_items} fields omitted"
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
        result = [
            redact_structured_value(
                item,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                max_items=max_items,
                _depth=_depth + 1,
            )
            for item in values[:max_items]
        ]
        if len(values) > max_items:
            result.append(f"{TRUNCATION_MARKER} {len(values) - max_items} items omitted")
        return result
    return value
