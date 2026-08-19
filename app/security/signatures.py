"""HMAC-SHA256 verification of GitHub webhook payloads.

GitHub signs the *raw* request body with the webhook secret and sends the digest
as `X-Hub-Signature-256: sha256=<hex>`. The body must be verified byte-for-byte
before it is parsed — re-serialising the JSON first would change the bytes and
break the MAC.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
_SCHEME = "sha256"
_DIGEST_LENGTH = 64  # hex characters of a SHA-256 digest


def compute_signature(payload: bytes, secret: str) -> str:
    """Produce the header value GitHub would send for this body."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{_SCHEME}={digest}"


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time check that `payload` was signed with `secret`.

    Returns False (never raises) for every failure mode: absent header, wrong
    scheme, malformed digest, or mismatch.
    """
    if not signature_header or not secret:
        return False

    scheme, separator, digest = signature_header.partition("=")
    if not separator or scheme.strip().lower() != _SCHEME:
        return False

    digest = digest.strip()
    if len(digest) != _DIGEST_LENGTH:
        return False

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    # compare_digest keeps the comparison time independent of where the first
    # differing byte falls, so an attacker cannot brute-force the digest.
    return hmac.compare_digest(expected, digest.lower())
