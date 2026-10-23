"""AA-986 L2 — the tour lifecycle service (ADR 0015 decision 2) and CIS 272, on real Postgres.

Test matrix: every writer of memo section 2.1 (source trash, DELETE raw-tours, staging replace / restore / create
new, set-active, Master trash / restore / deactivate / activate, source restore, the publish insert) × every
derivative it must cascade to (Master status + reason, pending reviews, queued T2 / T8 / T9 jobs, the lifecycle
record, the tenant notifications for T2 copies / Slate subjects / angle requests / pieces / live posts, atoms in
v_active_tour_atoms, the derived state in v_tour_liveness).

conftest replays 272 like every file. Each test runs in one transaction that is rolled back (the service's own
transactions are savepoints inside it). gold_aa_internal.tenant_tour_versions does not exist in the CI DB (001 is
skipped): each test creates the live-shape stand-in inside its transaction (_ttv_live_standin), dropped by the
rollback.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from _ttv_live_standin import TTV_LIVE_STANDIN_DDL  # noqa: E402

from services import tour_lifecycle as tl  # noqa: E402
import services.jobs.t2_rewrite_job  # noqa: E402,F401  (registers the kinds whose on_terminal hooks the cascade runs)
import services.jobs.t8_angles_job  # noqa: E402,F401
import services.jobs.t9_write_job  # noqa: E402,F401

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
SECRET = "test-secret"


class _OneConnPool:
    """A pool over the test connection (acquire + the pool-level fetch helpers the job hooks use)."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


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


@pytest.fixture(autouse=True)
def _secret_and_no_recompute(monkeypatch):
    from api.routers import admin
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    rec = AsyncMock()
    monkeypatch.setattr(admin, "_recompute_after_status_change", rec)
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", rec)
    return rec


# ── seeds ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _tenant(conn, label):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"aa986 {label}", f"aa986-{label}-{tid.hex[:8]}")
    return str(tid)


async def _tour(conn, label, *, group=None, version=1, source_status="active", master="active", deleted=False,
                provider=None, itinerary=None):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, deleted_at, source_group_id, source_version, provider, src_itineraries) "
        "VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', $5::silver_aa_internal.source_status_enum, "
        "CASE WHEN $6 THEN now() END, $7, $8, $9, $10)",
        tour_id, MASTER, BATCH_ID, f"aa986 {label}", source_status, deleted, group or uuid.uuid4(), version,
        provider, itinerary)
    pt_id = None
    if master:
        pt_id = await conn.fetchval(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, $5) RETURNING id",
            tour_id, uuid.uuid4(), MASTER, f"aa986 {label}", master)
    atom = f"aa986l2-{label}-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence) "
                       "VALUES ($1, $2, 'platform', 'Walk round the lake', 'Hoan Kiem', 'x')", atom, tour_id)
    return SimpleNamespace(tour=str(tour_id), pt=str(pt_id) if pt_id else None, atom=atom,
                           group=str(await conn.fetchval("SELECT source_group_id FROM silver_aa_internal.raw_tours "
                                                         "WHERE tour_id = $1", tour_id)))


async def _job(conn, kind, payload):
    return str(await conn.fetchval("INSERT INTO shared.job (kind, payload) VALUES ($1, $2::jsonb) RETURNING id",
                                   kind, json.dumps(payload)))


