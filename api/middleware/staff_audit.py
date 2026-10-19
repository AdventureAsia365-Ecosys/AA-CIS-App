"""AA-884 (S241 GĐ5) — one ``acp_shared.audit_log`` row per non-GET staff call.

Pure ASGI middleware, registered in ``api/main.py`` INSIDE ``StaffBearerMiddleware`` (so it sees the verified Cognito
identity on the scope StaffBearer passes down) and outside the rate limiter (a 429 is logged too).

Logged: a request whose method is not GET / HEAD / OPTIONS, on a staff path (``staff_authz.STAFF_PATH_PREFIXES``),
carrying the valid admin secret (``staff_authz._is_valid_secret``). Tenant JWT calls never carry the secret, so they
are never logged here (their own domain rows stay as they are). Refused calls (403 from the route policy, 4xx/5xx)
are logged with their status; a request StaffBearer refuses never reaches this middleware.

Row: ``actor`` ``admin:<admin_users.id>`` from the verified Cognito identity (details.auth ``cognito``), else
``admin:<x-admin-user-id>`` (details.auth ``legacy_unverified``: the header is forgeable until the AA-816 cutover),
else ``staff:unknown`` when only a role header came, else ``machine``; ``actor_type`` staff / machine;
``action`` ``staff.request`` / ``machine.request``; ``resource_type`` ``http_route``; ``resource_id`` the concrete
path (≤ 512 chars); ``route`` the FastAPI route template (None when nothing matched); ``http_method``; ``status``;
``details`` {auth, role, query_keys}. Never the body, never query values, never a header value besides the role.

The insert runs after the response has been sent and is bounded by ``WRITE_TIMEOUT_S``; any failure is logged as
``staff_audit_write_failed`` and never changes or fails the response. The hash-chain trigger (CIS 248) serializes
audit inserts, so this write is its own autocommit statement (the lock is held for that statement only).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.core.staff_authz import STAFF_PATH_PREFIXES, _is_valid_secret

logger = structlog.get_logger()

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_TIMEOUT_S = 3.0
MAX_RESOURCE_ID = 512
MAX_ACTOR = 64          # acp_shared.audit_log.actor varchar(64)
RESOURCE_TYPE = "http_route"
STAFF_ACTION = "staff.request"
MACHINE_ACTION = "machine.request"


def _is_staff_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES)


def _header(scope: Scope, name: bytes) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


def should_log(scope: Scope) -> bool:
    if scope.get("type") != "http" or scope.get("method", "GET").upper() in READ_METHODS:
        return False
    if not _is_staff_path(scope.get("path", "")):
        return False
    return _is_valid_secret(_header(scope, b"x-admin-secret"))


def _identity(scope: Scope) -> Any:
    state = scope.get("state")
    identity = state.get("staff_identity") if isinstance(state, dict) else None
    if identity is None:
        from api.core.staff_identity import current_staff
        identity = current_staff.get()
    return identity


def build_row(scope: Scope, status: int) -> dict:
    """The audit row for one finished request (pure; the middleware inserts it)."""
    role = _header(scope, b"x-admin-role")
    identity = _identity(scope)
    if identity is not None and getattr(identity, "auth", None) == "cognito":
        actor, actor_type, auth = f"admin:{identity.admin_id}", "staff", "cognito"
    elif _header(scope, b"x-admin-user-id"):
        actor, actor_type, auth = f"admin:{_header(scope, b'x-admin-user-id')}", "staff", "legacy_unverified"
    elif role is not None:
        actor, actor_type, auth = "staff:unknown", "staff", "legacy_unverified"
    else:
        actor, actor_type, auth = "machine", "machine", "secret"
    route = getattr(scope.get("route"), "path", None)
    query = scope.get("query_string") or b""
    keys = sorted({part.split(b"=", 1)[0].decode("latin-1") for part in query.split(b"&") if part})
    return {
        "actor": actor[:MAX_ACTOR], "actor_type": actor_type,
        "action": STAFF_ACTION if actor_type == "staff" else MACHINE_ACTION,
        "resource_type": RESOURCE_TYPE, "resource_id": scope.get("path", "")[:MAX_RESOURCE_ID],
        "http_method": scope.get("method", "").upper(), "route": route if isinstance(route, str) else None,
        "status": status, "details": {"auth": auth, "role": role, "query_keys": keys},
    }


def _pool(scope: Scope) -> Any:
    app = scope.get("app")
    return getattr(getattr(app, "state", None), "pool", None)


async def _write(scope: Scope, status: int) -> None:
    from services.acp_shared.audit_log import write_audit_log

    try:
        row = build_row(scope, status)
        pool = _pool(scope)
        if pool is None:
            logger.warning("staff_audit_write_failed", reason="no pool", path=scope.get("path"))
            return
        await asyncio.wait_for(write_audit_log(pool, tenant_id=None, **row), timeout=WRITE_TIMEOUT_S)
    except Exception as e:
        logger.warning("staff_audit_write_failed", error=repr(e)[:300], method=scope.get("method"),
                       path=scope.get("path"), status=status)


class StaffAuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            log = should_log(scope)
        except Exception as e:  # never let the audit decide whether a request runs
            logger.warning("staff_audit_write_failed", error=repr(e)[:300], step="should_log")
            log = False
        if not log:
            await self.app(scope, receive, send)
            return

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # An unhandled error: ServerErrorMiddleware (outside) answers 500. Log it as such, then re-raise.
            await _write(scope, status_holder.get("status", 500))
            raise
        await _write(scope, status_holder.get("status", 500))
