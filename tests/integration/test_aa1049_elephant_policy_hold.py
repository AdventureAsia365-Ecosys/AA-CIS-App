"""AA-1049 (tech lead decision S277, root ADR 0020 D8) — the elephant-riding policy hold on real Postgres (CIS 398).

A Master whose product offers elephant riding is not on sale until a person reviews it: the lifecycle service holds
it (master_status inactive, reason policy_hold, a pending Review Queue row marked as the policy review); Set Active
and a publish without a human approval are refused; the Review Queue approve (a review row 'approved' for the
version) lets the publish through and lifts the hold; _enqueue_review files a policy-only version 'pending' even when
the tour has a live Master; the one-time script pins and holds the same set.

conftest replays every migration (398 included). Each test runs in one transaction that is rolled back (the service's
own transactions are savepoints inside it). Invented names and places only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services import tour_lifecycle as tl  # noqa: E402
from services.quality import elephant_policy, policy_review  # noqa: E402
from services.tour_lifecycle import policy as lp  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
RIDE_SOURCE = "Day 1: Arrive in Quillmere. Day 2: Ride an elephant through the Brannoch forest, then lunch."
RIDE_PAGE = '["Morning elephant ride along the Brannoch river"]'
CLEAN_SOURCE = "Day 1: Arrive in Quillmere. Day 2: Walk the Brannoch forest trail, then lunch."


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
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _version(conn, tour_id, num, *, highlights="[]", status="approved"):
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_highlights, status) VALUES ($1, $2, $3, 'Quillmere Ridge', 'Three days in the hills', $4::jsonb, "
        "$5::content_status_enum) RETURNING id", uuid.UUID(tour_id), MASTER, num, highlights, status))


async def _tour(conn, *, source=RIDE_SOURCE, page_highlights=RIDE_PAGE, master="active"):
    tour_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries) VALUES ($1, $2, $3, 'Quillmere Ridge 3D', 'Thailand', '3 days', $4)",
        uuid.UUID(tour_id), MASTER, BATCH_ID, source)
    v1 = await _version(conn, tour_id, 1, highlights=page_highlights)
    if master:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, 'Quillmere Ridge', 'Three days', "
            "$4::jsonb, $5)", uuid.UUID(tour_id), uuid.UUID(v1), MASTER, page_highlights, master)
    return SimpleNamespace(tour=tour_id, v1=v1)


async def _master(conn, tour_id):
    return await conn.fetchrow("SELECT master_status::text AS s, master_status_reason AS r "
                               "FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid", tour_id)


async def _reviews(conn, tour_id):
    return await conn.fetch("SELECT id::text AS id, generated_content_id::text AS v, review_status::text AS st, "
                            "escalate_detail::text AS d, failure_summary AS fs FROM silver_aa_internal.review_queue "
                            "WHERE tour_id = $1::uuid ORDER BY created_at, id", tour_id)


# ── the hold ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_hold_switches_the_master_off_and_files_one_pending_policy_review(conn):
    t = await _tour(conn)
    out = await tl.policy_hold(conn, t.tour, actor="test")
    m = await _master(conn, t.tour)
    assert (m["s"], m["r"]) == ("inactive", "policy_hold")
    assert out.extra["held"] is True and ("master.highlights" in out.extra["policy_fields"])
    assert "source.itineraries" in out.extra["policy_fields"]
    rows = await _reviews(conn, t.tour)
    assert [(r["v"], r["st"]) for r in rows] == [(t.v1, "pending")]
    detail = json.loads(rows[0]["d"])
    assert policy_review.is_policy_detail(detail) and rows[0]["fs"].startswith(policy_review.SUMMARY_PREFIX)
    # evidence = codes only: never the matched words or any page text
    assert {(i["where"], i["field"]) for i in detail} == {("master", "highlights"), ("source", "itineraries")}
    assert all(i["source_span"] is None for i in detail) and "Brannoch" not in rows[0]["d"]
    # the served state is paused (Booking / TripPlanner read it), with a lifecycle event for the TripPlanner purge
    assert await tl.served_state(conn, out.source_id) == ("paused", "master_inactive")
    assert [e["event"] for e in out.events] == ["paused"]
    assert (t.tour, "inactive") in out.recompute
    assert await lp.held_master_version(conn, rows[0]["id"]) is True


@pytest.mark.asyncio
async def test_policy_hold_is_idempotent(conn):
    t = await _tour(conn)
    first = await tl.policy_hold(conn, t.tour, actor="test")
    again = await tl.policy_hold(conn, t.tour, actor="test")
    assert again.extra["held"] is False and again.extra["review_id"] == first.extra["review_id"]
    assert again.events == [] and len(await _reviews(conn, t.tour)) == 1


@pytest.mark.asyncio
async def test_policy_hold_refuses_a_tour_with_no_ride(conn):
    t = await _tour(conn, source=CLEAN_SOURCE, page_highlights='["Walk the river path"]')
    with pytest.raises(tl.LifecycleError) as e:
        await tl.policy_hold(conn, t.tour, actor="test")
    assert e.value.code == "no_policy_hit"
    assert (await _master(conn, t.tour))["s"] == "active"


@pytest.mark.asyncio
async def test_a_negated_ride_is_no_hit(conn):
    t = await _tour(conn, source="Day 2: an elephant sanctuary visit, no elephant riding.",
                    page_highlights='["Feed the elephants at the sanctuary"]')
    assert await lp.master_hits(conn, t.tour) == []


AGENCY = "7e1a0000-0000-4000-8000-0000000a9e01"


async def _t2_pending_row(conn, tour_id):
    """A tenant's T2 policy-hold review row of the tour (the shape t2_rewrite_job._save_policy_hold writes: the agency
    tenant, a tenant_tour_version_id, no generated_content_id). The column / tenant are added inside the test's
    rolled-back transaction when the replay lacks them."""
    await conn.execute("ALTER TABLE silver_aa_internal.review_queue "
                       "ADD COLUMN IF NOT EXISTS tenant_tour_version_id uuid")
    await conn.execute("ALTER TABLE silver_aa_internal.review_queue ALTER COLUMN generated_content_id DROP NOT NULL")
    await conn.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active)
        VALUES ($1::uuid, 'Vellmoor Travel', 'vellmoor-travel', 'internal', 7, 60, true) ON CONFLICT DO NOTHING
    """, AGENCY)
    detail = json.dumps([{"check_id": policy_review.CHECK_ID, "field": "highlights", "description": "x",
                          "source_span": None, "suggested_fix": None}])
    return await conn.fetchval("""
        INSERT INTO silver_aa_internal.review_queue (tour_id, tenant_id, tenant_tour_version_id, failure_summary,
                                                     escalate_detail)
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'POLICY_HOLD elephant_riding: review the tour', $4::jsonb)
        RETURNING id::text
    """, tour_id, AGENCY, str(uuid.uuid4()), detail)


