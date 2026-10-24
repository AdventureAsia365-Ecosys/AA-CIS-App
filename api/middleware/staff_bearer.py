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
  ``staff_identity.current_staff`` contextvar and ``scope["state"]["staff_identity"]``; the verified token itself
  goes to ``scope["state"]["staff_access_token"]`` (AA-941: the crm.ops.v1 relays send the staff member's own token
  on; never logged); one ``staff_bearer_verified`` line is logged. Refused: 401 (bad token, unknown / inactive
  user), 403 (no ``cis-*`` group), 503 (verifier, JWKS or directory unavailable). The request never reaches the route.
- **Any other Bearer** (legacy HS256 staff or tenant JWT without ``iss``, a JWT from another issuer such as a
  future EXTERNAL ``cis-portal`` token or the CIS-minted Booking RS256 token, or not a JWT) and **no Bearer**:
  passed through untouched; the route's own guard decides (``/admin/brand-identity`` and ``/admin/atoms`` take
  tenant tokens). Header trust for main-secret requests stays as it was (``staff_authz.py``) until the API legacy
  flag goes off (S2); C9 caps only the Preview credential (below).

AA-816 C9 (``api/core/staff_credential.py``): every admin-secret check on a staff path accepts a request whose
Cognito Bearer was verified here, without ``X-Admin-Secret``.

- **Preview credential** (no Cognito Bearer, ``X-Admin-Secret`` is not the main secret but equals the Preview
  secret read from Secrets Manager): ``x-admin-role`` is replaced by ``viewer`` whatever the client sent, or added
  when absent (never the machine principal), ``staff_credential.current_credential`` and
  ``scope["state"]["staff_credential"]`` are set to ``"preview"``, and the admin-secret checks accept it. The
  ``x-admin-user-id`` header is kept (legacy header trust, audited as such). One ``staff_preview_credential``
  line is logged per request.
- Anything else (main secret, wrong secret, no secret): untouched; the route's checks answer as before.
"""
from __future__ import annotations

from typing import Any

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.core import staff_credential, staff_identity
from api.core.staff_authz import STAFF_PATH_PREFIXES, _is_valid_secret, main_secrets

logger = structlog.get_logger()

_DROPPED = frozenset({b"authorization", b"x-admin-role", b"x-admin-user-id"})
# AA-941: scope["state"] key holding the verified Cognito access token (see __call__).
STAFF_ACCESS_TOKEN = "staff_access_token"
_ROLE = b"x-admin-role"


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


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


async def _is_preview_credential(headers: list[tuple[bytes, bytes]]) -> bool:
    """C9: ``X-Admin-Secret`` is the Preview credential (and not the main secret). A request without the header,
    or with the main secret, never triggers a Secrets Manager read."""
    provided = _header(headers, b"x-admin-secret")
    if not provided or _is_valid_secret(provided):
        return False
    preview = await staff_credential.preview_secret_async()
    return staff_credential.is_preview_secret(provided, preview, main_secrets())


def _capped(headers: list[tuple[bytes, bytes]]) -> list:
    kept = [(n, v) for n, v in headers if n != _ROLE]
    kept.append((_ROLE, staff_credential.PREVIEW_ROLE.encode()))
    return kept


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
            if await _is_preview_credential(headers):
                await self._preview(scope, receive, send, headers)
                return
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
        # AA-941: the verified access token itself, for the routes that relay the staff member's own token to another
        # app (crm.ops.v1: api/services/platform_apps.relay_authorization). The Authorization header is dropped
        # below, so without this those relays never had a token (AA-936 jobs card). Never logged.
        state[STAFF_ACCESS_TOKEN] = token
        inner = dict(scope, headers=_rewritten(headers, identity), state=state)
        reset = staff_identity.current_staff.set(identity)
        try:
            await self.app(inner, receive, send)
        finally:
            staff_identity.current_staff.reset(reset)
            _copy_route(inner, scope)

    async def _preview(self, scope: Scope, receive: Receive, send: Send, headers: list) -> None:
        """C9: the Preview credential is always the viewer."""
        logger.info("staff_preview_credential", method=scope.get("method", ""), path=scope.get("path", ""),
                    requested_role=_header(headers, _ROLE), role=staff_credential.PREVIEW_ROLE)
        state = dict(scope.get("state") or {})
        state["staff_credential"] = staff_credential.CRED_PREVIEW
        inner = dict(scope, headers=_capped(headers), state=state)
        reset = staff_credential.current_credential.set(staff_credential.CRED_PREVIEW)
        try:
            await self.app(inner, receive, send)
        finally:
            staff_credential.current_credential.reset(reset)
            _copy_route(inner, scope)


def _copy_route(inner: Scope, outer: Scope) -> None:
    """AA-871 — the router writes the matched route into the scope it was given (``inner``, a copy); hand it back so
    the request-id middleware's ``request_completed`` line has the route template for staff calls too."""
    if "route" in inner and "route" not in outer:
        outer["route"] = inner["route"]


def _pool(scope: Scope) -> Any:
    app = scope.get("app")
    return getattr(getattr(app, "state", None), "pool", None)
