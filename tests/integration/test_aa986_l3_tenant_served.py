"""AA-986 L3 + L4 code (root ADR 0015 decision 5) against real Postgres: the tenant side of a tour's served state.

  - served_status / served_reason / discontinued_at / served_notice on my-versions, version detail, the pool, the
    marketplace, publish pending and the publish log, the same on the main pool and the tenant pool (aa_app_user);
    a supersede waiting for its new Master shows no notice (Q2), a staff pause shows "paused" (Q1);
  - 409 tour_discontinued / tour_paused (plain sentence, X-Error-Code) on rewrite, retry, T8 goal, T9 write and
    publish of a new post;
  - the Slate drops proposed Subjects of a retired tour and keeps a picked one with the fields; the T7 trips carry
    the served state;
  - a publish-log row of a live post about a retired tour is flagged (the tenant removes it; AA never does), and
    A4 refuses to mark it unpublished (that would hide the flag and the one-click removal while the post stays live);
  - GET /v1/notifications on the tenant pool shows the tenant its tour.discontinued row written by the real
    lifecycle service, never another tenant's, and the staff bell no longer reads or clears it;
  - GET /v1/trip/{id}: 410 for a retired tour, 301 to the current version for a superseded one, 404 for a paused one.

Same harness as test_aa859_t2_version_reads_pool.py: the "tenant pool" is the test connection with SET LOCAL ROLE
aa_app_user (CIS 272 grants v_tour_liveness, 274 part A puts shared.notifications under RLS); everything runs in one
transaction that is rolled back.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from api.routers import admin as admin_mod  # noqa: E402
from api.routers import admin_a4  # noqa: E402
from api.routers import v1_marketplace, v1_notifications, v1_publish, v1_tours, v1_trip_page  # noqa: E402
from services.acp_angle_gate import service as angle_service  # noqa: E402
from services.acp_content_writing import service as writing_service  # noqa: E402
from services.acp_planning.runway import compute_runway_map  # noqa: E402
from services.acp_planning.tenant_pool import fetch_tenant_trips_on  # noqa: E402
from services.acp_shared.slate import fetch_slate_on  # noqa: E402
from services.tour_lifecycle import retire_source  # noqa: E402
from shared.tour_liveness import TourNotServedError  # noqa: E402
from test_aa859_t2_version_reads_pool import (  # noqa: E402
    BATCH_ID, MASTER, TTV_LIVE_STANDIN_DDL, _OneConnPool, _Redis, _tenant,
)

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _tour(conn, name, *, group=None, source_status="active", master_status="active", reason=None,
                version=1):
    tour_id = uuid.uuid4()
    group = group or tour_id
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_group_id, source_status, source_version) VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', $5, $6, $7)",
        tour_id, MASTER, BATCH_ID, name, group, source_status, version)
    pt = await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, "
        "aa_highlights, master_status, master_status_reason) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, $5, $6) "
        "RETURNING id", tour_id, uuid.uuid4(), MASTER, name, master_status, reason)
    return str(tour_id), pt, str(group)


async def _version(conn, tid, pt_id, *, status="ai_generated", job_id=None):
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, job_id) VALUES ($1::uuid, $2, 1, '{}'::jsonb, $3, $4) RETURNING id",
        tid, pt_id, status, job_id)


async def _request_row(conn, tid, tour_id, status="pending_goal"):
    return await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status) "
        "VALUES ($1::uuid, 'aa986-l3', $2::uuid, 'blog', $3) RETURNING request_id", tid, tour_id, status)


async def _piece(conn, tid, req):
    opt = await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, formula_fit, best_final_style, "
        "chosen) VALUES ($1, 0, 'Angle', 'w', 'f', 's', true) RETURNING option_id", req)
    return await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, attempt_number, "
        "content_text, status, channel, gate_ledger, repair_log) VALUES ($1::uuid, $2, $3, 1, 'Body', 'approved', "
        "'blog', '[]'::jsonb, '[]'::jsonb) RETURNING piece_id", tid, req, opt)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        # content_piece columns from 124 (pgvector, absent in CI) the publish SQL reads (see test_aa859_wave3)
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid, ADD COLUMN IF NOT EXISTS channel text,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer, ADD COLUMN IF NOT EXISTS flags jsonb""")
        tag = uuid.uuid4().hex[:8]
        a, b = await _tenant(conn), await _tenant(conn)
        served, served_pt, _ = await _tour(conn, f"Served {tag}")
        gone, gone_pt, gone_key = await _tour(conn, f"Gone {tag}", source_status="trashed",
                                              master_status="inactive", reason="source_deleted")
        await conn.execute("UPDATE silver_aa_internal.raw_tours SET deleted_at = now() WHERE tour_id = $1::uuid", gone)
        paused, paused_pt, _ = await _tour(conn, f"Paused {tag}", master_status="inactive",
                                           reason="staff_deactivated")
        # a supersede waiting for its new Master (Q11: paused / superseded_pending; Q2: nothing shown)
        old, old_pt, grp = await _tour(conn, f"Updating {tag}", source_status="superseded", master_status="inactive",
                                       reason="superseded")
        new_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
            "source_group_id, source_status, source_version) VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', $5, "
            "'active', 2)", new_id, MASTER, BATCH_ID, f"Updating v2 {tag}", UUID(grp))
        versions = {k: await _version(conn, a, pt) for k, pt in
                    (("served", served_pt), ("gone", gone_pt), ("paused", paused_pt), ("updating", old_pt))}
        yield SimpleNamespace(conn=conn, a=a, b=b, tag=tag, served=served, gone=gone, gone_key=gone_key,
                              paused=paused, old=old, served_pt=served_pt, gone_pt=gone_pt, paused_pt=paused_pt,
                              versions=versions)
    finally:
        await tr.rollback()
        await conn.close()