@pytest.mark.asyncio
async def test_policy_hold_never_closes_a_tenants_t2_review_of_the_tour(conn):
    t = await _tour(conn)
    t2 = await _t2_pending_row(conn, t.tour)
    out = await tl.policy_hold(conn, t.tour, actor="test")
    rows = {r["id"]: r["st"] for r in await _reviews(conn, t.tour)}
    # the agency's T2 review stays pending; the S1 policy review is a new row (the T2 one is never reused)
    assert rows[t2] == "pending" and out.extra["review_id"] != t2 and rows[out.extra["review_id"]] == "pending"
    # an older pending S1 row of the tour is still replaced (AA-653), the T2 row is not
    v2 = await _version(conn, t.tour, 2, status="hitl")
    old = await conn.fetchval("INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, "
                              "failure_summary) VALUES ($1::uuid, $2::uuid, $3::uuid, 'LOW_SCORE') RETURNING id::text",
                              t.tour, v2, MASTER)
    await conn.execute("UPDATE silver_aa_internal.review_queue SET review_status = 'rejected' WHERE id = $1::uuid",
                       out.extra["review_id"])
    again = await lp.ensure_policy_review(conn, t.tour, t.v1, [{"where": "master", "field": "highlights"}])
    rows = {r["id"]: r["st"] for r in await _reviews(conn, t.tour)}
    assert rows[old] == "superseded" and rows[t2] == "pending" and rows[again] == "pending"


