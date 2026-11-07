"""AA-1024 G17 — the wave report's stop flags are `pending` while any job of the wave's tours is queued or running.

S272 review: the pending read compared t2_rewrite's `published_tour_id` (a published_tours.id) with raw tour ids, and
missed the kinds whose payload carries a `tour_ids` list (s1_seo_prefetch, s1_batch_ingest), which run before any
s1_rewrite of the wave: a report run in that window read every flag as final. The wave start (POST /admin/waves) no
longer recomputes the flags inside the request (about 16 s for 50 tours, 75 s for 250: past the API Gateway's 29 s);
it inserts without reading quality. Invented tours and names only; no DB (fake connection).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_waves
from services.waves import quality as wq

T0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)


def test_active_jobs_sql_reads_every_payload_shape_of_a_wave_tour():
    sql = " ".join(wq.ACTIVE_JOBS_SQL.split())
    assert "j.payload->>'tour_id' = ANY($1::text[])" in sql
    # a tours list (s1_seo_prefetch {"tour_ids": [...]}, s1_batch_ingest)
    assert "j.payload->'tour_ids' ?| $1::text[]" in sql and "jsonb_typeof(j.payload->'tour_ids') = 'array'" in sql
    # t2_rewrite stores published_tours.id: mapped through published_tours, never compared with a raw tour id
    assert "SELECT pt.id::text FROM gold_aa_internal.published_tours pt WHERE pt.tour_id::text = ANY" in sql
    assert "payload->>'published_tour_id' = ANY($1" not in sql
    assert "j.status IN ('queued', 'running')" in sql


@pytest.mark.asyncio
async def test_a_queued_prefetch_of_the_waves_tours_makes_every_flag_pending():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"kind": "s1_seo_prefetch", "status": "queued", "n": 1}])
    active = await wq.load_active_jobs(conn, ["t-2", "t-1", "t-1"])
    assert conn.fetch.call_args[0] == (wq.ACTIVE_JOBS_SQL, ["t-1", "t-2"])
    ev = wq.evaluate([], active)
    assert set(ev["stop_state"].values()) == {"pending"} and ev["exit_code"] == wq.EXIT_PENDING


@pytest.mark.asyncio
async def test_no_tours_reads_no_jobs():
    conn = AsyncMock()
    assert await wq.load_active_jobs(conn, []) == []
    conn.fetch.assert_not_called()


def test_flags_are_final_once_no_job_is_active():
    stops = {"stayed_v1_over_10pct": True, "not_clearly_better": False}
    assert wq.stop_state(stops, [{"kind": "s1_rewrite", "status": "running", "n": 0}]) == {
        "stayed_v1_over_10pct": "true", "not_clearly_better": "false"}
    assert wq.exit_code(wq.stop_state(stops, [])) == wq.EXIT_STOP
    assert wq.exit_code({"a": "false"}) == 0


@pytest.mark.asyncio
async def test_wave_start_does_not_recompute_quality_in_the_request():
    # the 409 gate was taken out (S272 review): a 504 on a slow read inserted the wave anyway and a retry made a second
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tour_id": "t-1"}])
    conn.fetchrow = AsyncMock(return_value={
        "id": "w2", "name": "Next", "country": None, "expected_tour_ids": ["t-1"], "status": "open", "notes": None,
        "created_by": "admin", "created_at": T0, "closed_at": None, "last_checked_at": None, "last_check": None})
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire = MagicMock(return_value=ctx)
    with patch.object(admin_waves, "verify_admin_secret"), \
            patch.object(wq, "collect", AsyncMock(side_effect=AssertionError("quality read in a request"))):
        out = await admin_waves.create_wave(admin_waves.WaveCreate(name="Next", tour_ids=["t-1"]), req, "s", None)
    assert out["id"] == "w2"
    assert not hasattr(wq, "previous_wave_gate")


def _conn(versions=(), active=(), country_tours=()):
    """A fake connection that answers each query of collect() by its SQL text."""
    calls = []

    async def fetch(sql, *args):
        calls.append((sql, args))
        if sql == wq.VERSIONS_SQL:
            return [dict(v) for v in versions]
        if sql == wq.ACTIVE_JOBS_SQL:
            return [dict(a) for a in active]
        if sql == wq.COUNTRY_TOURS_SQL:
            return [{"tour_id": t} for t in country_tours]
        return []
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fetch)
    return conn, calls


def _active_ids(calls):
    return [args[0] for sql, args in calls if sql == wq.ACTIVE_JOBS_SQL]


@pytest.mark.asyncio
async def test_collect_reads_jobs_of_requested_tours_that_have_no_version_yet():
    # S272 review pass 3: no A1 version in the window, 50 queued s1_rewrite jobs of the requested tours. Reading jobs
    # only for the window's tours gave active=[], not_clearly_better true, exit 3 (STOP) while the wave was running.
    conn, calls = _conn(active=[{"kind": "s1_rewrite", "status": "queued", "n": 50}])
    tours, active = await wq.collect(conn, T0, T0 + dt.timedelta(hours=2), ids=["t-2", "t-1"])
    assert tours == [] and _active_ids(calls) == [["t-1", "t-2"]]
    ev = wq.evaluate(tours, active)
    assert set(ev["stop_state"].values()) == {"pending"} and ev["exit_code"] == wq.EXIT_PENDING


@pytest.mark.asyncio
async def test_collect_with_a_named_set_and_nothing_run_yet_is_pending_not_stop():
    conn, _ = _conn()
    tours, active = await wq.collect(conn, T0, T0 + dt.timedelta(hours=2), ids=["t-1"])
    assert active and active[0]["kind"] == wq.NO_VERSION_KIND
    assert wq.evaluate(tours, active)["exit_code"] == wq.EXIT_PENDING


@pytest.mark.asyncio
async def test_collect_by_country_reads_the_countrys_active_tours():
    conn, calls = _conn(active=[{"kind": "s1_seo_prefetch", "status": "queued", "n": 1}], country_tours=["t-9", "t-8"])
    tours, active = await wq.collect(conn, T0, T0 + dt.timedelta(hours=2), country="Invented Land")
    assert (wq.COUNTRY_TOURS_SQL, ("Invented Land",)) in calls
    assert _active_ids(calls) == [["t-8", "t-9"]]
    assert wq.evaluate(tours, active)["exit_code"] == wq.EXIT_PENDING


@pytest.mark.asyncio
async def test_collect_partial_wave_reads_jobs_of_every_requested_tour():
    # 1 of 3 requested tours rewritten, the other two still queued: the flags on the one tour are not final
    row = {"tour_id": "t-1", "after_id": "v-1", "before_id": None, "country": "Invented Land"}
    conn, calls = _conn(versions=[row], active=[{"kind": "s1_rewrite", "status": "queued", "n": 2}])
    done = [{"tour_id": "t-1", "country": "Invented Land", "outcome": "stayed_v1", "master_status": None,
             "after": None, "before": None}]
    with patch.object(wq, "_compute_tours", return_value=done):
        tours, active = await wq.collect(conn, T0, T0 + dt.timedelta(hours=2), ids=["t-1", "t-2", "t-3"])
    assert _active_ids(calls) == [["t-1", "t-2", "t-3"]]
    assert wq.evaluate(tours, active)["exit_code"] == wq.EXIT_PENDING


@pytest.mark.asyncio
async def test_collect_window_only_with_no_jobs_stays_final():
    # no named set (window only) and nothing in it: no synthetic pending row, the flags read as they are
    conn, calls = _conn()
    tours, active = await wq.collect(conn, T0, T0 + dt.timedelta(hours=2))
    assert tours == [] and active == [] and _active_ids(calls) == []