async def _derivatives(conn, t):
    """One tenant per derivative kind of tour t, plus one queued job of each tenant kind."""
    d = SimpleNamespace()
    d.t2 = await _tenant(conn, "t2")
    ttv = await conn.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, "
        "rewritten_content, status) VALUES ($1, $2, 1, '{}'::jsonb, 'pending') RETURNING id", d.t2, t.pt)
    d.ttv = str(ttv)
    d.job_t2 = await _job(conn, "t2_rewrite", {"version_id": d.ttv, "tenant_id": d.t2, "published_tour_id": t.pt})

    d.subject = await _tenant(conn, "subject")
    route_id = f"aa986-route-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day, "
                       "last_day) VALUES ($1, $2, 'Hanoi', '[]'::jsonb, 1, 2)", route_id, uuid.UUID(t.tour))
    await conn.execute("INSERT INTO acp_shared.subject (tenant_id, route_id, channel, cleared_bar_reason, state) "
                       "VALUES ($1, $2, 'blog', '{}'::jsonb, 'proposed')", d.subject, route_id)

    d.request = await _tenant(conn, "request")
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1, $2, 'blog', 'pending_goal') RETURNING request_id", d.request, t.atom)
    d.job_t8 = await _job(conn, "t8_angles", {"request_id": str(req), "tenant_id": d.request, "goal": "x"})

    d.post = await _tenant(conn, "post")
    req2 = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, "
                               "status) VALUES ($1, 'none', $2, 'blog', 'approved') RETURNING request_id",
                               d.post, uuid.UUID(t.tour))
    piece = await conn.fetchval("INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, "
                                "attempt_number, content_text, status) VALUES ($1, $2, 1, 'x', 'approved') "
                                "RETURNING piece_id", d.post, req2)
    await conn.execute("INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status) "
                       "VALUES ($1, $2, 'wordpress', 'published')", piece, d.post)
    piece2 = await conn.fetchval("INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, "
                                 "attempt_number, content_text, status) VALUES ($1, $2, 2, '', 'processing') "
                                 "RETURNING piece_id", d.post, req2)
    d.job_t9 = await _job(conn, "t9_write", {"piece_id": str(piece2), "request_id": str(req2), "context": {}})
    d.all = {d.t2, d.subject, d.request, d.post}
    return d


async def _review(conn, tour_id):
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, status) "
        "VALUES ($1, $2, 1, 'aa986', 'hitl'::content_status_enum) RETURNING id", uuid.UUID(tour_id), MASTER)
    return await conn.fetchval(
        "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status, "
        "score_overall) VALUES ($1, $2, $3, 'pending'::review_status_enum, 6.0) RETURNING id",
        uuid.UUID(tour_id), gc, MASTER)


async def _state(conn, t):
    return tuple(await conn.fetchrow("SELECT state, reason FROM gold_aa_internal.v_tour_liveness WHERE source_id = $1",
                                     t.group) or (None, None))


async def _master(conn, tour_id):
    return tuple(await conn.fetchrow("SELECT master_status::text, master_status_reason FROM "
                                     "gold_aa_internal.published_tours WHERE tour_id = $1", uuid.UUID(tour_id)))


async def _events(conn, t):
    return [(r["event"], r["reason"]) for r in await conn.fetch(
        "SELECT event, reason FROM gold_aa_internal.tour_lifecycle_event WHERE source_id = $1 ORDER BY id", t.group)]


async def _notified(conn, event_type, tour_ids):
    rows = await conn.fetch("SELECT tenant_id::text AS t FROM shared.notifications WHERE event_type = $1 "
                            "AND entity_id = ANY($2::text[])", event_type, tour_ids)
    return [r["t"] for r in rows]


async def _job_status(conn, job_id):
    return await conn.fetchval("SELECT status FROM shared.job WHERE id = $1::uuid", job_id)


async def _atoms_live(conn, t):
    return await conn.fetchval("SELECT count(*) FROM acp_contract.v_active_tour_atoms WHERE atom_id = $1", t.atom)


async def _active_masters_in_group(conn, group):
    return await conn.fetchval(
        "SELECT count(*) FROM gold_aa_internal.published_tours pt JOIN silver_aa_internal.raw_tours rt "
        "ON rt.tour_id = pt.tour_id WHERE rt.source_group_id = $1::uuid AND pt.master_status = 'active'", group)


