"""Webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.security.signatures import compute_signature, verify_signature

SECRET = "test-webhook-secret-that-is-long-enough"
BODY = b'{"action":"opened","number":42}'


def _signature(body: bytes = BODY, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_accepts_a_correctly_signed_payload() -> None:
    assert verify_signature(BODY, _signature(), SECRET) is True


def test_compute_signature_round_trips() -> None:
    assert verify_signature(BODY, compute_signature(BODY, SECRET), SECRET) is True


def test_rejects_a_payload_signed_with_a_different_secret() -> None:
    assert verify_signature(BODY, _signature(secret="wrong-secret"), SECRET) is False


def test_rejects_a_tampered_body() -> None:
    signature = _signature()
    assert verify_signature(b'{"action":"closed"}', signature, SECRET) is False


def test_rejects_a_single_flipped_byte() -> None:
    """A body that differs by one character must not validate."""
    assert verify_signature(BODY + b" ", _signature(), SECRET) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "deadbeef",  # no scheme
        "sha1=" + "0" * 40,  # wrong algorithm
        "sha256=",  # empty digest
        "sha256=tooshort",
        "sha256=" + "0" * 63,  # one hex char short
        "sha256=" + "z" * 64,  # right length, not hex
    ],
)
def test_rejects_malformed_headers(header: str | None) -> None:
    assert verify_signature(BODY, header, SECRET) is False


def test_rejects_when_no_secret_is_configured() -> None:
    """An empty secret must never be treated as 'anything goes'."""
    assert verify_signature(BODY, _signature(), "") is False


def test_digest_comparison_is_case_insensitive() -> None:
    """GitHub sends lowercase hex; tolerate uppercase without weakening the check."""
    upper = _signature().upper().replace("SHA256", "sha256")
    assert verify_signature(BODY, upper, SECRET) is True
