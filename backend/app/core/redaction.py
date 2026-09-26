import re


def redact_common_secrets(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        value,
    )
    redacted = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b",
        "[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret))\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        redacted,
    )