# ── retire at S0/A0 × every derivative ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trash_route_retires_and_cascades_to_every_derivative(conn, _secret_and_no_recompute):
    from api.routers import admin
    t = await _tour(conn, "retire")
    d = await _derivatives(conn, t)
    rq = await _review(conn, t.tour)
    assert await _atoms_live(conn, t) == 1

    out = await admin.trash_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="admin",
                                        x_admin_user_id="staff-1")

    assert out["source_status"] == "trashed" and out["master_inactivated"] is True
    assert await _master(conn, t.tour) == ("inactive", "source_deleted")
    assert await conn.fetchval("SELECT review_status::text FROM silver_aa_internal.review_queue WHERE id = $1",
                               rq) == "dismissed"
    for job in (d.job_t2, d.job_t8, d.job_t9):
        assert await _job_status(conn, job) == "cancelled"
    assert await _events(conn, t) == [("retired", "source_deleted")]
    assert await _state(conn, t) == ("retired", "source_deleted")
    assert await _atoms_live(conn, t) == 0
    warned = await _notified(conn, "tour.discontinued", [t.tour])
    assert sorted(warned) == sorted(d.all) and MASTER not in warned
    payload = await conn.fetchval("SELECT payload FROM shared.notifications WHERE event_type = 'tour.discontinued' "
                                  "AND tenant_id = $1", uuid.UUID(d.post))
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["live_posts"] == 1 and payload["reason"] == "source_deleted"
    assert await _notified(conn, "tour.source.trashed", [t.tour]) == [MASTER]
    # post-commit: the on_terminal hooks of the cancelled jobs ran (the T2 version and the T9 piece are failed, not
    # left "writing" forever), and the recompute was told the new state
    assert await conn.fetchval("SELECT status FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid",
                               d.ttv) == "failed"
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.content_piece WHERE tenant_id = $1 "
                               "AND status = 'processing'", uuid.UUID(d.post)) == 0
    calls = [c.args[1:] for c in _secret_and_no_recompute.await_args_list]
    assert (t.tour, "inactive") in calls and (t.tour, "trashed") in calls
    # tenants' own copies are kept
    assert await conn.fetchval("SELECT count(*) FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid",
                               d.ttv) == 1
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE tenant_id = $1",
                               uuid.UUID(d.post)) == 1


@pytest.mark.asyncio
async def test_trash_a_published_tour_needs_master_admin_q8(conn):
    from api.routers import admin
    t = await _tour(conn, "q8")
    with pytest.raises(HTTPException) as e:
        await admin.trash_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="content",
                                      x_admin_user_id=None)
    assert e.value.status_code == 403 and "master.admin" in e.value.detail
    assert await _state(conn, t) == ("served", None)
    # a tour never published (S0/A0 only) stays content.write
    raw = await _tour(conn, "q8raw", master=None)
    out = await admin.trash_source_tour(raw.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="content",
                                        x_admin_user_id=None)
    assert out["source_status"] == "trashed" and await _events(conn, raw) == []


@pytest.mark.asyncio
async def test_delete_raw_tours_route_is_the_same_retire(conn):
    from api.routers import admin_pipeline
    with patch.object(admin_pipeline, "verify_admin_secret", lambda s: None):
        t = await _tour(conn, "delete")
        d = await _derivatives(conn, t)
        out = await admin_pipeline.soft_delete_raw_tour(t.tour, _req(conn), x_admin_secret=SECRET,
                                                        x_deleted_by="ops", x_admin_role="admin",
                                                        x_admin_user_id=None)
    assert out["source_status"] == "trashed"
    assert await conn.fetchval("SELECT deleted_by FROM silver_aa_internal.raw_tours WHERE tour_id = $1",
                               uuid.UUID(t.tour)) == "ops"
    assert await _master(conn, t.tour) == ("inactive", "source_deleted")
    assert sorted(await _notified(conn, "tour.discontinued", [t.tour])) == sorted(d.all)


@pytest.mark.asyncio
async def test_restore_reenables_only_cascade_masters_and_reinstates_warned_tenants(conn, _secret_and_no_recompute):
    from api.routers import admin
    t = await _tour(conn, "restore")
    d = await _derivatives(conn, t)
    await admin.trash_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="admin",
                                  x_admin_user_id=None)
    _secret_and_no_recompute.reset_mock()
    out = await admin.restore_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert out["master_reactivated"] is True
    assert await _master(conn, t.tour) == ("active", None)
    assert await _state(conn, t) == ("served", None)
    assert await _events(conn, t) == [("retired", "source_deleted"), ("restored", "restore")]
    assert sorted(await _notified(conn, "tour.reinstated", [t.tour])) == sorted(d.all)
    assert await _atoms_live(conn, t) == 1
    # the L1 fix kept: recompute as active (place extraction runs)
    assert [c.args[1:] for c in _secret_and_no_recompute.await_args_list] == [(t.tour, "active")]


