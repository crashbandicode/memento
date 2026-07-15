"""Logging guards for request metadata that can contain credentials."""

from __future__ import annotations

import logging
import re


_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|code)=)[^&\s]*"
)


def redact_sensitive_query_values(path: str) -> str:
    """Redact credential-like query values while retaining request shape."""
    return _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", path)


class SensitiveQueryFilter(logging.Filter):
    """Redact query credentials from Uvicorn's positional access-log args."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            sanitized = list(args)
            sanitized[2] = redact_sensitive_query_values(sanitized[2])
            record.args = tuple(sanitized)
        return True


def install_sensitive_query_filter() -> None:
    """Install the access-log filter once, after Uvicorn configures logging."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SensitiveQueryFilter) for item in logger.filters):
        logger.addFilter(SensitiveQueryFilter())
