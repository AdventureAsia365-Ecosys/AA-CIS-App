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
    """Checked inside the app (TestClient runs it in another thread): a key bound while serving the first request is
    not visible to the route of the second, and each route sees only its own request id."""
    app = FastAPI()

    @app.get("/v1/_bind")
    async def _bind() -> dict:
        structlog.contextvars.bind_contextvars(leak_probe="from-first")
        return structlog.contextvars.get_contextvars()

    @app.get("/v1/_ctx")
    async def _ctx() -> dict:
        return structlog.contextvars.get_contextvars()

    app.add_middleware(RequestIdMiddleware)
    with TestClient(app) as client:
        a = client.get("/v1/_bind", headers={"X-Request-ID": "req-first-1"})
        b = client.get("/v1/_ctx")
    assert a.json() == {"request_id": "req-first-1", "leak_probe": "from-first"}
    assert b.json() == {"request_id": b.headers["x-request-id"]}
    structlog.get_logger("api.after").info("after_requests")
    after = next(x for x in json_lines() if x["event"] == "after_requests")
    assert "request_id" not in after


def test_uvicorn_traceback_line_of_a_500_keeps_the_request_id(json_lines):
    """uvicorn logs "Exception in ASGI application" in the request's task after the app raised; the request id is
    still bound there (cleared at the start of a request, not at its end)."""
    inner = _probe_app()

    async def uvicorn_like(scope, receive, send):
        try:
            await inner(scope, receive, send)
        except Exception as exc:  # what uvicorn's run_asgi does
            logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)

    with TestClient(uvicorn_like, raise_server_exceptions=False) as client:
        r = client.get("/v1/_boom", headers={"X-Request-ID": "req-tb-500"})
    assert r.status_code == 500
    lines = json_lines()
    tb = next(x for x in lines if x["event"].startswith("Exception in ASGI application"))
    assert tb["request_id"] == "req-tb-500" and "RuntimeError: boom" in tb["exception"]
    done = next(x for x in lines if x["event"] == "request_completed")
    assert done["status"] == 500 and "aborted" not in done


def test_request_without_a_response_is_logged_as_aborted(json_lines):
    async def silent_app(scope, receive, send):  # e.g. the client went away before an answer
        return None

    async def run():
        sent = []

        async def send(message):
            sent.append(message)

        await RequestIdMiddleware(silent_app)({"type": "http", "method": "GET", "path": "/v1/x", "headers": []},
                                              AsyncMock(), send)
        return sent

    assert asyncio.run(run()) == []
    done = next(x for x in json_lines() if x["event"] == "request_completed")
    assert done["status"] is None and done["aborted"] is True


def test_explicit_request_id_is_logged_as_acp_request_id(json_lines):
    """T8/T9 code logs its angle-gate / content-writing request as request_id=...; the HTTP id must survive."""
    app = FastAPI()

    @app.get("/v1/_acp")
    async def _acp() -> dict:
        structlog.get_logger("api.acp").info("angle_gate_generation_enqueued", request_id="acp-req-1")
        structlog.get_logger("api.acp").bind(request_id="acp-req-2").warning("t8_route_digest_failed")
        return {}

    app.add_middleware(RequestIdMiddleware)
    with TestClient(app) as client:
        client.get("/v1/_acp", headers={"X-Request-ID": "req-http-1"})
    lines = {x["event"]: x for x in json_lines()}
    for event, acp in (("angle_gate_generation_enqueued", "acp-req-1"), ("t8_route_digest_failed", "acp-req-2")):
        assert lines[event]["request_id"] == "req-http-1" and lines[event]["acp_request_id"] == acp


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


def test_rate_limiter_binds_tenant_id(monkeypatch, json_lines):
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

    @app.middleware("http")  # like sentry_context_middleware in api.main: the rate limiter then runs in a child task
    async def _passthrough(request, call_next):
        return await call_next(request)

    app.add_middleware(RequestIdMiddleware)
    app.state.redis = MagicMock(incr=AsyncMock(return_value=1), expire=AsyncMock())
    app.state.pool = MagicMock()
    with TestClient(app) as client:
        r = client.get("/v1/_who", headers={"Authorization": "Bearer t", "X-Request-ID": "req-tenant-1"})
    assert r.json() == {"request_id": "req-tenant-1", "tenant_id": "00000000-0000-0000-0000-0000000000aa"}
    assert structlog.contextvars.get_contextvars() == {}
    done = next(x for x in json_lines() if x["event"] == "request_completed")
    assert done["tenant_id"] == "00000000-0000-0000-0000-0000000000aa" and done["route"] == "/v1/_who"


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


