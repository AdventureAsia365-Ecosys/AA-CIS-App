"""S241 — constant-time comparison for shared secrets (X-Admin-Secret and friends).

`provided != expected` on two str returns as soon as the first byte differs, so the response
time leaks how many leading bytes of a guess are right. `hmac.compare_digest` takes the same time
for any two inputs of the same length. Both sides are encoded to UTF-8 bytes first: compare_digest
raises TypeError on a str that is not pure ASCII, and a header value is attacker-controlled.

A missing value (None or "") never matches, so callers keep their own "secret not configured"
branch (503 in verify_admin_secret) exactly as before.
"""
from __future__ import annotations

import hmac
from typing import Optional


def secrets_equal(provided: Optional[str], expected: Optional[str]) -> bool:
    """True only when both are non-empty strings with the same content (constant time)."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
