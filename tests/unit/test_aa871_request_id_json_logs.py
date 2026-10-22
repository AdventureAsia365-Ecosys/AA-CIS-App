"""AA-871 — JSON log lines with request ids (API) and job ids (worker)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware import request_id
from api.middleware.request_id import RequestIdMiddleware, unhandled_error_handler
from shared import log_config
from shared.jobs import queue, registry
from shared.jobs import worker as worker_mod


@pytest.fixture
def json_lines():
    """Route the root logger + structlog through the JSON handler into a buffer; yields a reader of parsed lines.
    Restores the previous logging and structlog set-up afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_structlog = structlog.get_config()
    access = logging.getLogger("uvicorn.access")
    saved_access = (list(access.handlers), access.propagate, access.disabled)
    buf = io.StringIO()
    log_config.configure_logging("cis-api", stream=buf)

    def read() -> list[dict]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    yield read
    for h in list(root.handlers):
        if h.get_name() == log_config.HANDLER_NAME:
            root.removeHandler(h)
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)
    access.handlers[:] = saved_access[0]
    access.propagate, access.disabled = saved_access[1], saved_access[2]
    structlog.configure(**saved_structlog)
    structlog.contextvars.clear_contextvars()


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/_probe/{item_id}")
    async def _probe(item_id: str) -> dict:
        structlog.get_logger("api.probe").info("probe_hit", item_id=item_id)
        logging.getLogger("api.probe.stdlib").info("stdlib %s", "line")
        return {"ok": True, "ctx": structlog.contextvars.get_contextvars()}

    @app.get("/v1/_boom")
    async def _boom() -> dict:
        raise RuntimeError("boom")

    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(RequestIdMiddleware)
    return app


# --- request id: accept / replace / generate ------------------------------------------------------------------------


@pytest.mark.parametrize("good", [
    "3f1c2a9e-8f0b-4c1d-9e2a-0b1c2d3e4f50",  # UUID
    "0123456789abcdef0123456789abcdef",      # uuid4().hex
    "req_0001",
    "x" * 64,
])
def test_well_formed_request_id_is_accepted(good):
    assert request_id.accept_request_id(good) == good


@pytest.mark.parametrize("bad", [
    None, "", "short", "x" * 65, "a b c d e f", "id\r\nSet-Cookie: x", "<script>alert</script>", "ééééééééé",
    "a.b.c.d.e.f", "user@example.com",
])
def test_malformed_request_id_is_rejected(bad):
    assert request_id.accept_request_id(bad) is None


def test_request_id_generated_when_absent():
    with TestClient(_probe_app()) as client:
        r = client.get("/v1/_probe/1")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and int(rid, 16) >= 0


