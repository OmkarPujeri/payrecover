"""Razorpay webhook signature verification (HMAC SHA-256)."""
from __future__ import annotations

import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    """Constant-time HMAC SHA-256 verification of a raw webhook body.

    Returns False if either the signature or secret is missing. Callers decide
    how to treat a missing secret (in dev/simulation we skip verification).
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
