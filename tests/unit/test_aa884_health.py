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
    sql = pool.fetchval.await_args_list[0].args[0]
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
    assert pool.fetchval.await_count == 2          # the version read + the registered keys read (AA-993 M4a)
    await readiness.readiness(pool, now=105.5)
    assert pool.fetchval.await_count == 4


@pytest.mark.asyncio
async def test_ready_passes_the_out_of_order_versions_to_the_cap():
    # AA-920: 297 is registered after 299; max() alone would report ready without it. The SQL caps the reported
    # version below the lowest listed version that is not registered (the cap itself runs on real Postgres in
    # tests/integration/test_aa920_migration_297.py).
    pool = _pool(readiness.REQUIRED_CIS_SCHEMA)
    await readiness.readiness(pool, now=0.0)
    sql, versions = pool.fetchval.await_args_list[0].args
    assert versions == list(readiness.OUT_OF_ORDER_CIS) and 297 in versions
    assert "unnest($1::int[])" in sql and "least(m.head, m.missing - 1)" in sql
    assert all(v <= readiness.REQUIRED_CIS_SCHEMA for v in readiness.OUT_OF_ORDER_CIS)


# ── AA-993 M4a: registered_keys and REQUIRED_CIS_KEYS ──
def _keys_pool(version, keys, keys_exc=None):
    """fetchval answering by statement: the version read, then the registered keys read."""
    async def fetchval(sql, *args):
        if sql == readiness.REGISTERED_KEYS_SQL:
            assert args == (readiness.LEGACY_CIS_BASELINE,)
            if keys_exc is not None:
                raise keys_exc
            return keys
        return version
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=fetchval)
    return pool


ALL_KEYS = list(readiness.REQUIRED_CIS_KEYS)


@pytest.mark.asyncio
async def test_ready_lists_the_registered_keys_above_the_baseline():
    # One key newer than the required schema (a later migration already applied) is listed too.
    newer = readiness.REQUIRED_CIS_SCHEMA + 1
    status, body = await readiness.readiness(_keys_pool(newer, ALL_KEYS + [newer]), now=0.0)
    assert status == 200 and body["status"] == "ok"
    assert body["registered_keys"] == [str(k) for k in ALL_KEYS] + [str(newer)]
    assert body["keys_baseline"] == str(readiness.LEGACY_CIS_BASELINE)
    assert "missing_schema_versions" not in body


@pytest.mark.asyncio
async def test_a_gap_under_the_required_key_is_schema_behind():
    gap = [k for k in ALL_KEYS if k != 280]
    status, body = await readiness.readiness(_keys_pool(readiness.REQUIRED_CIS_SCHEMA, gap), now=0.0)
    assert status == 503 and body["status"] == "schema_behind"
    assert body["missing_schema_versions"] == ["280"]
    assert "280" not in body["registered_keys"]


@pytest.mark.asyncio
async def test_a_failed_keys_read_keeps_the_version_rule():
    pool = _keys_pool(readiness.REQUIRED_CIS_SCHEMA, None, keys_exc=OSError("boom"))
    status, body = await readiness.readiness(pool, now=0.0)
    assert status == 200 and "registered_keys" not in body


def test_required_keys_run_from_the_baseline_to_the_required_schema():
    keys = readiness.REQUIRED_CIS_KEYS
    assert keys[0] == readiness.LEGACY_CIS_BASELINE + 1 and keys[-1] == readiness.REQUIRED_CIS_SCHEMA
    assert list(keys) == list(range(keys[0], keys[-1] + 1))
    assert set(readiness.OUT_OF_ORDER_CIS) <= set(keys)
