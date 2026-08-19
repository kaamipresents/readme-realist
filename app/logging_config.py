"""Structured logging with secret redaction.

Nothing that reaches a log sink should ever contain a private key, a webhook
secret, or an API key — including by accident via an exception repr.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

_RESERVED_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class SecretRedactingFilter(logging.Filter):
    """Replace known secret values anywhere in a record's message or args."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Only redact substantial values; a 3-character secret would mangle
        # every log line it happened to appear inside.
        self._secrets = tuple(sorted((s for s in secrets if len(s) >= 8), key=len, reverse=True))

    def _scrub(self, value: str) -> str:
        for secret in self._secrets:
            if secret in value:
                value = value.replace(secret, "***REDACTED***")
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with structured `extra=` fields merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable format for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    secrets: Iterable[str] = (),
) -> None:
    """Install the root handler. Idempotent — safe to call from tests."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    handler.addFilter(SecretRedactingFilter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at DEBUG and would echo request bodies.
    for noisy in ("httpx", "httpcore", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
