"""
Tests for Sentry error tracking integration (AA-142).
All sentry_sdk calls are mocked — no Sentry account required.
"""
import pytest
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# init_sentry() unit tests
# ---------------------------------------------------------------------------

def test_sentry_init_graceful_no_dsn():
    """init_sentry() must not raise when DSN fetch returns empty string."""
    with patch("shared.sentry_init._get_dsn", return_value=""), \
         patch("sentry_sdk.init") as mock_init:
        from api.core.sentry import init_sentry
        init_sentry()
    mock_init.assert_not_called()


def test_sentry_init_with_dsn():
    """sentry_sdk.init is called with correct dsn and environment when DSN available."""
    test_dsn = "https://abc123@sentry.io/456"
    with patch("shared.sentry_init._get_dsn", return_value=test_dsn), \
         patch("sentry_sdk.init") as mock_init:
        from api.core.sentry import init_sentry
        init_sentry()
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == test_dsn
    assert "environment" in kwargs
    assert kwargs["traces_sample_rate"] == 0.1


def test_get_dsn_returns_empty_on_boto3_error():
    """_get_dsn() returns empty string when Secrets Manager call fails."""
    with patch("shared.sentry_init.boto3") as mock_boto3:
        mock_boto3.client.side_effect = Exception("no credentials")
        from api.core.sentry import _get_dsn
        result = _get_dsn()
    assert result == ""


# ---------------------------------------------------------------------------
# sentry_context_middleware tag tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sentry_middleware_sets_tags():
    """Middleware sets tenant_id, run_id, and route tags from request."""
    from api.middleware.sentry_context import sentry_context_middleware

    mock_request = MagicMock()
    mock_request.headers.get = lambda k, d="": {
        "x-tenant-id": "tenant-abc",
        "x-run-id": "run-xyz",
    }.get(k, d)
    mock_request.url.path = "/acp/s1/runs"

    async def mock_call_next(req):
        return MagicMock()

    with patch("sentry_sdk.set_tag") as mock_set_tag:
        await sentry_context_middleware(mock_request, mock_call_next)

    mock_set_tag.assert_any_call("tenant_id", "tenant-abc")
    mock_set_tag.assert_any_call("run_id", "run-xyz")
    mock_set_tag.assert_any_call("route", "/acp/s1/runs")


@pytest.mark.asyncio
async def test_sentry_middleware_unknown_tenant_fallback():
    """Middleware falls back to 'unknown' when x-tenant-id header is absent."""
    from api.middleware.sentry_context import sentry_context_middleware

    mock_request = MagicMock()
    mock_request.headers.get = lambda k, d="": d
    mock_request.url.path = "/health"

    async def mock_call_next(req):
        return MagicMock()

    with patch("sentry_sdk.set_tag") as mock_set_tag:
        await sentry_context_middleware(mock_request, mock_call_next)

    mock_set_tag.assert_any_call("tenant_id", "unknown")


# ---------------------------------------------------------------------------
# capture_exception pattern test
# ---------------------------------------------------------------------------

def test_sentry_captures_500():
    """sentry_sdk.capture_exception is called when an exception is captured."""
    with patch("sentry_sdk.capture_exception") as mock_capture:
        import sentry_sdk
        exc = RuntimeError("Internal server error simulation")
        sentry_sdk.capture_exception(exc)
    mock_capture.assert_called_once_with(exc)


# ---------------------------------------------------------------------------
# AA-870: privacy scrubbing, release, worker init
# ---------------------------------------------------------------------------

def test_before_send_drops_body_cookies_and_unsafe_headers():
    from shared.sentry_init import before_send
    event = {"request": {
        "url": "https://api-cis.lumiguides.it.com/x?apiKey=abc123&page=2",
        "query_string": "apiKey=abc123&page=2",
        "data": {"email": "guest@example.com", "password": "p"},
        "cookies": {"session": "s"},
        "headers": {"X-Admin-Secret": "s3cret", "Authorization": "Bearer t", "x-api-key": "k",
                    "x-tripplanner-key": "k2", "Cookie": "a=b", "User-Agent": "ua", "x-tenant-id": "t1"},
    }}
    out = before_send(event, {})["request"]
    assert "data" not in out and "cookies" not in out
    assert out["headers"] == {"User-Agent": "ua", "x-tenant-id": "t1"}
    assert "abc123" not in out["url"] and "abc123" not in out["query_string"]
    assert "page=2" in out["query_string"]