def _by_version(rows, versions):
    rev = {v: k for k, v in versions.items()}
    return {rev[d["id"]]: d for d in rows if d["id"] in rev}


# ── fields on the reads ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False])
async def test_my_versions_carry_the_served_state(ctx, flag_on):
    out = await v1_tours.list_my_versions(_request(ctx.conn, flag_on), {"sub": ctx.a}, page=1, page_size=100,
                                          status=None, country=None)
    rows = _by_version(out["data"], ctx.versions)
    assert (rows["served"]["served_status"], rows["served"]["served_notice"]) == ("served", None)
    assert rows["served"]["discontinued_at"] is None
    assert (rows["gone"]["served_status"], rows["gone"]["served_reason"], rows["gone"]["served_notice"]) == (
        "retired", "source_deleted", "discontinued")
    assert rows["gone"]["discontinued_at"]
    assert (rows["paused"]["served_status"], rows["paused"]["served_notice"]) == ("paused", "paused")
    # Q2: a supersede is invisible to the tenant (the state is paused / superseded_pending, no badge)
    assert (rows["updating"]["served_reason"], rows["updating"]["served_notice"]) == ("superseded_pending", None)


@pytest.mark.asyncio
async def test_version_detail_and_pool(ctx):
    detail = await v1_tours.get_version(str(ctx.versions["gone"]), _request(ctx.conn, True), {"sub": ctx.a})
    assert detail["served_notice"] == "discontinued" and detail["served_status"] == "retired"
    pool = await v1_tours.browse_pool(_request(ctx.conn, True), {"sub": ctx.a}, page=1, page_size=50, country=None,
                                      search=ctx.tag, duration_min=None, duration_max=None, sort="newest")
    names = {d["aa_name"] for d in pool["data"]}
    assert names == {f"Served {ctx.tag}"}                      # retired / paused / superseded are not offered
    assert pool["data"][0]["served_status"] == "served"


@pytest.mark.asyncio
async def test_marketplace_keeps_the_row_without_runway(ctx):
    out = await v1_marketplace.get_marketplace(_request(ctx.conn, True), {"sub": ctx.a})
    by_name = {t["name"]: t for t in out["tours"]}
    gone = by_name[f"Gone {ctx.tag}"]
    assert gone["served_notice"] == "discontinued" and gone["runway_months"] is None
    assert by_name[f"Served {ctx.tag}"]["served_status"] == "served"


