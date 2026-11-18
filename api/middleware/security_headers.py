"""AA-1111 (S284, ZAP baseline triage) — security headers on every CIS API response.

The API answers JSON (and the docs pages) through API Gateway; ZAP's passive baseline found no ``nosniff`` and no HSTS
on ``/health``. Every response now carries:

- ``X-Content-Type-Options: nosniff`` — a browser never sniffs a JSON or text body into something it would run;
- ``Strict-Transport-Security`` — the same value as the four frontends (two years, sub-domains, no preload): the API
  host is HTTPS only (API Gateway has no plain HTTP listener).

A header the endpoint already set wins (an SVG served with its own sandbox CSP + nosniff keeps it). The ``Server``
header is not sent at all: the image starts uvicorn with ``--no-server-header`` (Dockerfile), so API Gateway has no
``x-amzn-remapped-server: uvicorn`` to pass on.

Pure ASGI (no ``BaseHTTPMiddleware``): no extra task, no body buffering. Registered as the outermost user middleware in
``api/main.py``; the unhandled 500 (answered by Starlette's ServerErrorMiddleware, outside every user middleware) gets
the same headers from :func:`api.middleware.request_id.unhandled_error_handler`.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