def test_before_send_without_request_is_untouched():
    from shared.sentry_init import before_send
    event = {"message": "job failed", "level": "error"}
    assert before_send(dict(event), {}) == event


def test_init_passes_privacy_options_and_release(monkeypatch):
    monkeypatch.setenv("APP_GIT_SHA", "abc123sha")
    with patch("shared.sentry_init._get_dsn", return_value="https://k@sentry.io/1"), \
         patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag") as mock_tag:
        from shared.sentry_init import init_sentry, before_send
        init_sentry(service="api")
    kw = mock_init.call_args.kwargs
    assert kw["release"] == "abc123sha"
    assert kw["send_default_pii"] is False
    assert kw["max_request_body_size"] == "never"
    assert kw["include_local_variables"] is False
    assert kw["before_send"] is before_send
    mock_tag.assert_called_once_with("service", "api")


def test_release_unknown_without_build_arg(monkeypatch):
    monkeypatch.delenv("APP_GIT_SHA", raising=False)
    from shared.sentry_init import release
    assert release() == "unknown"


def test_worker_init_has_no_web_integrations():
    with patch("shared.sentry_init._get_dsn", return_value="https://k@sentry.io/1"), \
         patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag") as mock_tag:
        from shared.sentry_init import init_sentry
        init_sentry(service="worker")
    assert mock_init.call_args.kwargs["integrations"] == []
    mock_tag.assert_called_once_with("service", "worker")


def test_worker_entry_does_not_import_api_package():
    # `api/__init__` builds the FastAPI app; the worker must reach Sentry through shared/ only.
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "worker" / "__main__.py").read_text()
    assert "from shared.sentry_init import init_sentry" in src
    assert "import api" not in src and "from api" not in src


# ---------------------------------------------------------------------------
# AA-941 (S249 security review): no frame locals, credentials and e-mails redacted
# ---------------------------------------------------------------------------

# Built at run time so the secret scan does not read the test token as a committed JWT.
_JWT = ".".join(("eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiI0YTFiIiwiZW1haWwiOiJ4QHkuY29tIn0",
                 "c2lnbmF0dXJlLXBhcnQtb2YtdGhlLXRva2Vu"))
_MAIL = "victim.owner@agency.example"


def test_before_send_drops_locals_of_every_frame_whatever_the_event():
    from shared.sentry_init import before_send
    frame = {"module": "api.middleware.staff_bearer", "function": "__call__", "lineno": 7, "filename": "x.py",
             "vars": {"scope": {"headers": [[b"authorization", ("Bearer " + _JWT).encode()]]}}}
    event = {"stacktrace": {"frames": [dict(frame)]},
             "exception": {"values": [{"type": "RuntimeError", "stacktrace": {"frames": [dict(frame)]}}]},
             "threads": {"values": [{"stacktrace": {"frames": [dict(frame)]}}]}}
    out = before_send(event, {})
    for f in (out["stacktrace"]["frames"] + out["exception"]["values"][0]["stacktrace"]["frames"]
              + out["threads"]["values"][0]["stacktrace"]["frames"]):
        assert "vars" not in f
        assert f["function"] == "__call__" and f["lineno"] == 7                       # the frame itself stays