# ── 409 on new work ──────────────────────────────────────────────────────────────────────────────────────────────

def _assert_409(exc, code):
    assert exc.status_code == 409
    assert exc.detail.startswith(f"{code}: ")
    assert exc.headers == {"X-Error-Code": code}


@pytest.mark.asyncio
async def test_rewrite_and_retry_refused(ctx):
    body = v1_tours.RewriteRequest()
    with pytest.raises(HTTPException) as e:
        await v1_tours.trigger_rewrite(str(ctx.gone_pt), body, _request(ctx.conn, False), {"sub": ctx.a})
    _assert_409(e.value, "tour_discontinued")
    assert "no longer offers this tour" in e.value.detail
    with pytest.raises(HTTPException) as e:
        await v1_tours.trigger_rewrite(str(ctx.paused_pt), body, _request(ctx.conn, False), {"sub": ctx.a})
    _assert_409(e.value, "tour_paused")
    job = await ctx.conn.fetchval("INSERT INTO shared.job (kind, status) VALUES ('t2_rewrite', 'failed') RETURNING id")
    failed = await ctx.conn.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, job_id) VALUES ($1::uuid, $2, 2, '{}'::jsonb, 'failed', $3) RETURNING id",
        ctx.a, ctx.gone_pt, job)
    with pytest.raises(HTTPException) as e:
        await v1_tours.retry_rewrite(str(failed), _request(ctx.conn, False), {"sub": ctx.a})
    _assert_409(e.value, "tour_discontinued")
    assert await ctx.conn.fetchval("SELECT status FROM gold_aa_internal.tenant_tour_versions WHERE id = $1",
                                   failed) == "failed"


@pytest.mark.asyncio
async def test_goal_and_write_refused(ctx):
    pool = _OneConnPool(ctx.conn)
    req = await _request_row(ctx.conn, ctx.a, ctx.gone)
    with pytest.raises(TourNotServedError) as e:
        await angle_service.start_goal_generation(UUID(ctx.a), req, "promotion", pool)
    assert e.value.code == "tour_discontinued"
    assert await ctx.conn.fetchval("SELECT goal FROM acp_shared.angle_gate_request WHERE request_id = $1", req) is None
    approved = await _request_row(ctx.conn, ctx.a, ctx.paused, status="approved")
    with pytest.raises(TourNotServedError) as e:
        await writing_service.start_write(UUID(ctx.a), approved, pool)
    assert e.value.code == "tour_paused"


@pytest.mark.asyncio
async def test_publish_refused_and_live_post_flagged(ctx):
    req = await _request_row(ctx.conn, ctx.a, ctx.gone, status="approved")
    piece = await _piece(ctx.conn, ctx.a, req)
    await ctx.conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key) "
                           "VALUES ($1::uuid, 'wordpress', '{}'::jsonb, $2)", ctx.a, f"acp/cms/{ctx.a}")
    pending = await v1_publish.list_pending(_request(ctx.conn, True), {"sub": ctx.a})
    row = next(p for p in pending["data"] if p["piece_id"] == str(piece))
    assert row["served_notice"] == "discontinued"
    with pytest.raises(HTTPException) as e:
        await v1_publish.publish(piece, _request(ctx.conn, True), {"sub": ctx.a})
    _assert_409(e.value, "tour_discontinued")
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1", piece) == 0
    # a post published before the tour was retired: flagged, never removed by AA
    await ctx.conn.execute("INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, published_at) "
                           "VALUES ($1, $2::uuid, 'blog', 'published', now())", piece, ctx.a)
    log = await v1_publish.list_publish_log(_request(ctx.conn, True), {"sub": ctx.a}, limit=50)
    live = next(r for r in log["data"] if r["piece_id"] == str(piece))
    assert live["live_post_of_discontinued_tour"] is True and live["status"] == "published"
    assert "request_id" not in live