# --- staff routes keep the route template ----------------------------------------------------------------------------


def test_request_completed_has_the_route_for_staff_bearer_requests(json_lines, monkeypatch):
    """StaffBearer hands a copy of the scope to the router; the matched route is copied back to the outer scope."""
    from api.middleware import staff_bearer

    monkeypatch.setattr(staff_bearer, "_is_preview_credential", AsyncMock(return_value=True))
    app = FastAPI()

    @app.get("/admin/_probe/{item_id}")
    async def _probe(item_id: str) -> dict:
        return {}

    app.add_middleware(staff_bearer.StaffBearerMiddleware)
    app.add_middleware(RequestIdMiddleware)
    with TestClient(app) as client:
        assert client.get("/admin/_probe/7", headers={"X-Admin-Secret": "preview"}).status_code == 200
    done = next(x for x in json_lines() if x["event"] == "request_completed")
    assert done["route"] == "/admin/_probe/{item_id}"


# --- scrubbing -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, secret", [
    ("connect postgresql://cis_app:s3cr3tPw@db.internal:5432/aa", "s3cr3tPw"),
    ("connect postgresql://cis_app:s3cr3tPw@db.internal:5432/aa", "cis_app"),
    ("password=hunter2xyz&user=1", "hunter2xyz"),
    ('{"client_secret": "abcSECRET123"}', "abcSECRET123"),
    ("x-admin-secret: topSecretValue1", "topSecretValue1"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("authorization=Token abcdef123456", "abcdef123456"),
    ("sent Basic dXNlcjpwYXNzd29yZA== upstream", "dXNlcjpwYXNzd29yZA"),
])
def test_scrub_redacts_credentials(raw, secret):
    out = log_config._scrub_value(raw)
    assert secret not in out and "[Filtered]" in out


def test_scrub_keeps_ordinary_text():
    text = "basic information about postgresql://db.internal/aa, see https://x.example/a?page=2"
    assert log_config._scrub_value(text) == text
    dsn = log_config._scrub_value("postgresql://u:p@db.internal:5432/aa")
    assert dsn == "postgresql://[Filtered]@db.internal:5432/aa"


def test_scrub_handles_namedtuples_and_never_raises():
    from typing import NamedTuple

    class Pair(NamedTuple):
        mail: str
        n: int

    out = log_config.scrub_event(None, "info", {"event": "e", "pair": Pair("a@b.example.com", 1)})
    assert out["pair"] == Pair("[email]", 1)

    class Odd(tuple):  # a tuple subclass that cannot be rebuilt from an iterable
        def __new__(cls, a, b):
            return super().__new__(cls, (a, b))

    ed = {"event": "e", "odd": Odd("x", "y")}
    assert log_config.scrub_event(None, "info", ed) is ed


# --- Sentry events from log lines ------------------------------------------------------------------------------------


@pytest.fixture
def sentry_events(monkeypatch):
    """A real Sentry client (logging integration only, no network); yields the events after before_send."""
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.transport import Transport

    from shared import sentry_init

    class _NoTransport(Transport):
        def capture_envelope(self, envelope):
            pass

    events: list[dict] = []

    def capture(event, hint):
        out = sentry_init.before_send(event, hint)
        if out is not None:
            events.append(out)
        return None

    monkeypatch.setattr(sentry_init, "_throttle", {})
    monkeypatch.setattr(sentry_init, "throttled_events", 0)
    sentry_sdk.init(dsn="https://public@sentry.invalid/1", default_integrations=False,
                    auto_enabling_integrations=False, integrations=[LoggingIntegration()], before_send=capture,
                    transport=_NoTransport, send_client_reports=False)
    yield events
    sentry_sdk.get_client().close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)


def test_structlog_error_becomes_a_sentry_event_named_after_the_event(json_lines, sentry_events):
    structlog.get_logger("services.tours").error("tour_write_failed", tour_id="t-1", error="boom", attempt=2)
    (event,) = sentry_events
    assert event["logentry"]["message"] == "tour_write_failed"
    assert event["logentry"]["formatted"] == "tour_write_failed"
    assert event["extra"]["tour_id"] == "t-1" and event["extra"]["attempt"] == 2
    assert not {"event", "timestamp", "level", "logger", "service", "git_sha"} & set(event["extra"])
    assert event["level"] == "error" and event["logger"] == "services.tours"


