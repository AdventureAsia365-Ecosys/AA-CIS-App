"""AA-1157 (S288, root ADR 0019 d5 as amended) — a "view as a lower role" session never writes.

Pure ASGI middleware, registered in ``api/main.py`` before ``StaffAuditMiddleware`` (so it sits INSIDE it, and inside
``StaffBearerMiddleware``): when the staff bearer middleware put an open view-as session on the scope
(``scope["state"]["staff_view_as"]``, api/core/staff_view_as.py), every method other than GET / HEAD / OPTIONS is
answered 403 ``{"detail": "view_as_read_only"}`` before any route code runs, whatever the path (every route, also the
ones added later: the check knows no route). The staff audit middleware around it writes its usual ``staff.request``
row for the refusal (status 403, ``details.view_as`` = the session). ``staff_authz.denial_reason`` refuses the same
writes again (defence in depth for a route reached any other way).
"""
from __future__ import annotations

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.core.staff_view_as import READ_METHODS, READ_ONLY_DETAIL

logger = structlog.get_logger()

STATE_KEY = "staff_view_as"   # api/middleware/staff_bearer.STAFF_VIEW_AS


def view_of(scope: Scope):
    state = scope.get("state")
    return state.get(STATE_KEY) if isinstance(state, dict) else None


class StaffViewAsGateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        view = view_of(scope)
        method = (scope.get("method") or "").upper()
        if view is not None and method not in READ_METHODS:
            logger.warning("staff_view_as_write_refused", method=method, path=scope.get("path", ""),
                           session_id=getattr(view, "session_id", None), view_role=getattr(view, "view_role", None))
            await JSONResponse({"detail": READ_ONLY_DETAIL}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
