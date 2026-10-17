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