@pytest.mark.asyncio
async def test_policy_hold_refuses_a_version_a_person_approved_in_its_policy_review(conn):
    t = await _tour(conn)
    held = await tl.policy_hold(conn, t.tour, actor="test")
    await conn.execute("UPDATE silver_aa_internal.review_queue SET review_status = 'approved' WHERE id = $1::uuid",
                       held.extra["review_id"])
    await tl.publish(conn, _publish_data(t, t.v1))
    assert (await _master(conn, t.tour))["s"] == "active" and await lp.policy_approved(conn, t.v1) is True
    with pytest.raises(tl.LifecycleError) as e:
        await tl.policy_hold(conn, t.tour, actor="test")
    assert e.value.code == lp.POLICY_APPROVED
    assert (await _master(conn, t.tour))["s"] == "active"
    assert [r["st"] for r in await _reviews(conn, t.tour)] == ["approved"]


# ── the guard: Set Active, pause, publish ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_active_refuses_a_held_master_and_pause_keeps_the_reason(conn):
    t = await _tour(conn)
    await tl.policy_hold(conn, t.tour, actor="test")
    with pytest.raises(tl.LifecycleError) as e:
        await tl.activate_master(conn, t.tour, actor="test")
    assert e.value.status == 409 and e.value.code == lp.POLICY_REVIEW_REQUIRED
    await tl.pause_master(conn, t.tour, actor="test")
    assert (await _master(conn, t.tour))["r"] == "policy_hold"


@pytest.mark.asyncio
async def test_set_active_refuses_a_staff_paused_master_that_offers_the_ride(conn):
    t = await _tour(conn)
    await tl.pause_master(conn, t.tour, actor="test")
    with pytest.raises(tl.LifecycleError) as e:
        await tl.activate_master(conn, t.tour, actor="test")
    assert e.value.code == lp.POLICY_REVIEW_REQUIRED
    # a staff-paused Master with no ride comes back as before
    clean = await _tour(conn, source=CLEAN_SOURCE, page_highlights='["Walk the river path"]')
    await tl.pause_master(conn, clean.tour, actor="test")
    await tl.activate_master(conn, clean.tour, actor="test")
    assert (await _master(conn, clean.tour))["s"] == "active"


def _publish_data(t, version_id):
    return {"tour_id": t.tour, "generated_content_id": version_id, "aa_name": "Quillmere Ridge",
            "aa_highlights": RIDE_PAGE, "approved_by": "pipeline"}


@pytest.mark.asyncio
async def test_publish_refuses_a_ride_with_no_human_approval(conn):
    t = await _tour(conn, master=None)
    with pytest.raises(tl.LifecycleError) as e:
        await tl.publish(conn, _publish_data(t, t.v1))
    assert e.value.code == lp.POLICY_REVIEW_REQUIRED
    assert await conn.fetchval("SELECT count(*) FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid",
                               t.tour) == 0


@pytest.mark.asyncio
async def test_the_review_queue_approval_publishes_and_lifts_the_hold(conn):
    t = await _tour(conn)
    held = await tl.policy_hold(conn, t.tour, actor="test")
    # the Review Queue approve claims the row 'approved' before it publishes (v1_pipeline.approve_review)
    await conn.execute("UPDATE silver_aa_internal.review_queue SET review_status = 'approved' WHERE id = $1::uuid",
                       held.extra["review_id"])
    out = await tl.publish(conn, _publish_data(t, t.v1))
    m = await _master(conn, t.tour)
    assert (m["s"], m["r"]) == ("active", None)
    assert out.extra["policy_hold_lifted"] is True and (t.tour, "active") in out.recompute
    assert [e["event"] for e in out.events] == ["restored"]