@pytest.mark.asyncio
async def test_restore_keeps_a_master_staff_had_deactivated(conn, _secret_and_no_recompute):
    """The admin.py:1930-era bug: restore turned every inactive Master back on, including one staff had switched off
    on purpose. Now only reason source_deleted comes back."""
    from api.routers import admin
    t = await _tour(conn, "staffoff")
    await _derivatives(conn, t)
    await admin.deactivate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _master(conn, t.tour) == ("inactive", "staff_deactivated")
    await admin.trash_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="admin",
                                  x_admin_user_id=None)
    _secret_and_no_recompute.reset_mock()
    out = await admin.restore_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert out["master_reactivated"] is False
    assert await _master(conn, t.tour) == ("inactive", "staff_deactivated")
    assert await _state(conn, t) == ("paused", "master_inactive")
    assert await _notified(conn, "tour.reinstated", [t.tour]) == []
    assert [c.args[1:] for c in _secret_and_no_recompute.await_args_list] == [(t.tour, "inactive")]


# ── Master buttons ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_activate_on_a_dead_source_is_409_source_not_live(conn):
    from api.routers import admin
    t = await _tour(conn, "act409")
    await admin.trash_source_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_role="admin",
                                  x_admin_user_id=None)
    with pytest.raises(HTTPException) as e:
        await admin.activate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert e.value.status_code == 409 and e.value.detail.startswith("source_not_live: cannot activate this Master")
    assert e.value.headers["X-Error-Code"] == "source_not_live"
    assert "Restoring the source tour" in e.value.detail
    assert await _master(conn, t.tour) == ("inactive", "source_deleted")
    # PATCH /status active is the same action
    with pytest.raises(HTTPException) as e2:
        req = _req(conn)
        req.json = AsyncMock(return_value={"master_status": "active"})
        await admin.toggle_master_status(t.tour, req, x_admin_secret=SECRET, x_admin_user_id=None)
    assert e2.value.status_code == 409


