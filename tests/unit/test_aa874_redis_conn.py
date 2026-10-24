"""AA-874 — Redis URL + TLS / AUTH kwargs from env (shared/cache/redis_conn.py).

The same image must run against the old plain node (no REDIS_TLS, no token: exactly the old
`redis://<host>:6379`) and against the new TLS + AUTH replication group, and the token must never be part
of the URL. These tests build real redis-py clients (no I/O) and read their connection kwargs.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as aioredis
from redis.asyncio.connection import SSLConnection
from structlog.testing import capture_logs

from api.core import redis_client
from shared.cache import redis_conn

REPO = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40 + "AA874tokenvalue"
REDIS_ENV = ("REDIS_HOST", "REDIS_PORT", "REDIS_TLS", "REDIS_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in REDIS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(redis_conn, "_auth_without_tls_logged", False)


def test_no_env_is_localhost_plain_without_kwargs():
    assert redis_conn.redis_conn_args() == ("redis://localhost:6379", {})


def test_host_only_is_the_old_plain_url(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    assert redis_conn.redis_conn_args() == ("redis://cache.internal:6379", {})


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "nope"])
def test_tls_off_values_keep_the_plain_scheme(monkeypatch, value):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_TLS", value)
    assert redis_conn.redis_conn_args() == ("redis://cache.internal:6379", {})


def test_port_override(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_PORT", "6380")
    assert redis_conn.redis_conn_args()[0] == "redis://cache.internal:6380"


@pytest.mark.parametrize("value", ["true", "TRUE", " 1 ", "yes", "on"])
def test_tls_with_token_uses_rediss_and_password_kwarg(monkeypatch, value):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_TLS", value)
    monkeypatch.setenv("REDIS_AUTH_TOKEN", TOKEN)
    url, kwargs = redis_conn.redis_conn_args()
    assert url == "rediss://cache.internal:6379"
    assert TOKEN not in url
    assert kwargs == {"ssl_cert_reqs": "required", "ssl_check_hostname": True, "password": TOKEN}


def test_tls_without_token_has_no_password(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_TLS", "true")
    url, kwargs = redis_conn.redis_conn_args()
    assert url == "rediss://cache.internal:6379"
    assert "password" not in kwargs


def test_token_without_tls_logs_one_error_and_drops_the_token(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_AUTH_TOKEN", TOKEN)
    with capture_logs() as logs:
        first = redis_conn.redis_conn_args()
        second = redis_conn.redis_conn_args()
    assert first == second == ("redis://cache.internal:6379", {})
    errors = [e for e in logs if e["event"] == "aa874_redis_auth_without_tls"]
    assert len(errors) == 1 and errors[0]["log_level"] == "error"
    assert TOKEN not in repr(logs)


def test_api_client_carries_tls_and_auth_and_keeps_the_s241_bounds(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_TLS", "true")
    monkeypatch.setenv("REDIS_AUTH_TOKEN", TOKEN)
    url, kwargs = redis_conn.redis_conn_args()
    client = redis_client.build_api_redis(url, **kwargs)
    pool = client.connection_pool
    kw = pool.connection_kwargs
    assert pool.connection_class is SSLConnection
    assert kw["password"] == TOKEN
    assert kw["ssl_cert_reqs"] == "required" and kw["ssl_check_hostname"] is True
    assert kw["socket_connect_timeout"] == kw["socket_timeout"] == 0.5
    assert kw["retry"]._retries == redis_client.REDIS_RETRIES
    assert TOKEN not in repr(client)


def test_plain_client_is_unchanged(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    url, kwargs = redis_conn.redis_conn_args()
    client = aioredis.from_url(url, decode_responses=True, **kwargs)
    pool = client.connection_pool
    assert pool.connection_class is not SSLConnection
    assert pool.connection_kwargs["host"] == "cache.internal"
    assert pool.connection_kwargs.get("password") is None


@pytest.mark.asyncio
async def test_api_lifespan_uses_the_env_tls_settings(monkeypatch):
    from api import main

    pool = MagicMock()
    pool.close = AsyncMock()
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_TLS", "true")
    monkeypatch.setenv("REDIS_AUTH_TOKEN", TOKEN)
    with patch.object(main.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch("api.core.aa544_tenant_pool.create_tenant_pool", AsyncMock(return_value=None)), \
            patch("shared.jobs.worker.in_api_enabled", return_value=False), \
            patch("api.core.aa859_main_pool_gauge.start_main_pool_gauge", return_value=None), \
            patch("api.core.aa859_main_pool_gauge.stop_main_pool_gauge", AsyncMock()), \
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z", "ENVIRONMENT": "test"}):
        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            cp = app.state.redis.connection_pool
            assert cp.connection_class is SSLConnection
            assert cp.connection_kwargs["host"] == "cache.internal"
            assert cp.connection_kwargs["password"] == TOKEN
            assert cp.connection_kwargs["socket_timeout"] == 0.5


def test_no_infra_hostname_or_inline_redis_url_in_the_redis_call_sites():
    """DM-6.10: no ElastiCache host name in this public repo, and every client is built from
    redis_conn_args() (a hand-built `redis://` f-string would silently skip TLS + AUTH)."""
    for rel in ("api/main.py", "shared/jobs/worker.py", "services/seo_intelligence/handler.py"):
        src = (REPO / rel).read_text()
        assert "cache.amazonaws.com" not in src, rel
        assert "redis_conn_args()" in src, rel
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
                assert not text.startswith(("redis://", "rediss://")), f"{rel}: inline Redis URL"
