"""AA-1056 — tour on/off events and the recompute that must follow them, on real Postgres.

* retire_source / restore_source + run_after_commit queue a platform `recompute` (the restore folds into the one the
  retire queued, so both events are covered by one run);
* the lag measure (services/recompute/lifecycle.LAG_SQL, also the Data coverage source) counts from the first
  recompute that STARTED after an event, so the 05/11-like pair (an event 80 h before its next run) reads as late,
  not as "followed";
* the sweep queues one platform recompute for an event nothing covered, scoped to the tour's Segments, run as
  cis_app; a second sweep finds the queued job and does nothing;
* the platform recompute lock (handler.job_session_lock): two recompute jobs queued together never run at the same
  time, across threads and event loops, on real session advisory locks.

Each test runs in one transaction that is rolled back (the service's own transactions are savepoints inside it), so
`now()` is the transaction start: seeded events carry explicit times relative to it. Fixture names and places are
invented.
"""
import asyncio
import json
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X1_CIS_SCHEMAS  # noqa: E402
from _ttv_live_standin import TTV_LIVE_STANDIN_DDL  # noqa: E402

from services import tour_lifecycle as tl  # noqa: E402
from services.export import handler  # noqa: E402
from services.jobs import recompute_job as rc  # noqa: E402
from services.recompute import lifecycle  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"


class _OneConnPool:
    """A pool over the test connection (acquire + the pool-level helpers the effects and the sweep use)."""

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
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(TTV_LIVE_STANDIN_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(conn, label):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, source_group_id, source_version) "
        "VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', 'active'::silver_aa_internal.source_status_enum, $5, 1)",
        tour_id, MASTER, BATCH_ID, f"aa1056 {label}", uuid.uuid4())
    await conn.execute(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, "
        "aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, 'active')",
        tour_id, uuid.uuid4(), MASTER, f"aa1056 {label}")
    await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence) "
                       "VALUES ($1, $2, 'platform', 'Walk along the Lantern Quay', 'Lantern Quay', 'x')",
                       f"aa1056-{label}-{uuid.uuid4().hex[:8]}", tour_id)
    return str(tour_id)


async def _recompute_rows(conn):
    return await conn.fetch("SELECT status, created_by, payload FROM shared.job WHERE kind = 'recompute' "
                            "ORDER BY created_at, id")


async def _event(conn, event, hours_ago, *, tour_id=None, reason=None):
    reason = reason or {"retired": "source_deleted", "paused": "master_inactive", "restored": "restore",
                        "superseded": "replaced"}[event]
    await conn.execute(
        "INSERT INTO gold_aa_internal.tour_lifecycle_event (source_system, source_id, tour_id, event, reason, "
        "replaced_by_tour_id, action, actor, details, occurred_at, idempotency_key) "
        "VALUES ('cis', $1, $2, $3, $4, $5, 'aa1056_test', 'admin', '{}'::jsonb, "
        "now() - make_interval(secs => $6), $7)",
        str(uuid.uuid4()), tour_id or uuid.uuid4(), event, reason,
        uuid.uuid4() if event == "superseded" else None, float(hours_ago * 3600), f"aa1056:{uuid.uuid4().hex}")


async def _finished_recompute(conn, started_hours_ago, finished_hours_ago, scope="platform"):
    await conn.execute(
        "INSERT INTO shared.job (kind, payload, status, attempt, max_attempts, run_after, created_by, created_at, "
        "started_at, finished_at) VALUES ('recompute', $1::jsonb, 'succeeded', 1, 2, "
        "now() - make_interval(secs => $2), 'test', now() - make_interval(secs => $2), "
        "now() - make_interval(secs => $2), now() - make_interval(secs => $3))",
        json.dumps({"scope": scope, "reason": "test"}), float(started_hours_ago * 3600),
        float(finished_hours_ago * 3600))


@asynccontextmanager
async def _as_cis_app(conn):
    """The sampler's pool on Dev is cis_app: run the block as that role (grants of this transaction only; the replay
    skips the Dev grant migration, conftest)."""
    for schema in X1_CIS_SCHEMAS:
        if await conn.fetchval("SELECT to_regnamespace($1)", schema) is not None:
            await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
            await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO cis_app")
    await conn.execute("GRANT INSERT, UPDATE ON shared.job TO cis_app")
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE cis_app")
        yield
        await conn.execute("RESET ROLE")


# ── retire_source / restore_source -> recompute ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retire_then_restore_source_queue_one_platform_recompute(conn):
    import services.jobs.recompute_job  # noqa: F401  (registers the kind enqueue_recompute uses)
    tour = await _tour(conn, "retire-restore")
    pool = _OneConnPool(conn)

    out = await tl.retire_source(conn, tour, actor="admin")
    # the Master switched off by the cascade, then the source trashed: two status changes, one event
    assert [e["event"] for e in out.events] == ["retired"]
    assert out.recompute == [(tour, "inactive"), (tour, "trashed")]
    await tl.run_after_commit(pool, out)
    rows = await _recompute_rows(conn)
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    payload = json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else rows[0]["payload"]
    assert payload["scope"] == "platform" and payload["reason"] == f"master_status=inactive:{tour}"
    assert payload["coalesced"] == 1                          # the second status change folded into the first

    out = await tl.restore_source(conn, tour, actor="admin")
    assert [e["event"] for e in out.events] == ["restored"] and out.recompute == [(tour, "active")]
    await tl.run_after_commit(pool, out)
    rows = await _recompute_rows(conn)
    payload = json.loads(rows[0]["payload"]) if isinstance(rows[0]["payload"], str) else rows[0]["payload"]
    assert len(rows) == 1 and payload["coalesced"] == 2      # the restore folded into the queued run: one pass
    async with _as_cis_app(conn):
        lag = await lifecycle.lag_summary(conn)
    assert lag["events"] == 2 and lag["waiting"] == 2 and lag["waiting_over_limit"] == 0   # just happened