@pytest.mark.asyncio
async def test_a4_keeps_the_tenant_removal_of_a_retired_tour_post(ctx, monkeypatch):
    """Review round: A4 "Mark unpublished in AA" on a live post about a retired tour would hide the tenant's flag
    and its one-click Remove post (DELETE takes only 'published' rows) while the post stays live: refused (409).
    A post about a served tour can still be marked; the note says the tenant must then delete it on the channel."""
    monkeypatch.setattr(admin_mod, "ADMIN_SECRET", "aa986-a4")
    req = await _request_row(ctx.conn, ctx.a, ctx.gone, status="approved")
    piece = await _piece(ctx.conn, ctx.a, req)
    gone_post = await ctx.conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, published_at, external_id) "
        "VALUES ($1, $2::uuid, 'blog', 'published', now(), '101') RETURNING publish_id", piece, ctx.a)
    with pytest.raises(HTTPException) as e:
        await admin_a4.force_unpublish(gone_post, _request(ctx.conn, False), x_admin_secret="aa986-a4",
                                       x_admin_user_id=None)
    _assert_409(e.value, "tenant_removes_post")
    assert await ctx.conn.fetchval("SELECT status FROM acp_shared.publish_log WHERE publish_id = $1",
                                   gone_post) == "published"
    log = await v1_publish.list_publish_log(_request(ctx.conn, True), {"sub": ctx.a}, limit=50)
    flagged = next(r for r in log["data"] if r["publish_id"] == str(gone_post))
    assert flagged["live_post_of_discontinued_tour"] is True        # the tenant keeps the flag + Remove post

    # a paused tour or a served one: A4 marks AA's record and says the post stays live and leaves the portal
    for tour in (ctx.paused, ctx.served):
        other = await _piece(ctx.conn, ctx.a, await _request_row(ctx.conn, ctx.a, tour, status="approved"))
        post = await ctx.conn.fetchval(
            "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, published_at, external_id) "
            "VALUES ($1, $2::uuid, 'blog', 'published', now(), '102') RETURNING publish_id", other, ctx.a)
        out = await admin_a4.force_unpublish(post, _request(ctx.conn, False), x_admin_secret="aa986-a4",
                                             x_admin_user_id=None)
        assert out["status"] == "unpublished" and out["channel_post_removed"] is False
        assert "still live" in out["note"] and "delete it there directly" in out["note"]
        # what the note says: the tenant's one-click removal no longer takes the row
        with pytest.raises(HTTPException) as e:
            await v1_publish.unpublish(post, _request(ctx.conn, True), {"sub": ctx.a})
        assert e.value.status_code == 404


# ── Slate, T7 ────────────────────────────────────────────────────────────────────────────────────────────────────

async def _route_subject(conn, tid, tour_id, state, day=1):
    route = f"aa986-{uuid.uuid4().hex[:10]}"
    await conn.execute("INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day, "
                       "last_day, version) VALUES ($1, $2::uuid, 'Hanoi', '[]'::jsonb, $3, $3, 1)", route, tour_id,
                       day)
    return await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, route_id, channel, state, cleared_bar_reason) "
        "VALUES ($1::uuid, $2, 'blog', $3, '{}'::jsonb) RETURNING subject_id", tid, route, state)


@pytest.mark.asyncio
async def test_slate_drops_proposed_subjects_of_a_retired_tour(ctx):
    keep = await _route_subject(ctx.conn, ctx.a, ctx.served, "proposed")
    drop = await _route_subject(ctx.conn, ctx.a, ctx.gone, "proposed")
    picked = await _route_subject(ctx.conn, ctx.a, ctx.gone, "picked", day=2)
    await ctx.conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
    await ctx.conn.execute("SELECT set_config('app.tenant_id', $1, true)", ctx.a)
    try:
        slate = await fetch_slate_on(ctx.conn, UUID(ctx.a), ["blog"])
    finally:
        await ctx.conn.execute("RESET ROLE")
    subjects = {s["subject_id"]: s for s in slate["blog"]["subjects"]}
    assert str(keep) in subjects and str(drop) not in subjects
    assert subjects[str(picked)]["served_notice"] == "discontinued"
    assert subjects[str(keep)]["served_status"] == "served"