@pytest.mark.asyncio
async def test_a_rejected_review_keeps_the_tour_held(conn):
    t = await _tour(conn)
    held = await tl.policy_hold(conn, t.tour, actor="test")
    await conn.execute("UPDATE silver_aa_internal.review_queue SET review_status = 'rejected' WHERE id = $1::uuid",
                       held.extra["review_id"])
    from services.export.handler import mark_tour_rejected
    await mark_tour_rejected(conn, t.tour)
    assert (await _master(conn, t.tour))["r"] == "policy_hold"
    with pytest.raises(tl.LifecycleError):
        await tl.publish(conn, _publish_data(t, t.v1))


@pytest.mark.asyncio
async def test_a_rewrite_with_no_ride_anywhere_publishes_and_lifts_the_hold(conn):
    t = await _tour(conn)
    await tl.policy_hold(conn, t.tour, actor="test")
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET src_itineraries = $2 WHERE tour_id = $1::uuid",
                       uuid.UUID(t.tour), CLEAN_SOURCE)
    v2 = await _version(conn, t.tour, 2, highlights='["Walk the river path"]')
    out = await tl.publish(conn, {**_publish_data(t, v2), "aa_highlights": '["Walk the river path"]'})
    assert (await _master(conn, t.tour))["s"] == "active" and out.extra["policy_hits"] == 0


# ── _enqueue_review (AA-653 keeps its rule for every other hold) ──────────────────────────────────────────────────

def _policy_only_result(version_text):
    return {"quality_score": 7.5, "brand_audit_status": "manual_check",
            "brand_audit_codes": [elephant_policy.POLICY_CODE],
            "generated": {"highlights": [version_text]}}


@pytest.mark.asyncio
async def test_enqueue_review_files_a_policy_only_version_pending_on_a_live_master(conn):
    from api.routers.admin_pipeline import _enqueue_review
    t = await _tour(conn)
    v2 = await _version(conn, t.tour, 2, highlights=RIDE_PAGE, status="hitl")
    await _enqueue_review(conn, t.tour, v2, _policy_only_result("Morning elephant ride by the river"))
    rows = await _reviews(conn, t.tour)
    assert [(r["v"], r["st"]) for r in rows] == [(v2, "pending")]
    assert policy_review.is_policy_detail(rows[0]["d"])
    # a later non-policy attempt on the live tour is dismissed (AA-653) and leaves the policy review pending
    v3 = await _version(conn, t.tour, 3, status="hitl")
    await _enqueue_review(conn, t.tour, v3, {"quality_score": 4.0, "failure_codes": ["LOW_SCORE"]})
    rows = {r["v"]: r["st"] for r in await _reviews(conn, t.tour)}
    assert rows == {v2: "pending", v3: "dismissed"}


@pytest.mark.asyncio
async def test_enqueue_review_keeps_aa653_for_another_manual_check(conn):
    from api.routers.admin_pipeline import _enqueue_review
    t = await _tour(conn, source=CLEAN_SOURCE, page_highlights='["Walk the river path"]')
    v2 = await _version(conn, t.tour, 2, status="hitl")
    result = {"quality_score": 7.5, "brand_audit_status": "manual_check",
              "brand_audit_codes": [elephant_policy.POLICY_CODE], "generated": {"highlights": ["A quiet walk"]}}
    await _enqueue_review(conn, t.tour, v2, result)
    rows = await _reviews(conn, t.tour)
    assert [(r["v"], r["st"]) for r in rows] == [(v2, "dismissed")] and rows[0]["d"] is None


# ── the one-time script ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_script_pins_the_same_set_before_and_after_the_hold(conn):
    from scripts.quality import hold_elephant_masters as h
    ride = await _tour(conn)
    clean = await _tour(conn, source=CLEAN_SOURCE, page_highlights='["Walk the river path"]')
    rows = await conn.fetch(h.candidates_sql(), MASTER)
    found = [f for f in h.scan_rows(rows) if f["tour_id"] in (ride.tour, clean.tour)]
    assert [(f["tour_id"], f["state"]) for f in found] == [(ride.tour, "active")]
    pin = h.pin_of(found)
    after = AsyncMock()
    done = await h.hold_all(_OneConnPool(conn), found, after_commit=after)
    assert done == {"held": 1} and after.await_count == 1
    assert (await _master(conn, ride.tour))["r"] == "policy_hold"
    rows = await conn.fetch(h.candidates_sql(), MASTER)
    again = [f for f in h.scan_rows(rows) if f["tour_id"] in (ride.tour, clean.tour)]
    assert [(f["tour_id"], f["state"]) for f in again] == [(ride.tour, "held")]
    assert h.pin_of(again) == pin
    # resumable: a second pass changes nothing
    assert await h.hold_all(_OneConnPool(conn), again, after_commit=after) == {"kept": 1}
    assert len(await _reviews(conn, ride.tour)) == 1


