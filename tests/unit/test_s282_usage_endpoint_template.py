"""S282 Sentry triage (AA-CIS-API-M) — usage rows record the route template, never a path too long for the column.

``shared.tenant_api_usage.endpoint`` is varchar(100). An admin route with two UUIDs
(``/admin/tenants/<uuid>/tripplanner-site/domains/<uuid>``) is 113 characters, so the INSERT failed and the usage row
was lost, logged at error level. The rate limiter now records the matched route template, and the billing writer cuts
endpoint and method to the column widths as a safety net (a 404 on a long unknown path still falls back to the raw
path) and logs a failed write as a warning with its traceback.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import rate_limit
from shared.services import billing_service

SECRET = "s282-test-admin-secret"
TENANT = "11111111-2222-4333-8444-555555555555"
DOMAIN = "66666666-7777-4888-9999-aaaaaaaaaaaa"
TEMPLATE = "/admin/tenants/{tenant_id}/sample-site/custom-domains/{domain_id}/retry-verification"


def _app(track: AsyncMock) -> FastAPI:
    app = FastAPI()

    @app.post(TEMPLATE)
    async def _retry(tenant_id: str, domain_id: str) -> dict:
        return {"ok": True}

    app.middleware("http")(rate_limit.rate_limit_middleware)
    app.state.pool = MagicMock()
    return app


@pytest.fixture
def track(monkeypatch):
    t = AsyncMock()
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(rate_limit, "track_api_call", t)
    return t


def test_admin_call_on_a_long_path_records_the_route_template(track):
    path = f"/admin/tenants/{TENANT}/sample-site/custom-domains/{DOMAIN}/retry-verification"
    assert len(path) > 100
    with TestClient(_app(track)) as client:
        assert client.post(path, headers={"X-Admin-Secret": SECRET}).status_code == 200
    track.assert_awaited_once()
    kw = track.await_args.kwargs
    assert kw["endpoint"] == TEMPLATE
    assert len(kw["endpoint"]) <= 100
    assert kw["actor_type"] == "admin" and kw["method"] == "POST" and kw["status_code"] == 200


def test_admin_call_with_no_matching_route_keeps_the_raw_path(track):
    """No route matched (404): the template is unknown, the raw path is passed and the writer cuts it."""
    path = "/admin/" + "x" * 150
    with TestClient(_app(track)) as client:
        assert client.get(path, headers={"X-Admin-Secret": SECRET}).status_code == 404
    assert track.await_args.kwargs["endpoint"] == path


def test_endpoint_helper_ignores_a_non_string_route_path():
    """The older unit tests build MagicMock requests: a MagicMock route path must not become the endpoint."""
    request = MagicMock()
    request.url.path = "/v1/tours/pool"
    assert rate_limit._endpoint(request) == "/v1/tours/pool"
    route = MagicMock()
    route.path = "/v1/tours/{tour_id}"
    request.scope = {"route": route}
    assert rate_limit._endpoint(request) == "/v1/tours/{tour_id}"
    request.scope = {}
    assert rate_limit._endpoint(request) == "/v1/tours/pool"


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool._conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_writer_cuts_endpoint_and_method_to_the_column_widths():
    conn = MagicMock()
    conn.execute = AsyncMock()
    await billing_service.track_api_call(
        _Pool(conn), None, "/admin/" + "y" * 200, "VERYLONGMETHOD", 200, 5, actor_type="admin",
    )
    args = conn.execute.await_args.args
    assert len(args[2]) == 100 and args[2].startswith("/admin/yyy")
    assert args[3] == "VERYLONGME"


@pytest.mark.asyncio
async def test_writer_failure_is_a_warning_with_traceback(caplog):
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("insert refused"))
    with caplog.at_level(logging.DEBUG, logger=billing_service.__name__):
        await billing_service.track_api_call(_Pool(conn), None, "/admin/x", "GET", 200, 1, actor_type="admin")
    recs = [r for r in caplog.records if r.name == billing_service.__name__]
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert recs[0].exc_info is not None
