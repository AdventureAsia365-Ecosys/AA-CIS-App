"""Content studio roles on tenant tokens (AA-979 P2b, root ADR 0010a; design §2.6 "Role enforcement").

One check for every route that accepts a tenant JWT (``/v1/*`` and the tenant branch of ``/admin/brand-identity``),
instead of one per dependency: a tenant JWT minted by ``POST /auth/tenant-session`` carries ``csr`` (the person's
Content studio role from ``shared.tenant_members``) and this middleware enforces it before the route runs:

- ``csr = viewer``: read only. Any method other than GET / HEAD / OPTIONS -> 403 ``studio_role_forbidden``.
- ``csr = editor``: everything except account writes: a non-read method on :data:`ACCOUNT_WRITE_PREFIXES`
  (channel connections, the API key, members, webhooks) -> 403 ``studio_role_forbidden``.
- ``csr = owner`` and every API-key token (no ``csr``; machines and the window's API-key sign-in): unchanged.

The token is verified with the tenant JWT rules (``api.routers.auth.verify_jwt`` + ``role == tenant``) only to READ the
claim; a request whose Bearer is not a valid tenant JWT passes through untouched and gets its real 401/403 from the
route's own dependency. A token with an unknown ``csr`` value is treated as viewer (fail closed).
"""

from __future__ import annotations

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from api.routers.auth import studio_role_of, verify_jwt

logger = structlog.get_logger()

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Account writes an editor may not make (design Q5): channel connections and tests, the API key, members, webhooks.
ACCOUNT_WRITE_PREFIXES = ("/v1/integrations", "/v1/account", "/v1/webhooks")
# Paths that accept a tenant JWT (the rest ignore it, or refuse it in their own dependency).
TENANT_PATH_PREFIXES = ("/v1/", "/admin/brand-identity")


def _matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def refusal(method: str, path: str, role: str | None) -> bool:
    """True when a token with Content studio role ``role`` may not make this request."""
    if role is None or role == "owner" or method in READ_METHODS:
        return False
    if role == "editor":
        return any(_matches(path, p) for p in ACCOUNT_WRITE_PREFIXES)
    return True  # viewer (and any unknown value, mapped to viewer by studio_role_of)


async def tenant_role_middleware(request: Request, call_next):
    path = request.url.path
    if not any(path.startswith(p) for p in TENANT_PATH_PREFIXES):
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)
    try:
        payload = verify_jwt(auth.split(" ", 1)[1])
    except Exception:  # noqa: BLE001 - not a valid HS256 token: the route's own auth answers
        return await call_next(request)
    if payload.get("role") != "tenant":
        return await call_next(request)
    role = studio_role_of(payload)
    if refusal(request.method, path, role):
        logger.info("tenant_role_refused", tenant_id=str(payload.get("sub")), role=role, method=request.method,
                    path=path)
        return JSONResponse(
            status_code=403,
            content={"detail": "studio_role_forbidden",
                     "message": f"Your Content studio role ({role}) cannot make this change."},
        )
    return await call_next(request)