@pytest.mark.asyncio
async def test_t7_trips_carry_the_served_state_and_the_runway_skips_them(ctx):
    trips = {str(t.id): t for t in await fetch_tenant_trips_on(ctx.conn, UUID(ctx.a))}
    assert trips[ctx.served].served_state == "served" and trips[ctx.served].served
    assert trips[ctx.gone].served_state == "retired" and not trips[ctx.gone].served
    for t in trips.values():
        t.period = "Jan-Dec"
    runway = compute_runway_map(UUID(ctx.a), 2027, list(trips.values()), ["US"])
    assert any("no longer served" in u and f"Gone {ctx.tag}" in u for u in runway.unknowns)


# ── notifications: the tenant bell, not the staff bell ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_reads_its_discontinued_warning_from_the_real_service(ctx):
    tour, pt, _ = await _tour(ctx.conn, f"Retire me {ctx.tag}")
    await _version(ctx.conn, ctx.a, pt)
    out = await retire_source(ctx.conn, tour, actor="admin")
    assert ctx.a in out.notified_tenants
    mine = await v1_notifications.list_notifications(_request(ctx.conn, True), {"sub": ctx.a}, unread_only=False,
                                                     limit=30)
    note = next(n for n in mine["items"] if n["tour_id"] == tour)
    assert note["event_type"] == "tour.discontinued" and not note["is_read"] and mine["unread"] >= 1
    assert "no longer serves" in note["message"]
    other = await v1_notifications.list_notifications(_request(ctx.conn, True), {"sub": ctx.b}, unread_only=False,
                                                      limit=30)
    assert all(n["tour_id"] != tour for n in other["items"])
    # the staff bell neither shows nor clears it (admin scope)
    scope = admin_mod.notification_scope(None)
    staff_ids = await ctx.conn.fetch(f"SELECT id FROM shared.notifications WHERE {scope} AND entity_id = $1", tour)
    assert note["id"] not in {r["id"] for r in staff_ids}
    # tenant b cannot mark it read; tenant a can
    with pytest.raises(HTTPException) as e:
        await v1_notifications.mark_read(note["id"], _request(ctx.conn, True), {"sub": ctx.b})
    assert e.value.status_code == 404
    assert (await v1_notifications.mark_all_read(_request(ctx.conn, True), {"sub": ctx.a}))["cleared"] >= 1
    assert await ctx.conn.fetchval("SELECT is_read FROM shared.notifications WHERE id = $1", note["id"]) is True


# ── /v1/trip/{id}: 410 / 301 / 404 ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trip_page_answers_410_301_404(ctx):
    req = _request(ctx.conn, False)
    assert (await v1_trip_page.get_trip_page(ctx.served, req))["tour_id"] == ctx.served
    with pytest.raises(HTTPException) as e:
        await v1_trip_page.get_trip_page(ctx.gone, req)
    assert e.value.status_code == 410
    with pytest.raises(HTTPException) as e:
        await v1_trip_page.get_trip_page(ctx.paused, req)
    assert e.value.status_code == 404
    # the superseded version of a key that is served again by its new version: 301 to the new one
    grp_new = await ctx.conn.fetchval("SELECT tour_id::text FROM silver_aa_internal.raw_tours "
                                      "WHERE source_group_id = (SELECT source_group_id FROM silver_aa_internal."
                                      "raw_tours WHERE tour_id = $1::uuid) AND source_status = 'active'", ctx.old)
    await ctx.conn.execute(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, "
        "aa_highlights, master_status) VALUES ($1::uuid, $2, $3, 'New', 'S', '[]'::jsonb, 'active')",
        grp_new, uuid.uuid4(), MASTER)
    moved = await v1_trip_page.get_trip_page(ctx.old, req)
    assert moved.status_code == 301 and moved.headers["location"] == f"/v1/trip/{grp_new}"


def test_payload_json_roundtrip():
    """The notification payload arrives as a JSON string on the pool without a jsonb codec."""
    item = v1_notifications._item({"id": 1, "event_type": "tour.discontinued", "entity_type": "tour",
                                   "entity_id": "t", "payload": json.dumps({"tour_name": "X", "live_posts": 2}),
                                   "is_read": False, "created_at": None})
    assert item["live_posts"] == 2 and "2 published posts about it are still live" in item["message"]
