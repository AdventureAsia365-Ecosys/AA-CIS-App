"""Content studio roles on tenant tokens (AA-979 P2b, root ADR 0010a; design §2.6 "Role enforcement").

One check for every route that accepts a tenant JWT (``/v1/*`` and the tenant branch of ``/admin/brand-identity``),
instead of one per dependency: a tenant JWT minted by ``POST /auth/tenant-session`` carries ``csr`` (the person's
Content studio role from ``shared.tenant_members``) and this middleware enforces it before the route runs:

- ``csr = viewer``: read only. Any method other than GET / HEAD / OPTIONS -> 403 ``studio_role_forbidden``.
- ``csr = editor``: everything except account writes: a non-read method on :data:`ACCOUNT_WRITE_PREFIXES`
  (channel connections, the API key, members, webhooks) -> 403 ``studio_role_forbidden``.
- ``csr = owner`` and every API-key token (no ``csr``; machines and the window's API-key sign-in): unchanged.

The token is verified with the tenant JWT rules (``api.routers.auth.verify_jwt`` + ``role == tenant``) only to READ the
claim; a request whose Authorization header carries no valid tenant JWT passes through untouched and gets its real
401/403 from the route's own dependency. A token with an unknown ``csr`` value is treated as viewer (fail closed).

Header parsing fails closed (AA-979 fix round 2): the routes read the header with FastAPI's ``HTTPBearer`` (scheme
compared case-insensitively, value stripped), so ``bearer <t>`` or ``Bearer  <t>`` reach the route as a normal token.
This middleware therefore checks every credential it can find, not only an exact ``Bearer <t>``: the HTTPBearer-style
value of each Authorization header and every whitespace-separated part of it. Any one that is a tenant JWT whose
``csr`` refuses the request -> 403. The two dependencies that accept a tenant JWT (``v1_tours.get_tenant`` and
``admin_pipeline._resolve_brand_tenant_id``) also call :func:`enforce` on the payload they accepted, so the check holds
even for a header shape this middleware did not foresee.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param

from api.routers.auth import studio_role_of, verify_jwt

logger = structlog.get_logger()

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Account writes an editor may not make (design Q5): channel connections and tests, the API key, members, webhooks.
ACCOUNT_WRITE_PREFIXES = ("/v1/integrations", "/v1/account", "/v1/webhooks")
# Paths that accept a tenant JWT (the rest ignore it, or refuse it in their own dependency).
TENANT_PATH_PREFIXES = ("/v1/", "/admin/brand-identity")
_MAX_PARTS = 8  # whitespace parts checked per header (bounds the work on a padded header)


def _matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def refusal(method: str, path: str, role: str | None) -> bool:
    """True when a token with Content studio role ``role`` may not make this request."""
    if role is None or role == "owner" or method in READ_METHODS:
        return False
    if role == "editor":
        return any(_matches(path, p) for p in ACCOUNT_WRITE_PREFIXES)
    return True  # viewer (and any unknown value, mapped to viewer by studio_role_of)


def _candidate_tokens(headers: Iterable[str]) -> list[str]:
    """Every credential the Authorization header(s) could carry: the value ``HTTPBearer`` would hand the route (any case
    of ``bearer``, value stripped) plus each whitespace-separated part, so no header shape slips past the check."""
    out: list[str] = []
    for header in headers:
        scheme, cred = get_authorization_scheme_param(header)
        if scheme.lower() == "bearer" and cred:
            out.append(cred)
        out.extend(header.split()[:_MAX_PARTS])  # the route only ever accepts the HTTPBearer value above
    return list(dict.fromkeys(out))  # de-duplicated, order kept


def _studio_roles(tokens: Iterable[str]) -> list[tuple[str, str]]:
    """(tenant id, csr role) for each token that is a valid tenant JWT carrying ``csr``."""
    roles = []
    for token in tokens:
        try:
            payload = verify_jwt(token)
        except Exception:  # noqa: BLE001 - not a valid HS256 token: the route's own auth answers
            continue
        if payload.get("role") != "tenant":
            continue
        role = studio_role_of(payload)
        if role is not None:
            roles.append((str(payload.get("sub")), role))
    return roles


def enforce(request: Request | None, payload: dict) -> None:
    """Dependency-side check (defence in depth): raise 403 ``studio_role_forbidden`` when the tenant JWT ``payload`` the
    route just accepted may not make this request. ``request`` is None on a direct call (tests): no check."""
    if request is None:
        return
    role = studio_role_of(payload)
    if refusal(request.method, request.url.path, role):
        logger.info("tenant_role_refused", tenant_id=str(payload.get("sub")), role=role, method=request.method,
                    path=request.url.path, at="dependency")
        raise HTTPException(status_code=403, detail="studio_role_forbidden")


async def tenant_role_middleware(request: Request, call_next):
    path = request.url.path
    if not any(path.startswith(p) for p in TENANT_PATH_PREFIXES):
        return await call_next(request)
    headers = request.headers.getlist("authorization")
    if not headers:
        return await call_next(request)
    for tenant_id, role in _studio_roles(_candidate_tokens(headers)):
        if refusal(request.method, path, role):
            logger.info("tenant_role_refused", tenant_id=tenant_id, role=role, method=request.method, path=path)
            return JSONResponse(
                status_code=403,
                content={"detail": "studio_role_forbidden",
                         "message": f"Your Content studio role ({role}) cannot make this change."},
            )
    return await call_next(request)