@pytest.mark.asyncio
async def test_a_lost_after_commit_step_is_caught_by_the_measure_and_the_sweep(conn):
    """retire_source without run_after_commit (a crash between commit and effects): nothing queues a recompute; the
    sweep does, once the event is older than its grace."""
    import services.jobs.recompute_job  # noqa: F401
    tour = await _tour(conn, "lost")
    out = await tl.retire_source(conn, tour, actor="admin")
    assert out.events and not await _recompute_rows(conn)
    pool = _OneConnPool(conn)
    async with _as_cis_app(conn):
        assert (await lifecycle.sweep(pool))["events"] == 0          # inside the grace: after-commit's turn
        swept = await lifecycle.sweep(pool, grace_s=-1)               # the event is past the grace
    assert swept["events"] == 1 and swept["tours"] == 1 and swept["created"] is True
    rows = await _recompute_rows(conn)
    assert len(rows) == 1 and rows[0]["created_by"] == lifecycle.SWEEP_CREATED_BY


# ── the lag measure and the sweep on seeded history ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lag_counts_from_the_first_recompute_started_after_each_event(conn):
    late_tour = uuid.uuid4()
    await _event(conn, "retired", 80, tour_id=late_tour)        # the 05/11-like pair: 78 h to the next run
    await _event(conn, "restored", 79, tour_id=late_tour)
    await _event(conn, "paused", 3)                              # 65 min to the same run
    await _finished_recompute(conn, started_hours_ago=2, finished_hours_ago=1.9)
    await _event(conn, "restored", 50 / 60)                      # nothing after it yet: waiting 50 min
    await _event(conn, "superseded", 1)                          # not an on/off event: not counted
    await _event(conn, "retired", 40 * 24)                       # outside the 30-day window

    async with _as_cis_app(conn):
        lag = await lifecycle.lag_summary(conn)
    assert lag["events"] == 4 and lag["followed"] == 3 and lag["waiting"] == 1 and lag["waiting_over_limit"] == 1
    assert lag["late"] == 3
    assert abs(lag["lag_max_s"] - 78.1 * 3600) < 5 and abs(lag["oldest_waiting_s"] - 3000) < 5
    row = await conn.fetchrow(lifecycle.LAG_SQL, *lifecycle.lag_args())
    assert row["lag_max_min"] == 4686                            # ceil(78.1 h in minutes), the page's number


@pytest.mark.asyncio
async def test_sweep_queues_one_run_for_the_uncovered_event_and_none_on_the_next_sweep(conn):
    import services.jobs.recompute_job  # noqa: F401
    covered = uuid.uuid4()
    await _event(conn, "retired", 3, tour_id=covered)
    await _finished_recompute(conn, started_hours_ago=2, finished_hours_ago=1.9)
    tour = await _tour(conn, "uncovered")
    segment = f"aa1056-seg-{uuid.uuid4().hex[:8]}"
    atom = await conn.fetchval("SELECT atom_id FROM acp_contract.tour_atoms WHERE tour_id = $1::uuid", tour)
    await conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action, "
                       "created_at) VALUES ($1, 'Lantern Quay', 'walk', now())", segment)
    await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias) "
                       "VALUES ($1, $2, false)", segment, atom)
    await _event(conn, "restored", 1, tour_id=tour)
    await _event(conn, "paused", 0.01)                           # 36 s ago: inside the grace

    pool = _OneConnPool(conn)
    async with _as_cis_app(conn):
        first = await lifecycle.sweep(pool)
        second = await lifecycle.sweep(pool)
    assert first["events"] == 1 and first["segments"] == 1 and first["created"] is True
    assert second == {"events": 0}
    queued = await conn.fetch("SELECT payload, created_by FROM shared.job WHERE kind = 'recompute' "
                              "AND status = 'queued'")
    payload = json.loads(queued[0]["payload"]) if isinstance(queued[0]["payload"], str) else queued[0]["payload"]
    assert len(queued) == 1 and payload["segment_ids"] == [segment] and payload["reason"] == "lifecycle_sweep:1"


# ── the platform recompute lock on real Postgres ───────────────────────────────────────────────────────────────────

def _ctx(job_id):
    job = queue.Job(id=job_id, kind="recompute", payload={"scope": "platform", "reason": "lifecycle_sweep:1"},
                    status="running", attempt=1, max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_two_recompute_jobs_never_run_at_the_same_time(monkeypatch):
    monkeypatch.setattr(handler, "_connect_tour_lock", lambda: asyncpg.connect(DSN))
    monkeypatch.setattr(rc, "LOCK_POLL_SECONDS", 0.05)
    inside, overlap, gate = [], [], threading.Lock()

    async def body(*a, **kw):
        with gate:
            inside.append(1)
            overlap.append(len(inside) > 1)
        await asyncio.sleep(0.4)
        with gate:
            inside.pop()
        return {"ranking": {}, "route": {}, "segment_gc": None}

    def _pool():
        p = MagicMock()
        p.close = AsyncMock()
        return p

    with patch("services.export.handler.recompute_rankings_and_routes", AsyncMock(side_effect=body)), \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _pool())):
        await asyncio.wait_for(asyncio.gather(rc.run(_ctx("job-a")), rc.run(_ctx("job-b"))), 20)
    assert overlap == [False, False]
    probe = await asyncpg.connect(DSN)
    try:
        held = await probe.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND classid = hashtext($1)::oid AND objid = hashtext($2)::oid AND objsubid = 2",
            rc.LOCK_CLASS, rc.LOCK_KEY)
    finally:
        await probe.close()
    assert held == 0