@pytest.mark.parametrize("call", ["error_exc_info", "exception"])
def test_structlog_error_with_exc_info_carries_the_traceback(json_lines, sentry_events, call):
    log = structlog.get_logger("services.tours")
    try:
        raise ValueError("bad tour")
    except ValueError:
        if call == "exception":
            log.exception("tour_parse_failed", tour_id="t-2")
        else:
            log.error("tour_parse_failed", tour_id="t-2", exc_info=True)
    (event,) = sentry_events
    exc = event["exception"]["values"][-1]
    assert exc["type"] == "ValueError" and exc["value"] == "bad tour"
    assert exc["stacktrace"]["frames"]
    assert event["logentry"]["message"] == "tour_parse_failed"
    line = next(x for x in json_lines() if x["event"] == "tour_parse_failed")
    assert line["exception"].count("Traceback") == 1  # the JSON line is unchanged (one traceback)


def test_sentry_log_events_are_throttled_per_logger_and_event(json_lines, sentry_events):
    from shared import sentry_init

    log = structlog.get_logger("services.llm")
    for i in range(7):
        log.error("llm_call_failed", n=i)
    log.error("llm_other_failed")
    structlog.get_logger("services.other").error("llm_call_failed")
    names = [(e["logger"], e["logentry"]["message"]) for e in sentry_events]
    assert names.count(("services.llm", "llm_call_failed")) == sentry_init.THROTTLE_MAX
    assert ("services.llm", "llm_other_failed") in names and ("services.other", "llm_call_failed") in names
    assert sentry_init.throttled_events == 2
    # every log line is still written; only the Sentry events are capped
    assert sum(1 for x in json_lines() if x["event"] == "llm_call_failed") == 8


def test_throttle_window_reopens(monkeypatch):
    from shared import sentry_init

    monkeypatch.setattr(sentry_init, "_throttle", {})
    key = ("l", "e")
    assert not any(sentry_init._throttled(key, now=float(i)) for i in range(sentry_init.THROTTLE_MAX))
    assert sentry_init._throttled(key, now=10.0)
    assert not sentry_init._throttled(key, now=sentry_init.THROTTLE_WINDOW_S + 1.0)


def test_api_sentry_ignores_uvicorn_error_logger(monkeypatch):
    from unittest.mock import patch

    from sentry_sdk.integrations import logging as sentry_logging

    from shared import sentry_init

    monkeypatch.setattr(sentry_logging, "_IGNORED_LOGGERS", set(sentry_logging._IGNORED_LOGGERS))
    with patch.object(sentry_init, "_get_dsn", return_value="https://k@sentry.invalid/1"), \
            patch("sentry_sdk.init"), patch("sentry_sdk.set_tag"):
        sentry_init.init_sentry(service="api")
    assert "uvicorn.error" in sentry_logging._IGNORED_LOGGERS


def test_failed_job_reaches_sentry_with_traceback_and_job_tags(json_lines, sentry_events, monkeypatch):
    async def handler(ctx):
        raise RuntimeError("handler broke")

    monkeypatch.setitem(registry._KINDS, "aa871_fail", registry.JobKind("aa871_fail", handler))
    monkeypatch.setattr(queue, "fail", AsyncMock(return_value="queued"))
    w = worker_mod.Worker(MagicMock(), worker_id="w-aa871")
    job = queue.Job("22222222-2222-2222-2222-222222222222", "aa871_fail", {}, "running", 1, 3, {}, 0.0)
    asyncio.run(w._execute(job))
    (event,) = [e for e in sentry_events if e["logentry"]["message"] == "job_failed"]
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert event["tags"]["job_id"] == job.id and event["tags"]["job_kind"] == "aa871_fail"
    assert event["extra"]["next_status"] == "queued"
    line = next(x for x in json_lines() if x["event"] == "job_failed")
    assert line["level"] == "error" and "RuntimeError: handler broke" in line["exception"]


def test_first_process_configuration_wins(monkeypatch):
    """Live S247: the worker said service=cis-api because a later import of api.main re-configured logging."""
    calls = []
    monkeypatch.setattr(log_config, "configure_logging", lambda service, **kw: calls.append(service))
    monkeypatch.setattr(log_config, "_process_service", None)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    log_config.configure_for_process("cis-worker")
    log_config.configure_for_process("cis-api")
    assert calls == ["cis-worker"]
