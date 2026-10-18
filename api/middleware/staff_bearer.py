"""AA-816 / AA-854 step C3 — staff routes take the caller's identity from a verified Cognito Bearer token.

Pure ASGI middleware on the staff paths (``STAFF_PATH_PREFIXES``: /admin, /v1/pipeline, /v1/rules,
/v1/social). Registered in ``api/main.py`` after the rate limiter, so it wraps it: the rate limiter's admin
tracking and every route below see the verified identity, never the client's.

- **Cognito Bearer** (a JWT whose ``iss`` is the WORKFORCE issuer, ``api/core/staff_identity.token_kind``): verified
  by the C2 verifier (WORKFORCE x {cis-admin, breakglass}) and resolved to the active ``shared.admin_users`` row by
  ``(iss, sub)`` (first sign-in binds by email, once). Then ``x-admin-role`` (highest ``cis-*`` group) and
  ``x-admin-user-id`` (``admin_users.id``) are REWRITTEN from the verified principal and whatever the client sent
  under those names is dropped; the ``Authorization`` header is dropped too, so the routes' own guards (which read a
  Bearer as a tenant HS256 JWT) see a plain staff request. The identity is put in the
  ``staff_identity.current_staff`` contextvar and ``scope["state"]["staff_identity"]``; one
  ``staff_bearer_verified`` line is logged. Refused: 401 (bad token, unknown / inactive user), 403 (no ``cis-*``
  group), 503 (verifier, JWKS or directory unavailable). The request never reaches the route.
- **Any other Bearer** (legacy HS256 staff or tenant JWT without ``iss``, a JWT from another issuer such as a
  future EXTERNAL ``cis-portal`` token or the CIS-minted Booking RS256 token, or not a JWT) and **no Bearer**:
  passed through untouched; the route's own guard decides (``/admin/brand-identity`` and ``/admin/atoms`` take
  tenant tokens). Header trust for those stays as it was (``staff_authz.py``) until cutover (C9).

The secret check is not touched: the inline ``verify_admin_secret`` calls and ``staff_policy`` still require
``X-Admin-Secret`` (ADR 0006 phase 1: the BFF keeps sending it until cutover).
"""
from __future__ import annotations

from typing import Any

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.core import staff_identity
from api.core.staff_authz import STAFF_PATH_PREFIXES

logger = structlog.get_logger()

_DROPPED = frozenset({b"authorization", b"x-admin-role", b"x-admin-user-id"})


def is_staff_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES)


def _bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name == b"authorization":
            text = value.decode("latin-1").strip()
            if text[:7].lower() == "bearer " and text[7:].strip():
                return text[7:].strip()
            return None
    return None


def _rewritten(headers: list[tuple[bytes, bytes]], identity: staff_identity.StaffIdentity) -> list:
    kept = [(n, v) for n, v in headers if n not in _DROPPED]
    kept.append((b"x-admin-role", identity.role.encode()))
    kept.append((b"x-admin-user-id", identity.admin_id.encode()))
    return kept


class StaffBearerMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_staff_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        token = _bearer(headers)
        if token is None or staff_identity.token_kind(token) != staff_identity.AUTH_COGNITO:
            await self.app(scope, receive, send)
            return

        method, path = scope.get("method", ""), scope.get("path", "")
        try:
            identity = await staff_identity.authenticate(_pool(scope), token)
        except staff_identity.StaffAuthError as exc:
            logger.warning("staff_bearer_refused", status=exc.status, reason=exc.reason, method=method, path=path)
            extra = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            await JSONResponse({"detail": exc.detail}, status_code=exc.status, headers=extra)(scope, receive, send)
            return

        logger.info("staff_bearer_verified", admin_id=identity.admin_id, role=identity.role,
                    client_id=identity.client_id, method=method, path=path)
        state = dict(scope.get("state") or {})
        state["staff_identity"] = identity
        inner = dict(scope, headers=_rewritten(headers, identity), state=state)
        reset = staff_identity.current_staff.set(identity)
        try:
            await self.app(inner, receive, send)
        finally:
            staff_identity.current_staff.reset(reset)


def _pool(scope: Scope) -> Any:
    app = scope.get("app")
    return getattr(getattr(app, "state", None), "pool", None)