def test_before_send_redacts_jwt_bearer_and_email_in_text_fields():
    import json
    from shared.sentry_init import before_send
    event = {
        "message": f"owner {_MAIL}",
        "logentry": {"message": "invite_failed", "formatted": f"invite_failed for {_MAIL}"},
        "exception": {"values": [{"type": "ValueError", "value": f"bad token {_JWT} for {_MAIL}"}]},
        "extra": {"email": _MAIL, "auth": f"Bearer opaque-{_JWT[:20]}", "nested": [{"t": _JWT}], "n": 3},
        "breadcrumbs": {"values": [{"message": f"GET /x?token=abc {_MAIL}", "data": {"h": f"Bearer {_JWT}"}}]},
        "request": {"url": f"https://h/x?apiKey=k1&to={_MAIL}", "query_string": f"to={_MAIL}"},
    }
    blob = json.dumps(before_send(event, {}))
    assert _MAIL not in blob and _JWT not in blob and "opaque-" not in blob and "k1" not in blob
    assert "abc" not in blob
    assert event["logentry"]["message"] == "invite_failed" and event["extra"]["n"] == 3
    assert event["exception"]["values"][0]["type"] == "ValueError"


def test_before_breadcrumb_redacts_credentials_and_email():
    from shared.sentry_init import before_breadcrumb
    crumb = before_breadcrumb({"message": f"login {_MAIL}", "data": {"url": "https://h/?key=s", "a": _JWT}}, {})
    assert _MAIL not in crumb["message"] and _JWT not in str(crumb["data"]) and "key=s" not in crumb["data"]["url"]


def test_unhandled_500_on_staff_route_sends_no_token_and_no_body(monkeypatch):
    """The S249 repro, end to end through init_sentry, the Starlette/FastAPI integrations and the real
    StaffBearerMiddleware: a Cognito-verified POST whose route raises. Before AA-941 the event carried the full
    access token (middleware + Starlette frame locals) and the JSON body's e-mail (FastAPI `body` / `values`)."""
    import json

    import sentry_sdk
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel
    from sentry_sdk.transport import Transport

    from api.core import identity_config, staff_identity
    from api.middleware.staff_bearer import StaffBearerMiddleware
    from shared import sentry_init as si
    from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

    events: list = []

    class _Capture(Transport):
        def capture_envelope(self, envelope):
            ev = envelope.get_event()
            if ev is not None:
                events.append(ev)

    real_init = sentry_sdk.init

    def init(**kw):
        return real_init(transport=_Capture, send_client_reports=False, **kw)

    monkeypatch.setattr(si, "_get_dsn", lambda: "https://public@sentry.invalid/1")
    monkeypatch.setattr(si.sentry_sdk, "init", init)
    monkeypatch.setattr(si.sentry_sdk, "set_tag", lambda *a, **k: None)
    monkeypatch.setattr(si, "_throttle", {})

    issuer, _ = install_issuer()
    directory = Directory()
    directory.add(role="admin", iss=ISS, sub=SUB)

    class Invite(BaseModel):
        email: str
        ticket_ref: str

    app = FastAPI()

    @app.post("/admin/aa941/owner-invite")
    async def owner_invite(body: Invite):
        invite_email = body.email                                   # a local holding the e-mail
        raise RuntimeError(f"upstream refused ({len(invite_email)})")

    app.add_middleware(StaffBearerMiddleware)
    app.state.pool = directory
    try:
        si.init_sentry("api")
        token = issuer.token()
        r = TestClient(app, raise_server_exceptions=False).post(
            "/admin/aa941/owner-invite", headers={"Authorization": f"Bearer {token}"},
            json={"email": _MAIL, "ticket_ref": "AA-1"})
        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)
        identity_config.reset_cis_staff_verifier()
        staff_identity.clear_cache()

    assert r.status_code == 500
    errors = [e for e in events if e.get("exception")]
    assert errors, "the unhandled 500 must still reach Sentry"
    blob = json.dumps(events, default=str)
    sig = token.rsplit(".", 1)[1]
    assert token not in blob and sig not in blob and token.split(".")[1][:40] not in blob
    assert _MAIL not in blob
    frames = [f for e in errors for v in e["exception"]["values"] for f in v["stacktrace"]["frames"]]
    assert frames and all("vars" not in f for f in frames)
    assert any(f.get("function") == "owner_invite" for f in frames)                # stack trace kept
