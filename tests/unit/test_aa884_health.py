"""AA-884 — /health carries git_sha, /health/ready reports DB + the newest numeric CIS migration (cached 5 s)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.core import readiness


@pytest.fixture(autouse=True)
def _fresh_cache():
    readiness.reset_cache()
    yield
    readiness.reset_cache()


def _pool(value=None, exc=None):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=value, side_effect=exc)
    return pool


def test_health_keeps_status_ok_and_adds_git_sha(monkeypatch):
    from api import main

    monkeypatch.setenv("APP_GIT_SHA", "abc123def")
    body = TestClient(main.app).get("/health").json()
    assert body["status"] == "ok" and list(body)[0] == "status"
    assert body["git_sha"] == "abc123def" and body["service"] == "aa-cis-api"


def test_health_git_sha_unknown_outside_an_image(monkeypatch):
    from api import main

    monkeypatch.delenv("APP_GIT_SHA", raising=False)
    assert TestClient(main.app).get("/health").json()["git_sha"] == "unknown"


def test_health_ready_route_ok_and_503(monkeypatch):
    from api import main

    monkeypatch.setattr(main, "pool", _pool(readiness.REQUIRED_CIS_SCHEMA))
    client = TestClient(main.app)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["schema_version"] == str(readiness.REQUIRED_CIS_SCHEMA) and resp.json()["db"] == "ok"
    readiness.reset_cache()
    monkeypatch.setattr(main, "pool", _pool(readiness.REQUIRED_CIS_SCHEMA - 1))
    resp = client.get("/health/ready")
    assert resp.status_code == 503 and resp.json()["status"] == "schema_behind"


@pytest.mark.asyncio
async def test_ready_compares_numeric_versions_only():
    pool = _pool(readiness.REQUIRED_CIS_SCHEMA + 3)
    status, body = await readiness.readiness(pool, now=0.0)
    assert status == 200 and body["status"] == "ok"
    sql = pool.fetchval.await_args.args[0]
    assert "version ~ '^[0-9]+$'" in sql and "version::int" in sql  # bk-/tp-/rl- keys never compared


@pytest.mark.asyncio
async def test_ready_db_error_is_503_not_500():
    status, body = await readiness.readiness(_pool(exc=OSError("connection refused")), now=0.0)
    assert status == 503 and body["db"] == "error" and body["schema_version"] is None


@pytest.mark.asyncio
async def test_ready_no_pool_and_no_versions():
    assert (await readiness.readiness(None, now=0.0))[0] == 503
    readiness.reset_cache()
    status, body = await readiness.readiness(_pool(None), now=0.0)
    assert status == 503 and body["schema_version"] is None


@pytest.mark.asyncio
async def test_ready_is_cached_for_five_seconds():
    pool = _pool(readiness.REQUIRED_CIS_SCHEMA)
    await readiness.readiness(pool, now=100.0)
    await readiness.readiness(pool, now=104.0)
    assert pool.fetchval.await_count == 1
    await readiness.readiness(pool, now=105.5)
    assert pool.fetchval.await_count == 2
