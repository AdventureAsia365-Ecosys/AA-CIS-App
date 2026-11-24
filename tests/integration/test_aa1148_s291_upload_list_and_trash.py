"""AA-1148 follow-ups (S291) on real Postgres.

1. Upload > "Tours Ready for Rewrite" (GET /admin/tours-ready) lists uploaded sheet tours only: a hand-made tour
   (raw_tours.entry_origin 'manual', CIS 478) belongs to the content workspace and is not offered for Rewrite there.
2. Trashing a tour whose key was never served (a hand-made tour never published) queues no platform recompute and no
   place-graph work (it affects nothing served; on Dev one such job ran ~50 min and held a DB Migrate apply). Trashing
   a published tour keeps today's behaviour: the scoped platform recompute is queued.

Every test runs in one rolled-back transaction (the seed helpers of test_aa986_l2_tour_lifecycle). Invented names.
"""
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services import tour_lifecycle as tl  # noqa: E402


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    from api.routers import admin
    monkeypatch.setattr(admin, "ADMIN_SECRET", l2.SECRET)


@pytest.fixture
def spies(monkeypatch):
    """The real post-commit recompute (services.tour_lifecycle.effects) with its two queue writes spied on."""
    enqueue = AsyncMock(return_value=("00000000-0000-4000-8000-000000001148", True))
    places = AsyncMock()
    monkeypatch.setattr("services.jobs.recompute_job.enqueue_recompute", enqueue)
    monkeypatch.setattr("services.places.enqueue.after_liveness_change", places)
    monkeypatch.setattr("services.tour_lifecycle.effects.recheck_trip_pages", AsyncMock())
    monkeypatch.setattr("services.tour_lifecycle.effects.notify_tripplanner", AsyncMock(return_value=0))
    monkeypatch.setattr("services.tour_lifecycle.effects.fan_out_to_tenants", AsyncMock(return_value=0))
    return enqueue, places


async def _hand_made(conn, label, *, published=False):
    t = await l2._tour(conn, label, master="active" if published else None,
                       itinerary="Day 1 - Arrive at Vellmar Bay\nWalk the old quay.")
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET entry_origin = 'manual', "
                       "manual_facts = '{}'::jsonb, pipeline_status = 'ingested' WHERE tour_id = $1", uuid.UUID(t.tour))
    return t


async def _sheet(conn, label):
    t = await l2._tour(conn, label, master=None, itinerary="Day 1 - Arrive at Corvel Point\nA short walk.")
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET pipeline_status = 'ingested' WHERE tour_id = $1",
                       uuid.UUID(t.tour))
    return t


# ── 1. the Upload list ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tours_ready_lists_sheet_tours_but_not_hand_made_ones(conn):
    from api.routers import admin_pipeline
    sheet = await _sheet(conn, "s291-sheet")
    manual = await _hand_made(conn, "s291-manual")
    with patch.object(admin_pipeline, "verify_admin_secret", lambda s: None):
        out = await admin_pipeline.get_tours_ready(l2._req(conn), x_admin_secret=l2.SECRET)
    ids = {t["tour_id"] for t in out["tours"]}
    assert sheet.tour in ids
    assert manual.tour not in ids
    assert out["total"] == len(out["tours"])


# ── 2. trash and the recompute ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trashing_a_never_published_hand_made_tour_queues_no_recompute(conn, spies):
    from api.routers import admin
    enqueue, places = spies
    t = await _hand_made(conn, "s291-never")
    out = await admin.trash_source_tour(t.tour, l2._req(conn), x_admin_secret=l2.SECRET, x_admin_role="admin",
                                        x_admin_user_id="staff-1")
    assert out["source_status"] == "trashed"
    assert out["lifecycle"]["state_before"] is None and out["lifecycle"]["state_after"] is None
    enqueue.assert_not_awaited()
    places.assert_not_awaited()

    # the restore of that tour changes nothing served either
    out = await admin.restore_source_tour(t.tour, l2._req(conn), x_admin_secret=l2.SECRET, x_admin_user_id=None)
    enqueue.assert_not_awaited()
    places.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_service_outcome_of_a_never_served_key_has_nothing_to_recompute(conn):
    sheet = await _sheet(conn, "s291-raw")                       # an uploaded row never published: same rule
    out = await tl.retire_source(conn, sheet.tour, actor="admin")
    assert out.recompute == [] and out.before == (None, None) and out.after == (None, None)


@pytest.mark.asyncio
async def test_trashing_a_published_tour_still_queues_the_platform_recompute(conn, spies):
    from api.routers import admin
    enqueue, places = spies
    t = await _hand_made(conn, "s291-live", published=True)
    out = await admin.trash_source_tour(t.tour, l2._req(conn), x_admin_secret=l2.SECRET, x_admin_role="admin",
                                        x_admin_user_id="staff-1")
    assert out["lifecycle"]["state_before"] == "served" and out["lifecycle"]["state_after"] == "retired"
    assert enqueue.await_count >= 1
    assert all(c.kwargs["scope"] == "platform" for c in enqueue.await_args_list)
    reasons = {c.kwargs["reason"] for c in enqueue.await_args_list}
    assert f"master_status=trashed:{t.tour}" in reasons and f"master_status=inactive:{t.tour}" in reasons
    assert places.await_count >= 1