@pytest.mark.asyncio
async def test_a_rerun_after_a_reviewer_approved_never_holds_the_tour_again(conn):
    from scripts.quality import hold_elephant_masters as h
    ride = await _tour(conn)
    other = await _tour(conn)
    mine = (ride.tour, other.tour)
    rows = await conn.fetch(h.candidates_sql(), MASTER)
    found = [f for f in h.scan_rows(rows) if f["tour_id"] in mine]
    pin = h.pin_of(found)
    after = AsyncMock()
    assert await h.hold_all(_OneConnPool(conn), found, after_commit=after) == {"held": 2}
    # a reviewer approves the first tour: its publish lifts the hold, the ride stays in its text by design
    rid = await conn.fetchval("SELECT id::text FROM silver_aa_internal.review_queue WHERE tour_id = $1::uuid",
                              ride.tour)
    await conn.execute("UPDATE silver_aa_internal.review_queue SET review_status = 'approved' WHERE id = $1::uuid",
                       rid)
    await tl.publish(conn, _publish_data(ride, ride.v1))
    rows = await conn.fetch(h.candidates_sql(), MASTER)
    again = [f for f in h.scan_rows(rows) if f["tour_id"] in mine]
    assert sorted((f["tour_id"], f["state"]) for f in again) == sorted([(ride.tour, "approved"), (other.tour, "held")])
    # the set to hold moved, so an APPLY with the old pin stops; the approved tour is never held again
    assert h.pin_of(again) != pin and h.pin_of(again).startswith("1:")
    assert await h.hold_all(_OneConnPool(conn), again, after_commit=after) == {"kept": 1}
    assert (await _master(conn, ride.tour))["s"] == "active"
    assert [r["st"] for r in await _reviews(conn, ride.tour)] == ["approved"]
    # the post-APPLY check: no active Master with a hit and no approved policy review
    assert [f for f in again if f["state"] == "active"] == []


# ── the Review Queue read: filter, badge fields, facet ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_review_queue_filters_and_marks_the_policy_reviews(conn, monkeypatch):
    from api.routers import admin_pipeline
    monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda _s: None)
    held = await _tour(conn)
    await tl.policy_hold(conn, held.tour, actor="test")
    other = await _tour(conn, source=CLEAN_SOURCE, page_highlights='["Walk the river path"]', master=None)
    await conn.execute("INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, "
                       "failure_summary, score_overall, review_status) VALUES ($1::uuid, $2::uuid, $3::uuid, "
                       "'LOW_SCORE', 4.0, 'pending')", other.tour, other.v1, MASTER)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))
    mine = {held.tour, other.tour}

    every = await admin_pipeline.admin_review_queue(req, x_admin_secret="x", page_size=200)
    rows = {r["tour_id"]: r for r in every["data"] if str(r["tour_id"]) in mine}
    assert set(rows) == mine and rows[other.tour]["policy_review"] is None
    pr = rows[held.tour]["policy_review"]
    assert pr["kind"] == policy_review.FILTER_VALUE and pr["held"] is True and pr["approvable"] is True
    assert {(e["where"], e["field"]) for e in pr["evidence"]} == {("master", "highlights"), ("source", "itineraries")}
    assert every["facets"]["policy_reviews"] >= 1

    only = await admin_pipeline.admin_review_queue(req, x_admin_secret="x", page_size=200,
                                                   policy=policy_review.FILTER_VALUE)
    assert {str(r["tour_id"]) for r in only["data"]} & mine == {held.tour}
    assert all(r["policy_review"] for r in only["data"])
