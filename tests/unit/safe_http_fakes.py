"""AA-875 — stand-ins for shared.safe_http.request() in router/adapter unit tests.

Callers no longer open aiohttp sessions; tests patch `shared.safe_http.request` (the one outbound
seam) with `fake_request(...)`. safe_http's own behaviour (DNS/redirect/size checks) is tested in
test_aa875_safe_http.py.
"""
from typing import Optional
from unittest.mock import AsyncMock

from shared.safe_http import SafeResponse

TARGET = "shared.safe_http.request"


def response(status: int, content_type: str = "application/json", body_text: str = "") -> SafeResponse:
    return SafeResponse(status=status, headers={"content-type": content_type}, body=body_text.encode())


def fake_request(status: Optional[int] = None, *, raise_exc: Optional[BaseException] = None,
                 content_type: str = "application/json", body_text: str = "") -> AsyncMock:
    """AsyncMock for safe_http.request: returns one response, or raises `raise_exc`."""
    if raise_exc is not None:
        return AsyncMock(side_effect=raise_exc)
    return AsyncMock(return_value=response(status, content_type, body_text))
