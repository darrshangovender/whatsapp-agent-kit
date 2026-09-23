"""Meta webhook signing helpers, shared by the real and mock transports."""

from __future__ import annotations

import hashlib
import hmac

PREFIX = "sha256="


def sign_body(app_secret: str, body: bytes) -> str:
    """Return the value Meta would put in ``X-Hub-Signature-256`` for ``body``."""
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return PREFIX + digest


def verify_signature(app_secret: str, signature: str | None, body: bytes) -> bool:
    """Constant-time check of ``signature`` against the HMAC of the *raw* ``body`` bytes.

    Compared as bytes: ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII
    ``str`` input, which an attacker-controlled header must never turn into a 500.
    """
    if not signature or not signature.startswith(PREFIX):
        return False
    expected = sign_body(app_secret, body).encode("ascii")
    return hmac.compare_digest(expected, signature.encode("utf-8", "replace"))
