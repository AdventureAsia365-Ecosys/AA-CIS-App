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


ALL_KEYS = sorted(readiness.REQUIRED_CIS_KEYS)


@pytest.mark.asyncio
async def test_ready_lists_the_registered_keys_above_the_baseline():
    # One key newer than the required schema (a later migration already applied) is listed too.
    newer = max(ALL_KEYS) + 1
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
    gap = readiness.REQUIRED_CIS_ABOVE_GAP
    keys = readiness.REQUIRED_CIS_KEYS[:len(readiness.REQUIRED_CIS_KEYS) - len(gap)]
    assert keys[0] == readiness.LEGACY_CIS_BASELINE + 1 and keys[-1] == readiness.REQUIRED_CIS_SCHEMA
    assert list(keys) == list(range(keys[0], keys[-1] + 1))
    assert set(readiness.OUT_OF_ORDER_CIS) <= set(keys)
    assert readiness.REQUIRED_CIS_KEYS[len(keys):] == gap
    assert all(k > readiness.REQUIRED_CIS_SCHEMA for k in gap) and list(gap) == sorted(set(gap))


# AA-1035 (S267 review): CIS 336 (AA-1024 D8) is time-gated and not applied on Dev. Raising the strict prefix past it
# would answer 503 schema_behind until it is; the R2 keys 362-364 are required above the gap instead.
# AA-1042 (S268): 370 (route control) is required above the gap too; 365-369 are not needed by this code.
# AA-1036 R3 (S268): 374 (add-on requests) too. AA-1035 close-out (S269): 375 (the reviewer alias removed) too.
# AA-1035 A10d (S270): 378 (the Dev-only E2E customer_success address) too.
# AA-1055 PR 2 (S274): 393 (place_keyword, the SERP tier of search_demand, the research log tier) too.
# AA-1039 follow-up (S276): 395 (tour_retirement_policy.auto_draft, the agency opt-in to automatic drafts) too.
# AA-1080 (S277): 397 (the tenant pool INSERT on tenant_brand_rules.custom_validators, the signature words) too.
# AA-1049 (S277): 398 (published_tours.master_status_reason 'policy_hold') too.
# AA-1091 (S278): 402 (silver_aa_internal.tour_reference_price, the Reference prices routes) too.
# AA-1093 L2 (S279): 408 / 409 (the tenant erasure saga tables and the feed event type) too.
DEV_KEYS_WITHOUT_336 = [k for k in range(readiness.LEGACY_CIS_BASELINE + 1, 410) if k != 336]


def test_the_time_gated_336_is_not_required_while_the_r2_keys_are():
    assert 336 not in readiness.REQUIRED_CIS_KEYS
    assert readiness.REQUIRED_CIS_SCHEMA < 336
    assert {362, 363, 364} <= set(readiness.REQUIRED_CIS_ABOVE_GAP)


@pytest.mark.asyncio
async def test_dev_without_336_is_ready_once_the_r2_keys_are_registered():
    status, body = await readiness.readiness(_keys_pool(397, DEV_KEYS_WITHOUT_336), now=0.0)
    assert status == 200 and body["status"] == "ok" and "missing_schema_versions" not in body


@pytest.mark.asyncio
async def test_dev_without_393_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 393]
    status, body = await readiness.readiness(_keys_pool(392, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["393"]


@pytest.mark.asyncio
async def test_dev_without_395_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 395]
    status, body = await readiness.readiness(_keys_pool(394, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["395"]


@pytest.mark.asyncio
async def test_dev_without_408_409_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k not in (408, 409)]
    status, body = await readiness.readiness(_keys_pool(407, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["408", "409"]


@pytest.mark.asyncio
async def test_dev_without_402_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 402]
    status, body = await readiness.readiness(_keys_pool(400, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["402"]


@pytest.mark.asyncio
async def test_dev_without_398_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 398]
    status, body = await readiness.readiness(_keys_pool(397, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["398"]


@pytest.mark.asyncio
async def test_dev_without_397_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 397]
    status, body = await readiness.readiness(_keys_pool(396, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["397"]


@pytest.mark.asyncio
async def test_dev_without_378_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 378]
    status, body = await readiness.readiness(_keys_pool(377, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["378"]


@pytest.mark.asyncio
async def test_dev_without_374_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 374]
    status, body = await readiness.readiness(_keys_pool(373, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["374"]


@pytest.mark.asyncio
async def test_dev_without_375_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 375]
    status, body = await readiness.readiness(_keys_pool(374, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["375"]


@pytest.mark.asyncio
async def test_dev_without_370_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 370]
    status, body = await readiness.readiness(_keys_pool(369, keys), now=0.0)
    assert status == 503 and body["missing_schema_versions"] == ["370"]


@pytest.mark.asyncio
async def test_a_missing_key_above_the_gap_is_schema_behind():
    keys = [k for k in DEV_KEYS_WITHOUT_336 if k != 364]
    status, body = await readiness.readiness(_keys_pool(370, keys), now=0.0)
    assert status == 503 and body["status"] == "schema_behind"
    assert body["missing_schema_versions"] == ["364"]
