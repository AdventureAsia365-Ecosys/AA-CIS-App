"""AA-816 C9 — the admin-secret checks become principal-aware (root ADR 0006 / 0006a A9, cutover plan E1).

Until C9 every staff call needed ``X-Admin-Secret`` (the one shared secret the staff BFF holds), even when the
staff bearer middleware had already verified a Cognito token for the request. C9 changes what a staff path
accepts:

1. **A verified Cognito principal, no secret.** ``api/middleware/staff_bearer.py`` verified a WORKFORCE Bearer
   and put the identity in ``staff_identity.current_staff`` (and rewrote ``x-admin-role`` / ``x-admin-user-id``
   from it). Every admin-secret check on a staff path accepts that request without the secret. The BFF stops
   sending the secret for a Cognito session on those paths.
2. **The Preview credential, capped at viewer.** A second, Preview-only secret, read from Secrets Manager by the
   code constant :data:`PREVIEW_SECRET_NAME` (no env var). When a staff-path request carries it as
   ``X-Admin-Secret`` (and no Cognito Bearer), the middleware replaces ``x-admin-role`` with
   :data:`PREVIEW_ROLE` whatever the client sent (a higher role is lowered; no role header gives the viewer, never
   the machine principal, so ``MACHINE_ROUTES`` refuse it) and marks the request in :data:`current_credential`.
   The Vercel Preview BFF holds this value instead of the main secret, so a leaked Preview value reads content
   and nothing else.
3. **The main secret, unchanged.** ``ADMIN_SECRET`` keeps its meaning everywhere: the Production BFF for legacy
   HS256 sessions while the legacy flag is on, the CI deploy gate / smoke and the ops-probe Lambda (machine).

The Preview secret is optional: missing, unreadable, empty or equal to the main secret means the feature is off
(requests with that value get the usual 403 "Invalid admin secret"), never an error. It is read only on the
deployed API (``AWS_EXECUTION_ENV``, the AA-577 signal), cached for :data:`_PREVIEW_TTL_S` (a failure for
:data:`_PREVIEW_FAILURE_TTL_S`, so a stream of wrong secrets never becomes a stream of Secrets Manager calls),
and fetched in a worker thread so the event loop never waits on it. Both secrets are compared in constant time
(``api/core/secret_compare.secrets_equal``).

Non-staff paths (``/v1/tours``, ``/acp/*``, ``/auth/*``, the AA-577 ``/docs``) are unchanged: no middleware runs
there, so only the main secret opens them.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from contextvars import ContextVar
from typing import Callable, Optional

import structlog

from api.core.secret_compare import secrets_equal

logger = structlog.get_logger()

# The Secrets Manager name of the Preview BFF credential (root secrets register, AA-816 C9). A code constant on
# purpose: no env var, so a task-definition change can not point the API at another secret.
PREVIEW_SECRET_NAME = "aa-cis/dev/admin-secret-preview"
# The role a Preview-credential request gets, whatever x-admin-role it sent (staff_authz.READ_ONLY_ROLES).
PREVIEW_ROLE = "viewer"

CRED_PREVIEW = "preview"

_PREVIEW_TTL_S = 300.0          # re-read every 5 min: a rotation lands without a deploy
_PREVIEW_FAILURE_TTL_S = 60.0   # missing / unreadable: ask again after a minute
_SM_CONNECT_TIMEOUT_S = 2
_SM_READ_TIMEOUT_S = 3
_SM_MAX_ATTEMPTS = 2

# "preview" while the staff bearer middleware handles a request that carried the Preview credential (None
# otherwise). Set and reset by api/middleware/staff_bearer.py only.
current_credential: ContextVar[Optional[str]] = ContextVar("aa816_staff_credential", default=None)


def principal_verified() -> bool:
    """True when the staff middleware already established who calls, without the main secret: a verified
    Cognito principal (``staff_identity.current_staff``) or the Preview credential (:data:`current_credential`).
    Only ever true on a staff path, inside the request the middleware handled."""
    from api.core.staff_identity import current_staff

    return current_staff.get() is not None or current_credential.get() == CRED_PREVIEW


# ── The Preview secret ───────────────────────────────────────────────────────────────────────────


def _fetch_preview_secret() -> Optional[str]:
    """The Preview credential from Secrets Manager, or None when it does not exist. Blocking (boto3)."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"
    client = boto3.client("secretsmanager", region_name=region, config=Config(
        connect_timeout=_SM_CONNECT_TIMEOUT_S, read_timeout=_SM_READ_TIMEOUT_S,
        retries={"max_attempts": _SM_MAX_ATTEMPTS, "mode": "standard"},
    ))
    try:
        value = client.get_secret_value(SecretId=PREVIEW_SECRET_NAME).get("SecretString")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return value.strip() if isinstance(value, str) else None


class _PreviewSecretCache:
    """Thread-safe TTL cache of the Preview secret (successes and failures both expire)."""

    def __init__(self, fetch: Callable[[], Optional[str]]) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._value: Optional[str] = None
        self._expires_at = 0.0
        self._logged_state: Optional[str] = None

    def reset(self, fetch: Optional[Callable[[], Optional[str]]] = None) -> None:
        with self._lock:
            if fetch is not None:
                self._fetch = fetch
            self._value, self._expires_at, self._logged_state = None, 0.0, None

    def fresh(self) -> bool:
        return time.monotonic() < self._expires_at

    def cached(self) -> Optional[str]:
        return self._value

    def get(self) -> Optional[str]:
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at:
                return self._value
            try:
                value = self._fetch() or None
                state = "loaded" if value else "missing"
                ttl = _PREVIEW_TTL_S if value else _PREVIEW_FAILURE_TTL_S
            except Exception as exc:  # never an error for the caller: the feature is just off
                value, state, ttl = None, f"unavailable:{type(exc).__name__}", _PREVIEW_FAILURE_TTL_S
            self._value, self._expires_at = value, now + ttl
            if state != self._logged_state:     # one line per state change, never the value
                logger.info("staff_preview_credential_state", state=state, secret=PREVIEW_SECRET_NAME)
                self._logged_state = state
            return value


_cache = _PreviewSecretCache(_fetch_preview_secret)


def _preview_enabled_here() -> bool:
    """Only the deployed API reads the secret (AA-577 signal); local runs and CI never call AWS."""
    return bool(os.environ.get("AWS_EXECUTION_ENV"))


def preview_secret() -> Optional[str]:
    """The Preview credential, or None (feature off). Blocking on a cache miss: async callers use
    :func:`preview_secret_async`."""
    if not _preview_enabled_here():
        return None
    return _cache.get()


async def preview_secret_async() -> Optional[str]:
    if not _preview_enabled_here():
        return None
    if _cache.fresh():
        return _cache.cached()
    return await asyncio.to_thread(_cache.get)


def reset_preview_cache(fetch: Optional[Callable[[], Optional[str]]] = None) -> None:
    """Tests: drop the cached value and optionally replace the fetcher."""
    _cache.reset(fetch)


def is_preview_secret(provided: Optional[str], preview: Optional[str], main_secrets: list[str]) -> bool:
    """True when ``provided`` is the Preview credential: it matches ``preview`` (constant time) and the
    Preview value is not also a main secret (equal values = misconfiguration: the main meaning wins and the
    request is never capped, so the Production BFF and machine callers keep working)."""
    if not provided or not preview:
        return False
    if any(secrets_equal(preview, s) for s in main_secrets if s):
        return False
    return secrets_equal(provided, preview)