@pytest.mark.asyncio
async def test_activate_a_superseded_version_is_409(conn):
    from api.routers import admin
    g = uuid.uuid4()
    old = await _tour(conn, "supold", group=g, source_status="superseded", master="inactive")
    await _tour(conn, "supnew", group=g, version=2)
    with pytest.raises(HTTPException) as e:
        await admin.activate_master_tour(old.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert e.value.status_code == 409 and "newer version" in e.value.detail
    assert await _active_masters_in_group(conn, str(g)) == 1


@pytest.mark.asyncio
async def test_pause_cancels_jobs_without_warning_tenants_q1(conn):
    from api.routers import admin
    t = await _tour(conn, "pause")
    d = await _derivatives(conn, t)
    await admin.deactivate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _state(conn, t) == ("paused", "master_inactive")
    assert await _events(conn, t) == [("paused", "master_inactive")]
    assert await _notified(conn, "tour.discontinued", [t.tour]) == []
    for job in (d.job_t2, d.job_t8, d.job_t9):
        assert await _job_status(conn, job) == "cancelled"
    await admin.activate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _state(conn, t) == ("served", None)
    assert await _events(conn, t) == [("paused", "master_inactive"), ("restored", "activate")]
    assert await _notified(conn, "tour.reinstated", [t.tour]) == []    # nobody was warned


@pytest.mark.asyncio
async def test_trash_restore_activate_master(conn):
    from api.routers import admin
    t = await _tour(conn, "mt")
    d = await _derivatives(conn, t)
    rq = await _review(conn, t.tour)
    await admin.trash_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _master(conn, t.tour) == ("trashed", "staff_trashed")
    assert await _state(conn, t) == ("retired", "master_trashed")
    assert sorted(await _notified(conn, "tour.discontinued", [t.tour])) == sorted(d.all)
    assert await conn.fetchval("SELECT review_status::text FROM silver_aa_internal.review_queue WHERE id = $1",
                               rq) == "dismissed"
    with pytest.raises(HTTPException) as e:
        await admin.activate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert e.value.status_code == 409 and e.value.detail.startswith("master_trashed")
    await admin.restore_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _state(conn, t) == ("paused", "master_inactive")
    await admin.activate_master_tour(t.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _state(conn, t) == ("served", None)
    assert await _events(conn, t) == [("retired", "master_trashed"), ("paused", "master_inactive"),
                                      ("restored", "activate")]
    assert sorted(await _notified(conn, "tour.reinstated", [t.tour])) == sorted(d.all)


# ── supersede (staging replace), publish, set-active ───────────────────────────────────────────────────────────────

async def _staging(conn, matched, name="aa986 incoming"):
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.upload_staging (batch_id, tenant_id, parsed_payload, matched_tour_id, "
        "matched_source_group_id, decision) VALUES ($1, $2, $3::jsonb, $4, $5, 'pending') RETURNING id",
        uuid.UUID(BATCH_ID), uuid.UUID(MASTER),
        json.dumps({"src_name": name, "country": "Vietnam", "duration": "3 days", "src_itineraries": "Day 1"}),
        uuid.UUID(matched.tour), uuid.UUID(matched.group)))


@pytest.mark.asyncio
async def test_staging_replace_supersedes_the_old_master_then_publish_restores(conn):
    from api.routers.admin_pipeline import DecideRequest, decide_staging
    t = await _tour(conn, "rep")
    d = await _derivatives(conn, t)
    rq = await _review(conn, t.tour)
    sid = await _staging(conn, t)
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        out = await decide_staging(sid, DecideRequest(decision="replace"), _req(conn), x_admin_secret=SECRET,
                                   x_admin_user_id=None)
    new = out["new_tour_id"]
    assert await conn.fetchval("SELECT source_status::text FROM silver_aa_internal.raw_tours WHERE tour_id = $1",
                               uuid.UUID(t.tour)) == "superseded"
    assert await _master(conn, t.tour) == ("inactive", "superseded")
    assert await _state(conn, t) == ("paused", "superseded_pending")
    assert await _events(conn, t) == [("superseded", "replaced")]
    assert await conn.fetchval("SELECT replaced_by_tour_id::text FROM gold_aa_internal.tour_lifecycle_event "
                               "WHERE source_id = $1", t.group) == new
    assert await conn.fetchval("SELECT review_status::text FROM silver_aa_internal.review_queue WHERE id = $1",
                               rq) == "dismissed"
    # Q2: a supersede is invisible to tenants: no notification, their jobs keep running
    assert await _notified(conn, "tour.discontinued", [t.tour, new]) == []
    assert await _job_status(conn, d.job_t2) == "queued"
    # the new version's Master is published (process_export's insert): served again, one active Master
    pub = await tl.publish(conn, {"tour_id": new, "generated_content_id": str(uuid.uuid4()), "tenant_id": MASTER,
                                  "aa_name": "aa986 v2", "aa_highlights": "[]", "seo_keywords_used": "[]",
                                  "og_tags": "{}"})
    assert pub.extra["catalog_id"]
    assert await _state(conn, t) == ("served", None)
    assert await _events(conn, t) == [("superseded", "replaced"), ("restored", "publish")]
    assert await _active_masters_in_group(conn, t.group) == 1


@pytest.mark.asyncio
async def test_publish_switches_off_a_leftover_active_master_in_the_key(conn):
    """Never two active Masters per key, even for data written before L2 (a superseded version left active)."""
    g = uuid.uuid4()
    old = await _tour(conn, "left1", group=g, source_status="superseded")        # Master still active (the leak)
    new = await _tour(conn, "left2", group=g, version=2, master=None)
    await tl.publish(conn, {"tour_id": new.tour, "generated_content_id": str(uuid.uuid4()), "tenant_id": MASTER,
                            "aa_name": "aa986 v2", "aa_highlights": "[]", "seo_keywords_used": "[]",
                            "og_tags": "{}"})
    assert await _master(conn, old.tour) == ("inactive", "superseded")
    assert await _active_masters_in_group(conn, str(g)) == 1


@pytest.mark.asyncio
async def test_set_active_refuses_a_trashed_version(conn):
    from api.routers.admin_pipeline import set_tour_active
    g = uuid.uuid4()
    trashed = await _tour(conn, "sat", group=g, source_status="trashed", deleted=True)
    live = await _tour(conn, "sal", group=g, version=2)
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        with pytest.raises(HTTPException) as e:
            await set_tour_active(trashed.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert e.value.status_code == 409 and e.value.detail.startswith("source_trashed")
    assert await conn.fetchval("SELECT source_status::text FROM silver_aa_internal.raw_tours WHERE tour_id = $1",
                               uuid.UUID(live.tour)) == "active"


@pytest.mark.asyncio
async def test_set_active_swaps_versions_and_their_masters(conn):
    from api.routers.admin_pipeline import set_tour_active
    g = uuid.uuid4()
    v1 = await _tour(conn, "sw1", group=g, source_status="superseded", master="inactive")
    await conn.execute("UPDATE gold_aa_internal.published_tours SET master_status_reason = 'superseded' "
                       "WHERE tour_id = $1", uuid.UUID(v1.tour))
    v2 = await _tour(conn, "sw2", group=g, version=2)
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        await set_tour_active(v1.tour, _req(conn), x_admin_secret=SECRET, x_admin_user_id=None)
    assert await _master(conn, v1.tour) == ("active", None)
    assert await _master(conn, v2.tour) == ("inactive", "superseded")
    assert await _active_masters_in_group(conn, str(g)) == 1
    assert await _state(conn, v1) == ("served", None)
    assert await _events(conn, v1) == [("superseded", "replaced"), ("restored", "set_active")]


# ── re-upload of a deleted tour (Q6) ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staging_on_a_deleted_tour_offers_restore_or_create_new(conn):
    from api.routers.admin_pipeline import DecideRequest, decide_staging, list_upload_staging
    t = await _tour(conn, "q6")
    await tl.retire_source(conn, t.tour)
    sid = await _staging(conn, t)
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        listed = await list_upload_staging(BATCH_ID, _req(conn), x_admin_secret=SECRET)
        item = next(i for i in listed["items"] if i["staging_id"] == sid)
        assert item["matched_deleted"] is True and item["matched_deleted_at"]
        assert item["existing"]["source_status"] == "trashed"
        for decision in ("replace", "update", "keep_both"):
            with pytest.raises(HTTPException) as e:
                await decide_staging(sid, DecideRequest(decision=decision), _req(conn), x_admin_secret=SECRET,
                                     x_admin_user_id=None)
            assert e.value.status_code == 409 and e.value.detail.startswith("source_deleted: this tour was deleted on")
        assert await _state(conn, t) == ("retired", "source_deleted")      # nothing revived
        out = await decide_staging(sid, DecideRequest(decision="restore"), _req(conn), x_admin_secret=SECRET,
                                   x_admin_user_id=None)
    assert out["restored_tour_id"] == t.tour
    assert await _state(conn, t) == ("served", None)
    assert await conn.fetchval("SELECT count(*) FROM silver_aa_internal.upload_staging WHERE id = $1::uuid", sid) == 0


@pytest.mark.asyncio
async def test_staging_create_new_leaves_the_deleted_tour_deleted(conn):
    from api.routers.admin_pipeline import DecideRequest, decide_staging
    t = await _tour(conn, "q6new")
    await tl.retire_source(conn, t.tour)
    sid = await _staging(conn, t)
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        out = await decide_staging(sid, DecideRequest(decision="create_new"), _req(conn), x_admin_secret=SECRET,
                                   x_admin_user_id=None)
    new_group = await conn.fetchval("SELECT source_group_id::text FROM silver_aa_internal.raw_tours "
                                    "WHERE tour_id = $1::uuid", out["new_tour_id"])
    assert new_group != t.group
    assert await _state(conn, t) == ("retired", "source_deleted")
    with patch("api.routers.admin_pipeline.verify_admin_secret", lambda s: None):
        with pytest.raises(HTTPException) as e:
            sid2 = await _staging(conn, SimpleNamespace(tour=out["new_tour_id"], group=new_group))
            await decide_staging(sid2, DecideRequest(decision="restore"), _req(conn), x_admin_secret=SECRET,
                                 x_admin_user_id=None)
    assert e.value.status_code == 409 and e.value.detail.startswith("not_deleted")


@pytest.mark.asyncio
async def test_near_duplicate_check_includes_deleted_tours(conn):
    from services.ingestion.near_duplicate import Checker
    itin = "Day 1 Hanoi old quarter walking tour lake temple. Day 2 Halong bay cruise kayaking caves sunset."
    t = await _tour(conn, "neardel", provider="Acme Travel", itinerary=itin)
    await tl.retire_source(conn, t.tour)
    row = {"src_name": "aa986 neardel", "provider": "ACME travel", "country": "Vietnam", "duration": "3 days",
           "src_itineraries": itin}
    near = await Checker(conn, MASTER).check(row)
    assert near.duplicate is not None and near.duplicate.tour_id == t.tour
    assert near.duplicate.deleted_at is not None and "deleted on" in near.message()
    # a live tour of the same provider wins over the deleted one
    live = await _tour(conn, "nearlive", provider="Acme Travel", itinerary=itin)
    near2 = await Checker(conn, MASTER).check({**row, "src_name": "aa986 nearlive"})
    assert near2.duplicate.tour_id == live.tour and near2.duplicate.deleted_at is None


# ── dry run (Q8) ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preview_counts_the_retire_and_changes_nothing(conn):
    from api.routers import admin
    t = await _tour(conn, "preview")
    d = await _derivatives(conn, t)
    await _review(conn, t.tour)
    out = await admin.lifecycle_preview(t.tour, _req(conn), action="retire_source", x_admin_secret=SECRET)
    assert out["allowed"] is True and out["state_after"] == "retired" and out["reason_after"] == "source_deleted"
    assert out["derivatives"] == {"tenants": 4, "t2_copies": 1, "subjects": 1, "angle_requests": 2, "pieces": 2,
                                  "live_posts": 1}
    assert out["queued_jobs"] == 3 and out["jobs_cancelled"] == 3 and out["notifies_tenants"] == 4
    assert out["reviews_dismissed"] == 1 and out["masters_switched_off"] == [t.tour]
    assert out["bookings"] is None and "AA-Booking" in out["bookings_note"]
    # rolled back: nothing changed
    assert await _state(conn, t) == ("served", None)
    assert await _events(conn, t) == []
    assert await _notified(conn, "tour.discontinued", [t.tour]) == []
    assert await _job_status(conn, d.job_t2) == "queued"
    refused = await admin.lifecycle_preview(t.tour, _req(conn), action="trash_master", x_admin_secret=SECRET)
    assert refused["allowed"] is True and refused["reason_after"] == "master_trashed"
    with pytest.raises(HTTPException) as e:
        await admin.lifecycle_preview(t.tour, _req(conn), action="nope", x_admin_secret=SECRET)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_preview_of_a_refused_action_says_why(conn):
    from api.routers import admin
    t = await _tour(conn, "prevref")
    await tl.trash_master(conn, t.tour)
    out = await admin.lifecycle_preview(t.tour, _req(conn), action="pause_master", x_admin_secret=SECRET)
    assert out["allowed"] is False and out["refused"].startswith("master_trashed")


@pytest.mark.asyncio
async def test_the_service_runs_with_the_runtime_role_grants(conn):
    """cis_app (the api + worker login since X1 S18) holds what the service writes and reads, the new objects
    included (CIS 272 grants)."""
    await conn.execute(migration_body(272))          # its grants, whatever earlier modules did to the roles
    t = await _tour(conn, "role")
    # The CI DB skips 244 (cis_app's schema-wide grants and parity policies): give cis_app, inside this rolled-back
    # transaction, the Dev privileges on the tables the service touches (db-schema-reference.md), never on the
    # CIS 272 objects, whose grants come from the file. RLS: the AA master tenant GUC stands in for the parity policy.
    for schema in ("gold_aa_internal", "silver_aa_internal", "shared", "acp_shared", "acp_contract"):
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON gold_aa_internal.published_tours, "
                       "silver_aa_internal.raw_tours, silver_aa_internal.review_queue, shared.job, "
                       "shared.notifications, gold_aa_internal.tenant_tour_versions, acp_shared.subject, "
                       "acp_shared.angle_gate_request, acp_shared.content_piece, acp_shared.publish_log, "
                       "acp_contract.route, acp_contract.tour_atoms, acp_contract.atom_segment_member TO cis_app")
    await conn.execute("GRANT SELECT ON acp_contract.v_active_tour_atoms TO cis_app")
    await conn.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared TO cis_app")
    await conn.execute(f"SET LOCAL app.tenant_id = '{MASTER}'")
    await conn.execute("SET LOCAL ROLE cis_app")
    try:
        out = await tl.trash_master(conn, t.tour, actor="admin")
    finally:
        await conn.execute("RESET ROLE")
    assert out.after == ("retired", "master_trashed") and out.events