def test_valid_incoming_request_id_is_echoed():
    rid = "3f1c2a9e-8f0b-4c1d-9e2a-0b1c2d3e4f50"
    with TestClient(_probe_app()) as client:
        r = client.get("/v1/_probe/1", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid
    assert r.json()["ctx"] == {"request_id": rid}


def test_invalid_incoming_request_id_is_replaced():
    with TestClient(_probe_app()) as client:
        r = client.get("/v1/_probe/1", headers={"X-Request-ID": "<bad id>"})
    rid = r.headers["x-request-id"]
    assert rid != "<bad id>" and len(rid) == 32


def test_unhandled_500_keeps_the_request_id():
    with TestClient(_probe_app(), raise_server_exceptions=False) as client:
        r = client.get("/v1/_boom", headers={"X-Request-ID": "req-500-abc"})
    assert r.status_code == 500 and r.text == "Internal Server Error"
    assert r.headers["x-request-id"] == "req-500-abc"


# --- the real app ----------------------------------------------------------------------------------------------------


def test_request_id_middleware_is_outermost_in_the_real_app():
    from api.main import app
    assert app.user_middleware[0].cls is RequestIdMiddleware
    assert app.exception_handlers.get(Exception) is unhandled_error_handler


def test_refused_staff_call_on_the_real_app_carries_the_header():
    """A refusal (401/403/503) still carries X-Request-ID, and a valid incoming one is echoed."""
    from api.main import app
    client = TestClient(app)
    r = client.get("/admin/tenants")
    assert r.status_code in (401, 403, 503)  # 503: no ADMIN_SECRET in the unit env (fails closed)
    assert len(r.headers["x-request-id"]) == 32
    r = client.get("/admin/tenants", headers={"X-Request-ID": "req-refused-1"})
    assert r.headers["x-request-id"] == "req-refused-1"


# --- JSON lines ------------------------------------------------------------------------------------------------------


def test_every_line_of_a_request_is_json_with_its_request_id(json_lines):
    with TestClient(_probe_app()) as client:
        client.get("/v1/_probe/42?q=secret-search-text&api_key=k123", headers={"X-Request-ID": "req-json-1"})
    lines = [x for x in json_lines() if x.get("request_id") == "req-json-1"]
    events = [x["event"] for x in lines]
    assert "probe_hit" in events and "stdlib line" in events
    done = next(x for x in lines if x["event"] == "request_completed")
    assert done["method"] == "GET" and done["status"] == 200
    assert done["path"] == "/v1/_probe/42" and done["route"] == "/v1/_probe/{item_id}"
    assert isinstance(done["duration_ms"], float)
    for x in lines:
        assert x["service"] == "cis-api" and {"timestamp", "level", "logger", "environment", "git_sha"} <= set(x)
    text = json.dumps(json_lines())
    assert "secret-search-text" not in text and "k123" not in text  # no query string, ever


def test_request_ids_do_not_leak_between_requests(json_lines):
    with TestClient(_probe_app()) as client:
        a = client.get("/v1/_probe/1", headers={"X-Request-ID": "req-first-1"})
        b = client.get("/v1/_probe/2")
    assert a.json()["ctx"] == {"request_id": "req-first-1"}
    assert b.json()["ctx"] == {"request_id": b.headers["x-request-id"]}
    structlog.get_logger("api.after").info("after_requests")
    after = next(x for x in json_lines() if x["event"] == "after_requests")
    assert "request_id" not in after
    assert structlog.contextvars.get_contextvars() == {}


def test_lines_are_scrubbed_and_uvicorn_access_is_replaced(json_lines):
    structlog.get_logger("api.pii").warning("lookup_failed", detail="mail jane@example.com",
                                            url="https://x.example/v1?apiKey=abc123", auth="Bearer abcdefghijkl")
    line = next(x for x in json_lines() if x["event"] == "lookup_failed")
    assert "jane@example.com" not in json.dumps(line) and "abc123" not in line["url"]
    assert "abcdefghijkl" not in line["auth"]
    assert line["level"] == "warning"
    assert logging.getLogger("uvicorn.access").disabled is True
    assert logging.getLogger("uvicorn.error").propagate is True


def test_metric_filter_substrings_still_match(json_lines):
    """Infra metric filters on /ecs/aa-cis-dev match quoted substrings; they must still appear in a JSON line."""
    structlog.get_logger().error("llm_call_log_write_failed", error="x")
    logging.getLogger("asyncpg.test").error('too many connections for role "cis_app"')
    raw = "\n".join(json.dumps(x) for x in json_lines())
    assert "llm_call_log_write_failed" in raw
    assert "too many connections for role" in raw and "cis_app" in raw


def test_configure_for_process_is_a_no_op_under_unit_tests(monkeypatch):
    called = []
    monkeypatch.setattr(log_config, "configure_logging", lambda *a, **k: called.append(a))
    monkeypatch.setenv("ENVIRONMENT", "test")
    log_config.configure_for_process("cis-api")
    assert called == []
    monkeypatch.setenv("ENVIRONMENT", "dev")
    log_config.configure_for_process("cis-worker")
    assert called == [("cis-worker",)]


# --- rate limiter binds the verified tenant --------------------------------------------------------------------------


def test_rate_limiter_binds_tenant_id(monkeypatch):
    from api.middleware import rate_limit

    monkeypatch.setattr(rate_limit, "verify_jwt", lambda _t: {"sub": "00000000-0000-0000-0000-0000000000aa"})
    monkeypatch.setattr(rate_limit, "_get_tenant_meta", AsyncMock(return_value={"rate_limit_rpm": 60,
                                                                                 "is_active": True}))
    monkeypatch.setattr(rate_limit, "track_api_call", AsyncMock())
    app = FastAPI()

    @app.get("/v1/_who")
    async def _who() -> dict:
        return structlog.contextvars.get_contextvars()

    app.middleware("http")(rate_limit.rate_limit_middleware)
    app.add_middleware(RequestIdMiddleware)
    app.state.redis = MagicMock(incr=AsyncMock(return_value=1), expire=AsyncMock())
    app.state.pool = MagicMock()
    with TestClient(app) as client:
        r = client.get("/v1/_who", headers={"Authorization": "Bearer t", "X-Request-ID": "req-tenant-1"})
    assert r.json() == {"request_id": "req-tenant-1", "tenant_id": "00000000-0000-0000-0000-0000000000aa"}
    assert structlog.contextvars.get_contextvars() == {}


# --- worker binds job ids --------------------------------------------------------------------------------------------


def test_worker_binds_job_id_and_kind_while_the_job_runs(json_lines, monkeypatch):
    seen: dict = {}

    async def handler(ctx):
        seen.update(structlog.contextvars.get_contextvars())
        structlog.get_logger("jobs.test").info("handler_ran")
        return {"ok": True}

    monkeypatch.setitem(registry._KINDS, "aa871_test", registry.JobKind("aa871_test", handler))
    monkeypatch.setattr(queue, "complete", AsyncMock())
    pool = MagicMock(fetchval=AsyncMock(return_value=0.0))
    w = worker_mod.Worker(pool, worker_id="w-aa871")
    job = queue.Job("11111111-1111-1111-1111-111111111111", "aa871_test", {}, "running", 1, 3, {}, 0.0)

    async def run():
        await w._execute(job)
        return structlog.contextvars.get_contextvars()

    after = asyncio.run(run())
    assert seen == {"job_id": job.id, "job_kind": "aa871_test"}
    assert after == {}
    lines = json_lines()
    ran = next(x for x in lines if x["event"] == "handler_ran")
    assert ran["job_id"] == job.id and ran["job_kind"] == "aa871_test"
    done = next(x for x in lines if x["event"] == "job_succeeded")
    assert done["job_id"] == job.id and done["job_kind"] == "aa871_test"
