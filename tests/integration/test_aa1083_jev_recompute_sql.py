"""AA-1083 — the SQL of a recompute's Jev side, on real Postgres (services/recompute/jev_scope.py and
atom_ranking.previous_demand):

* the cap read (no row = the 0.50 USD hard default; a row's per_run_usd / hard_stop);
* the unit cost per stage (answered Jev calls of the window only) and a job's real Jev spend (its rows only, refused
  asks counted apart);
* the held bell (one shared.notifications row, written as cis_app);
* release: only a recompute held by the Jev cap goes back to the queue, once, with who released it and the estimate;
* a released queued job is never folded into by a later platform enqueue;
* previous_demand: the demand of the current, ranked Score row per Segment of one market.

Each test runs in one transaction that is rolled back. Places, keywords and tour names are invented.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X1_CIS_SCHEMAS  # noqa: E402

from services.acp_contract import atom_ranking as ar  # noqa: E402
from services.jobs import recompute_job as rc  # noqa: E402
from services.recompute import jev_scope  # noqa: E402
from services.recompute.errors import JEV_CAP_HELD  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("DELETE FROM shared.job WHERE kind = 'recompute'")
        yield c
    finally:
        await tr.rollback()
        await c.close()


@asynccontextmanager
async def _as_cis_app(conn):
    """The worker's pool on Dev is cis_app (grants of this transaction only; the replay skips the Dev grant
    migration)."""
    for schema in X1_CIS_SCHEMAS:
        if await conn.fetchval("SELECT to_regnamespace($1) IS NOT NULL AS ok", schema):
            await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
            await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO cis_app")
    await conn.execute("GRANT INSERT ON shared.notifications TO cis_app")
    await conn.execute("GRANT USAGE ON SEQUENCE shared.notifications_id_seq TO cis_app")
    await conn.execute("GRANT UPDATE ON shared.job TO cis_app")
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE cis_app")
        yield
        await conn.execute("RESET ROLE")


async def _job(conn, *, kind="recompute", status="stopped_budget", error=None, payload=None, result=None):
    return await conn.fetchval(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, error, result, finished_at) "
        "VALUES ($1, $2::jsonb, $3, 1, 2, $4, $5::jsonb, now()) RETURNING id::text AS id",
        kind, json.dumps(payload or {"scope": "platform", "reason": "a3_atomize:t"}), status, error,
        json.dumps(result) if result is not None else None)


async def _call(conn, stage, cost, *, provider="typesafe", job_id=None, status="ok", days_ago=0):
    await conn.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, status, job_id, "
        "created_at) VALUES ($1, 'decide', 'zz-jev', $2, '{}'::jsonb, $3, $4, $5::uuid, "
        "now() - make_interval(days => $6))",
        stage, cost, provider, status, job_id, days_ago)


HELD_ERROR = f"{JEV_CAP_HELD}: Jev estimate $0.64 (40000 new questions) is above the $0.50 cap per recompute run."
HELD_RESULT = {"held": True, "jev": {"estimate": {"usd": 0.64, "new_calls": 40000}, "cap_usd": 0.5}}


@pytest.mark.asyncio
async def test_the_cap_defaults_to_a_hard_half_dollar_and_reads_its_row(conn):
    pool = _OneConnPool(conn)
    await conn.execute("DELETE FROM shared.spend_budget WHERE provider = 'jev' AND scope = 'job:recompute'")
    assert await jev_scope.read_cap(pool) == (0.5, True)
    await conn.execute("INSERT INTO shared.spend_budget (provider, scope, per_run_usd, hard_stop, alert_pct, "
                       "updated_at) VALUES ('jev', 'job:recompute', 1.25, false, 80, now())")
    assert await jev_scope.read_cap(pool) == (1.25, False)


@pytest.mark.asyncio
async def test_unit_cost_and_job_spend(conn):
    pool = _OneConnPool(conn)
    await conn.execute("DELETE FROM shared.llm_call_log WHERE stage = ANY($1::text[])", list(jev_scope.STAGES))
    job = await _job(conn, status="succeeded")
    other = await _job(conn, status="succeeded")
    await _call(conn, "a3_demand", 0.00002, job_id=job)
    await _call(conn, "a3_demand", 0.00004, job_id=job)
    await _call(conn, "a3_demand", 0, job_id=job, status="refused")            # a refused ask: 0 USD
    await _call(conn, "a3_demand", 0.5, provider="bedrock", job_id=job)        # not Jev
    await _call(conn, "a3_demand", 0.9, days_ago=30)                           # outside the window
    await _call(conn, "a3_question_landing", 0.00001, job_id=other)
    units = await jev_scope.unit_costs(pool)
    assert round(units["a3_demand"], 8) == 0.00003 and units["a3_question_landing"] == 0.00001
    assert units["a3_segment_type"] == jev_scope.FALLBACK_UNIT_USD
    async with _as_cis_app(conn):
        spend = await jev_scope.job_jev_spend(pool, job)
    assert spend["calls"] == 2 and spend["refused"] == 1 and round(spend["usd"], 6) == 0.00006
    assert set(spend["by_stage"]) == {"a3_demand"}


@pytest.mark.asyncio
async def test_the_held_bell_is_one_admin_notification(conn):
    pool = _OneConnPool(conn)
    job = await _job(conn, error=HELD_ERROR, result=HELD_RESULT)
    async with _as_cis_app(conn):
        await jev_scope.ring_held_bell(pool, job, HELD_ERROR, HELD_RESULT["jev"])
    row = await conn.fetchrow("SELECT event_type, entity_type, target_roles, payload FROM shared.notifications "
                              "WHERE entity_id = $1", job)
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    assert row["event_type"] == jev_scope.HELD_EVENT and row["entity_type"] == "job"
    assert row["target_roles"] == ["admin"] and payload["estimate"]["usd"] == 0.64 and payload["cap_usd"] == 0.5


@pytest.mark.asyncio
async def test_release_requeues_only_a_recompute_the_jev_cap_held(conn):
    pool = _OneConnPool(conn)
    held = await _job(conn, error=HELD_ERROR, result=HELD_RESULT)
    tenant_stop = await _job(conn, error="tenant_ai_budget_approval: over the approval threshold")
    other_kind = await _job(conn, kind="segment_research", error=HELD_ERROR)
    async with _as_cis_app(conn):
        out = await jev_scope.release_held(pool, held, "admin:u-1")
        again = await jev_scope.release_held(pool, held, "admin:u-1")
        assert await jev_scope.release_held(pool, tenant_stop, "admin:u-1") is None
        assert await jev_scope.release_held(pool, other_kind, "admin:u-1") is None
    assert out["status"] == "queued" and out["jev_release"]["by"] == "admin:u-1"
    assert out["jev_release"]["estimate_usd"] == 0.64 and again is None
    row = await conn.fetchrow("SELECT status, attempt, error, payload FROM shared.job WHERE id = $1::uuid", held)
    payload = json.loads(row["payload"])
    assert row["status"] == "queued" and row["attempt"] == 0 and row["error"] is None
    assert payload["scope"] == "platform" and payload["jev_release"]["by"] == "admin:u-1"


@pytest.mark.asyncio
async def test_a_released_queued_job_is_never_folded_into(conn):
    import services.jobs.recompute_job  # noqa: F401  (registers the kind enqueue_recompute uses)
    pool = _OneConnPool(conn)
    released = await _job(conn, status="queued", payload={"scope": "platform", "reason": "r",
                                                          "segment_ids": ["seg-a"], "jev_release": {"by": "a"}})
    job_id, created = await rc.enqueue_recompute(pool, scope="platform", reason="a3_atomize:t2",
                                                 segment_ids=["seg-b"])
    assert created and job_id != released
    # the next enqueue folds into the new job, not the released one
    job2, created2 = await rc.enqueue_recompute(pool, scope="platform", reason="a3_atomize:t3",
                                                segment_ids=["seg-c"])
    assert (job2, created2) == (job_id, False)
    payload = json.loads(await conn.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", released))
    assert payload["segment_ids"] == ["seg-a"]


@pytest.mark.asyncio
async def test_previous_demand_reads_the_current_ranked_row_per_segment(conn):
    pool = _OneConnPool(conn)
    tours = []
    for label in ("one", "two"):
        tour_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
            "source_status, source_group_id, source_version) VALUES ($1, $2, $3, $4, 'Vietnam', '2 days', "
            "'active'::silver_aa_internal.source_status_enum, $5, 1)",
            tour_id, MASTER, BATCH_ID, f"aa1083 {label}", uuid.uuid4())
        tours.append(tour_id)
    for seg, place in (("aa1083-s1", "Velmora Falls"), ("aa1083-s2", "Ostrel Market"),
                       ("aa1083-s3", "Corvane Harbour")):
        await conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                           "VALUES ($1, $2, 'visit')", seg, place)
    ins = ("INSERT INTO acp_contract.atom_ranking (market, tour_id, segment_id, demand_volume, recurrence, questions, "
           "said, excluded_reason, version, superseded_at) VALUES ($1, $2, $3, $4, 1, 0, 0, $5, $6, $7)")
    await conn.execute(ins, "ZZ", tours[0], "aa1083-s1", 900, None, 3, None)
    await conn.execute(ins, "ZZ", tours[1], "aa1083-s1", 900, None, 3, None)
    await conn.execute(ins, "ZZ", tours[0], "aa1083-s1", 100, None, 2,
                       datetime(2026, 11, 1, tzinfo=timezone.utc))     # superseded
    await conn.execute(ins, "ZZ", tours[0], "aa1083-s2", None, None, 3, None)           # ranked, nothing claimed
    await conn.execute(ins, "ZZ", tours[0], "aa1083-s3", None, "transit", 3, None)      # excluded: no demand row
    await conn.execute(ins, "YY", tours[0], "aa1083-s3", 40, None, 3, None)             # another market
    got = await ar.previous_demand(pool, "ZZ")
    assert {k: v for k, v in got.items() if k.startswith("aa1083-")} == {"aa1083-s1": 900, "aa1083-s2": None}
