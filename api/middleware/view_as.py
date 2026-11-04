"""AA-1035 R2 slice 2 (root ADR 0019 d5) — the gate every request with a view-as token passes (api/core/view_as.py).

Pure ASGI middleware, registered in ``api/main.py`` around the tenant role middleware and the rate limiter, so a
view-as request is decided before either (and before any route) runs.

A request is a view-as request when one credential its Authorization header(s) could carry (the same candidates the
tenant role middleware reads: the HTTPBearer value and every whitespace-separated part) is a valid HS256 JWT with the
``vas`` claim. Then, in this order:

1. method + path not an allowed read (:func:`api.core.view_as.allowed_route`) -> 403 ``view_as_read_only``;
2. the claims are not three UUIDs of a tenant token -> 401 ``view_as_ended``;
3. the session is not open, or its staff member lost the account, the capability or the tenant (DB, per request)
   -> 401 ``view_as_ended``; the DB unreachable / the table missing -> 503;
   the staff member's current role may not make this read (:func:`api.core.view_as.role_may_read`, e.g. support on
   ``/v1/quota``) -> 403 ``view_as_not_for_role``;
4. the audit row is written (``acp_shared.audit_log``, action ``tenant.view_as_request``) and only then is the request
   served. If the row cannot be written the request is NOT served (503): no unlogged read (root ADR 0019 d5).

The refusals of 1-3 are logged too (best effort, bounded: the refusal is answered whatever the log write does). Any
other request passes through untouched; a token that does not verify is left to the route's own auth.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import asyncpg
import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.core import view_as

logger = structlog.get_logger()

WRITE_TIMEOUT_S = 3.0


def _authorization_headers(scope: Scope) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"authorization"]


def view_as_payload(scope: Scope) -> Optional[dict]:
    """The verified payload of the first view-as token among the request's credentials, else None."""
    from api.middleware.tenant_role import _candidate_tokens
    from api.routers.auth import verify_jwt

    headers = _authorization_headers(scope)
    if not headers:
        return None
    for token in _candidate_tokens(headers):
        try:
            payload = verify_jwt(token)
        except Exception:  # noqa: BLE001 - not a valid HS256 token (or expired): the route's own auth answers
            continue
        if view_as.is_view_as(payload):
            return payload
    return None


def _query_keys(scope: Scope) -> list[str]:
    query = scope.get("query_string") or b""
    return sorted({part.split(b"=", 1)[0].decode("latin-1") for part in query.split(b"&") if part})


def _pool(scope: Scope) -> Any:
    app = scope.get("app")
    return getattr(getattr(app, "state", None), "pool", None)


async def _write(pool: Any, row: dict) -> None:
    from services.acp_shared.audit_log import write_audit_log

    await asyncio.wait_for(write_audit_log(pool, http_method=None, **row), timeout=WRITE_TIMEOUT_S)


async def _write_best_effort(pool: Any, row: dict) -> None:
    if pool is None:
        logger.warning("view_as_audit_write_failed", reason="no pool", outcome=row["details"]["outcome"])
        return
    try:
        await _write(pool, row)
    except Exception as e:  # noqa: BLE001 - a refusal is answered whatever the log write does
        logger.warning("view_as_audit_write_failed", error=repr(e)[:300], outcome=row["details"]["outcome"])


def _answer(status: int, detail: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "message": message})


class ViewAsGateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        payload = view_as_payload(scope)
        if payload is None:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        claims = view_as.claims_of(payload)
        pool = _pool(scope)
        keys = _query_keys(scope)

        def row(outcome: str, status: Optional[int], route: Optional[str] = None) -> dict:
            return view_as.audit_row(claims, method=method, path=path, route=route, status=status, outcome=outcome,
                                     query_keys=keys)

        template = view_as.allowed_route(method, path)
        if template is None:
            logger.info("view_as_refused", reason="read_only", method=method, path=path[:200],
                        session_id=claims.session_id if claims else None)
            await _write_best_effort(pool, row("refused_read_only", 403))
            await _answer(403, view_as.READ_ONLY_DETAIL, view_as.READ_ONLY_MESSAGE)(scope, receive, send)
            return
        if claims is None:
            logger.info("view_as_refused", reason="bad_claims", path=path[:200])
            await _write_best_effort(pool, row("refused_bad_claims", 401, template))
            await _answer(401, view_as.ENDED_DETAIL, view_as.ENDED_MESSAGE)(scope, receive, send)
            return
        if pool is None:
            await _answer(503, "view_as_unavailable", "View as tenant is unavailable.")(scope, receive, send)
            return
        try:
            async with pool.acquire() as conn:
                is_open, reason = await view_as.session_open(conn, claims, template)
        except (asyncpg.UndefinedTableError, asyncpg.PostgresError, OSError, asyncio.TimeoutError) as exc:
            logger.error("view_as_check_failed", error=type(exc).__name__, session_id=claims.session_id)
            await _answer(503, "view_as_unavailable", "View as tenant is unavailable.")(scope, receive, send)
            return
        if not is_open and reason == view_as.NOT_FOR_ROLE_REASON:
            logger.info("view_as_refused", reason=reason, route=template, session_id=claims.session_id)
            await _write_best_effort(pool, row(f"refused_{reason}", 403, template))
            await _answer(403, view_as.NOT_FOR_ROLE_DETAIL, view_as.NOT_FOR_ROLE_MESSAGE)(scope, receive, send)
            return
        if not is_open:
            logger.info("view_as_refused", reason=reason, session_id=claims.session_id)
            await _write_best_effort(pool, row(f"refused_{reason}", 401, template))
            await _answer(401, view_as.ENDED_DETAIL, view_as.ENDED_MESSAGE)(scope, receive, send)
            return
        try:
            await _write(pool, row("served", None, template))   # written before serving: no status yet
        except Exception as e:  # noqa: BLE001 - no unlogged read: refuse when the row cannot be written
            logger.error("view_as_audit_write_failed", error=repr(e)[:300], outcome="served",
                         session_id=claims.session_id)
            await _answer(503, "view_as_unavailable", "View as tenant is unavailable.")(scope, receive, send)
            return
        logger.info("view_as_request", session_id=claims.session_id, tenant_id=claims.tenant_id, route=template)
        await self.app(scope, receive, send)
