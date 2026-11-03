"""AA-1024 D2 — migration 352 and the model lifecycle sync against a real Postgres (migrations applied by conftest):
the five nullable columns, cis_app may write them, one live run of llm_model_lifecycle_sync (Bedrock faked) leaves no
routed model with a NULL status, a refusing account keeps an earlier answered status, the bell, and the digest read.
Invented model ids only."""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.jobs import llm_model_lifecycle_job as job  # noqa: E402
from services.reports import digest  # noqa: E402
from shared.llm_client import model_lifecycle as ml  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "352_llm_model_lifecycle.sql"
COLUMNS = {"lifecycle_status": "text", "eol_date": "date", "legacy_date": "date",
           "lifecycle_checked_at": "timestamp with time zone", "lifecycle_detail": "jsonb"}


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("UPDATE shared.llm_model_catalog SET lifecycle_status = NULL, eol_date = NULL, legacy_date = NULL, "
                    "lifecycle_checked_at = NULL, lifecycle_detail = NULL")
    await p.execute("DELETE FROM shared.notifications WHERE event_type = $1", job.BELL_EVENT)
    yield p
    await p.execute("UPDATE shared.llm_model_catalog SET lifecycle_status = NULL, eol_date = NULL, legacy_date = NULL, "
                    "lifecycle_checked_at = NULL, lifecycle_detail = NULL")
    await p.execute("DELETE FROM shared.notifications WHERE event_type = $1", job.BELL_EVENT)
    await p.close()


def _ctx(payload, pool):
    ctx = MagicMock()
    ctx.payload, ctx.job_id, ctx.pool = payload, "j-aa1024-d2", pool
    return ctx


async def _fake_listings(state, *, acc3_refuses=True, legacy_wire=None, eol=None):
    """acc2 lists every foundation model its deployments name (ACTIVE); acc3 refuses (its Bedrock hold)."""
    out = {}
    for key in state["connections"]:
        if key == "acc3" and acc3_refuses:
            out[key] = ml.Listing(key, ml.ACCESS_REFUSED, error_class="access_denied", error="AccessDeniedException")
            continue
        ids = {ml.foundation_model_id(d["wire_id"]) for d in state["deployments"] if d["connection_key"] == key}
        models = {}
        for fid in sorted(i for i in ids if i):
            status = "LEGACY" if fid == legacy_wire else "ACTIVE"
            lc = {"status": status}
            if fid == legacy_wire and eol:
                lc["endOfLifeTime"] = datetime.combine(eol, datetime.min.time(), tzinfo=timezone.utc)
            models[fid] = ml.summarize({"modelId": fid, "modelLifecycle": lc})
        out[key] = ml.Listing(key, ml.ACCESS_OK, models)
    return out


