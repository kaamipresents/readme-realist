"""Log formatting and secret redaction.

Redaction is a security control, not a nicety: an exception repr that reaches a
log aggregator with a GitHub App private key in it is a credential leak.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from app.logging_config import JsonFormatter, SecretRedactingFilter, configure_logging

SECRET = "sk-ant-super-secret-value-12345"


def _record(
    msg: str, args: tuple[object, ...] | Mapping[str, object] | None = None
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_redacts_a_secret_from_the_message() -> None:
    record = _record(f"calling API with {SECRET}")
    SecretRedactingFilter([SECRET]).filter(record)

    assert SECRET not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_redacts_a_secret_from_positional_args() -> None:
    record = _record("token=%s status=%s", (SECRET, 200))
    SecretRedactingFilter([SECRET]).filter(record)

    assert SECRET not in record.getMessage()
    assert "200" in record.getMessage()


def test_redacts_a_secret_from_dict_args() -> None:
    # `logging` passes mapping-style args as a 1-tuple and unwraps them itself,
    # so construct the record the same way rather than handing it a bare dict.
    record = _record("token=%(token)s", ({"token": SECRET},))
    assert isinstance(record.args, dict)

    SecretRedactingFilter([SECRET]).filter(record)
    assert SECRET not in record.getMessage()


def test_redacts_a_secret_embedded_in_a_longer_string() -> None:
    """The realistic leak is a URL or an exception repr, not a bare value."""
    record = _record(f"401 from https://api.github.com?token={SECRET}&x=1")
    SecretRedactingFilter([SECRET]).filter(record)
    assert SECRET not in record.getMessage()


def test_redacts_a_multiline_private_key() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----\n"
    record = _record(f"failed to sign JWT with {pem}")
    SecretRedactingFilter([pem]).filter(record)
    assert "MIIEvQIBADAN" not in record.getMessage()


def test_short_values_are_not_redacted() -> None:
    """Redacting a 3-character value would mangle unrelated log lines."""
    record = _record("processing pull request 42")
    SecretRedactingFilter(["42"]).filter(record)
    assert "42" in record.getMessage()


def test_the_filter_is_a_no_op_without_secrets() -> None:
    record = _record("nothing to hide")
    assert SecretRedactingFilter([]).filter(record) is True
    assert record.getMessage() == "nothing to hide"


def test_json_formatter_emits_one_object_per_line() -> None:
    record = _record("review complete")
    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "review complete"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "timestamp" in payload


def test_json_formatter_merges_structured_extra_fields() -> None:
    record = _record("review complete")
    record.repo = "acme/widget"  # type: ignore[attr-defined]
    record.pull_number = 42  # type: ignore[attr-defined]

    payload = json.loads(JsonFormatter().format(record))
    assert payload["repo"] == "acme/widget"
    assert payload["pull_number"] == 42


def test_configure_logging_is_idempotent() -> None:
    configure_logging(level="INFO", json_output=True, secrets=[SECRET])
    first = len(logging.getLogger().handlers)

    configure_logging(level="INFO", json_output=True, secrets=[SECRET])
    assert len(logging.getLogger().handlers) == first == 1


def test_configured_root_logger_redacts_end_to_end(capsys) -> None:
    configure_logging(level="INFO", json_output=True, secrets=[SECRET])
    logging.getLogger("app.test").info("using key %s", SECRET)

    captured = capsys.readouterr().out
    assert SECRET not in captured
    assert "***REDACTED***" in captured
