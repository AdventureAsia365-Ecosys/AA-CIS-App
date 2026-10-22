"""AA-871 — request id for every HTTP request to the CIS API.

Each request gets one id:

- the caller's ``X-Request-ID`` when it is well formed (8–64 characters of ``[A-Za-z0-9_-]``, e.g. a UUID);
- otherwise a new ``uuid4().hex``. A malformed header is replaced, never echoed or logged.

The id is bound to :mod:`structlog.contextvars` for the request (every log line written while serving it carries
``request_id``), set as the Sentry tag ``request_id``, stored in ``request.state.request_id`` and returned as
``X-Request-ID`` on the response. The contextvars are cleared when a request STARTS, not when it ends: uvicorn runs
each request in its own task (a fresh copy of the server's context), so nothing leaks to the next request, and
uvicorn's "Exception in ASGI application" traceback — logged in that task after the app raised — keeps the id.

One ``request_completed`` line per request records method, path, route template, status, duration and, for
``/v1/*`` tenant calls, ``tenant_id`` (``request.state.tenant_id``, set by the rate limiter) and ``client`` (AA-760:
``portal`` session or metered ``api`` call, ``request.state.tenant_client``) — never the query
string, headers or body (DM-6.4). It replaces uvicorn's access log (shared/log_config.py). ``status`` is 500 when an
exception went through (Starlette's ServerErrorMiddleware answers it outside this middleware); when no response
was started and nothing was raised through it (client gone, task cancelled) ``status`` is null and
``aborted`` is true.

Pure ASGI (not ``BaseHTTPMiddleware``): no extra task and no body buffering, so the contextvars reach the endpoint.
Registered last in ``api/main.py`` = outermost user middleware, so a 401/403/429 answered by an inner middleware
carries the header too; the unhandled 500 (answered by Starlette's ServerErrorMiddleware, outside every user
middleware) gets it from :func:`unhandled_error_handler`.
"""
from __future__ import annotations

import re
import time
import uuid

import sentry_sdk
import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"
_HEADER_RAW = HEADER.lower().encode("latin-1")
_VALID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

log = structlog.get_logger("api.request")


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str | None:
    """The caller's id when it is safe to log and echo, else None."""
    if value and _VALID.match(value):
        return value
    return None


def _incoming(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _HEADER_RAW:
            return accept_request_id(value.decode("latin-1").strip())
    return None


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming(scope) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        sentry_sdk.set_tag("request_id", request_id)
        status: int | None = None
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            if status is None:
                status = 500  # answered by ServerErrorMiddleware, outside this middleware
            raise
        finally:
            # No clear_contextvars() here: see the module docstring (the 500 traceback line keeps request_id).
            route = scope.get("route")
            fields = {
                "method": scope.get("method"),
                "path": scope.get("path"),
                "route": getattr(route, "path", None),
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            state = scope.get("state") or {}
            if (tenant_id := state.get("tenant_id")) is not None:
                fields["tenant_id"] = tenant_id
            # AA-760 — "portal" (a portal session, not metered) or "api" (metered in shared.tenant_api_usage).
            if (client := state.get("tenant_client")) is not None:
                fields["client"] = client
            if status is None:
                fields["aborted"] = True
            log.info("request_completed", **fields)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """The unhandled-500 answer, with the request id. Same body as Starlette's default; Starlette re-raises the
    exception afterwards, so uvicorn and Sentry still see it."""
    headers = {HEADER: rid} if (rid := request_id_of(request)) else None
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)