@pytest.mark.asyncio
async def test_migration_352_adds_five_nullable_columns_and_is_idempotent(pool):
    rows = await pool.fetch("SELECT column_name, data_type, is_nullable, column_default "
                            "FROM information_schema.columns WHERE table_schema = 'shared' "
                            "AND table_name = 'llm_model_catalog' AND column_name = ANY($1::text[])", list(COLUMNS))
    got = {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"]) for r in rows}
    assert got == {k: (v, "YES", None) for k, v in COLUMNS.items()}
    await pool.execute(MIGRATION.read_text())                                  # a second apply changes nothing
    assert await pool.fetchval("SELECT count(*)::int AS n FROM shared.schema_versions WHERE version = '352'") == 1


@pytest.mark.asyncio
async def test_cis_app_may_write_the_lifecycle_columns(pool):
    """The job runs as cis_app. On Dev cis_app holds table-level SELECT / INSERT / UPDATE / DELETE on the catalog
    (db-schema-reference.md), which covers columns added later: no column grant is needed. The CI database has neither
    that grant nor USAGE on schema shared, so the test gives both inside a transaction it rolls back."""
    async with pool.acquire() as conn:
        key = await conn.fetchval("SELECT model_key FROM shared.llm_model_catalog ORDER BY model_key LIMIT 1")
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
            await conn.execute("GRANT SELECT, UPDATE ON shared.llm_model_catalog TO cis_app")
            for col in COLUMNS:
                assert await conn.fetchval("SELECT has_column_privilege('cis_app', 'shared.llm_model_catalog', $1, "
                                           "'UPDATE') AS ok", col)
            await conn.execute("SET LOCAL ROLE cis_app")
            done = await conn.execute(job.UPDATE_SQL, key, "active", date(2027, 5, 1), None, json.dumps({"x": 1}))
        finally:
            await tr.rollback()
    assert done == "UPDATE 1"


@pytest.mark.asyncio
async def test_one_run_leaves_no_routed_model_without_a_status(pool, monkeypatch):
    async with pool.acquire() as conn:
        state = await job.read_state(conn)
    assert state["ready"] and {"acc2", "acc3"} <= set(state["connections"])
    listings = await _fake_listings(state)

    async def fake_list_all(connections, **kw):
        return listings
    monkeypatch.setattr(job, "list_all", fake_list_all)
    out = await job.run(_ctx({"trigger": "manual"}, pool))
    assert out["updated"] == len(state["catalog"]) and out["routed"] > 0
    assert out["routed_without_status"] == []
    async with pool.acquire() as conn:
        routed = ml.route_keys(state["routes"], state["aliases"])
        rows = await conn.fetch("SELECT model_key, lifecycle_status AS status, lifecycle_detail AS detail, "
                                "lifecycle_checked_at AS at FROM shared.llm_model_catalog "
                                "WHERE model_key = ANY($1::text[])", list(routed))
    in_catalog = {r["model_key"] for r in rows}
    assert all(r["status"] in ml.STATUSES and r["at"] is not None for r in rows)
    assert set(routed) - in_catalog == set(out["routed_without_status"])
    acc3_only = [r for r in rows if r["status"] == "unknown"]
    for r in acc3_only:                                        # no answer, no earlier status: unknown, never NULL
        assert json.loads(r["detail"])["connections"]["acc3"]["access"] == "refused"


@pytest.mark.asyncio
async def test_a_refusing_account_keeps_the_answered_status_of_the_night_before(pool, monkeypatch):
    async with pool.acquire() as conn:
        state = await job.read_state(conn)
    acc3_models = sorted({d["model_key"] for d in state["deployments"] if d["connection_key"] == "acc3"}
                         - {d["model_key"] for d in state["deployments"] if d["connection_key"] != "acc3"})
    if not acc3_models:
        pytest.skip("the seeded registry has no acc3-only model")
    answered = await _fake_listings(state, acc3_refuses=False)
    refused = await _fake_listings(state, acc3_refuses=True)
    for listings in (answered, refused):
        async def fake_list_all(connections, _l=listings, **kw):
            return _l
        monkeypatch.setattr(job, "list_all", fake_list_all)
        await job.run(_ctx({"trigger": "manual"}, pool))
    status = await pool.fetchval("SELECT lifecycle_status FROM shared.llm_model_catalog WHERE model_key = $1",
                                 acc3_models[0])
    assert status == "active"


@pytest.mark.asyncio
async def test_a_routed_model_near_its_end_of_life_rings_one_bell_and_shows_in_the_digest(pool, monkeypatch):
    async with pool.acquire() as conn:
        state = await job.read_state(conn)
    routed = ml.route_keys(state["routes"], state["aliases"])
    acc2_routed = sorted((d["model_key"], d["wire_id"]) for d in state["deployments"]
                         if d["connection_key"] == "acc2" and d["model_key"] in routed)
    if not acc2_routed:
        pytest.skip("the seeded registry routes no acc2 model")
    model_key, wire = acc2_routed[0]
    eol = datetime.now(timezone.utc).date() + timedelta(days=30)
    listings = await _fake_listings(state, legacy_wire=ml.foundation_model_id(wire), eol=eol)

    async def fake_list_all(connections, **kw):
        return listings
    monkeypatch.setattr(job, "list_all", fake_list_all)
    first = await job.run(_ctx({"trigger": "schedule"}, pool))
    second = await job.run(_ctx({"trigger": "schedule"}, pool))
    assert model_key in {w["model_key"] for w in first["warnings"]} and first["bells"] >= 1
    assert second["bells"] == 0                                                  # once per 7 days
    n = await pool.fetchval("SELECT count(*)::int AS n FROM shared.notifications WHERE event_type = $1 "
                            "AND entity_id = $2", job.BELL_EVENT, f"llm_model:{model_key}")
    assert n == 1
    async with pool.acquire() as conn:
        lc = await digest.load_lifecycle(conn, datetime.now(timezone.utc))
    assert lc["ready"] and any(w["model_key"] == model_key for w in lc["warnings"])
    assert any(m["model_key"] == model_key and m["status"] == "legacy" for m in lc["models"])
    assert any(model_key in line for line in digest.lifecycle_attention(lc))
