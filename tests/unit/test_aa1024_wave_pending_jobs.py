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
