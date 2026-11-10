"""AA-1057 on real Postgres.

GAP 1: a Segment Subject's request is planned on one representative member (atom + trip_id). Retiring that tour
while another member of the Segment is served changes nothing for the tenant: no 409 on write / goal / publish
(request_tours -> the served member's tour), no derivative (no bell, no notice, no mail), no tour_retired flag on its
live WordPress page, A4 reads the post as served, the AA-subject gate still sees the Subject. When the last served
member leaves, the fan-out of THAT event flags the page and mails the tenant; when another member comes back before
the notice period ends, the move to draft resolves the flag (subject_alive) instead of drafting the page.

GAP 2: the `retarget` recompute stage moves a used Route Subject off a superseded Route to the current Route of the
same span, re-points a request on a replaced atom to the live same-place atom, and re-seeds a Segment request whose
seed tour retired onto the member a pick chooses today; the one-off script's DRY RUN changes nothing.

Every test runs in one rolled-back transaction (the seed helpers of test_aa986_l2_tour_lifecycle). Invented names.
"""
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services import tour_lifecycle as tl  # noqa: E402
from services.acp_shared import retarget as rt  # noqa: E402
from services.acp_shared import subject_gate as sg  # noqa: E402
from services.tour_lifecycle import derivatives as dv  # noqa: E402
from services.tour_lifecycle import fanout  # noqa: E402
from shared import tour_liveness as tlv  # noqa: E402


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text, "
                        "ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid")
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", AsyncMock())
    monkeypatch.setattr("services.tour_lifecycle.effects.recheck_trip_pages", AsyncMock())
    monkeypatch.setattr("services.tour_lifecycle.effects.notify_tripplanner", AsyncMock(return_value=0))


# ── seeds ──────────────────────────────────────────────────────────────────────────────────────────────────────────

PLACE = "Lumen Lake"


async def _atom(conn, tour, place, *, day=1, deleted=False):
    atom = f"zz1057-{uuid.uuid4().hex[:10]}"
    await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence, "
                       "itinerary_day, deleted) VALUES ($1, $2, 'platform', 'A quiet walk', $3, 'x', $4, $5)",
                       atom, uuid.UUID(tour), place, day, deleted)
    return atom


async def _segment(conn, atoms, place=PLACE):
    seg = f"zz1057-seg-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                       "VALUES ($1, $2, 'walk')", seg, place)
    await conn.executemany("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias) "
                           "VALUES ($1, $2, false)", [(seg, a) for a in atoms])
    return seg


async def _adopt(conn, tenant, pt):
    """The tenant's own tour (tenant_tour_versions): the pick rule seeds only from members on these."""
    await conn.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, "
                       "version_number, rewritten_content, status) VALUES ($1, $2, 1, '{}'::jsonb, 'pending')",
                       tenant, uuid.UUID(pt))


async def _world(conn, label, *, channel="blog"):
    """Tours A (the representative) and B, each with a member of one Segment; a tenant with a used Segment Subject,
    its request on A's atom (trip A), a piece and a live WordPress page."""
    w = SimpleNamespace()
    w.a = await l2._tour(conn, f"{label}-a")
    w.b = await l2._tour(conn, f"{label}-b")
    w.atom_a = await _atom(conn, w.a.tour, PLACE)
    w.atom_b = await _atom(conn, w.b.tour, PLACE)
    w.seg = await _segment(conn, [w.atom_a, w.atom_b])
    w.tenant = await l2._tenant(conn, label)
    # B is the tenant's own tour (a re-seed may pick it); no T2 copy of A, so A's retirement has no derivative of
    # its own for this tenant (a T2 copy of A would rightly get the digest notice about A)
    await _adopt(conn, w.tenant, w.b.pt)
    w.subject = str(await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, segment_id, channel, state, cleared_bar_reason) "
        "VALUES ($1, $2, $3, 'used', '{}'::jsonb) RETURNING subject_id", uuid.UUID(w.tenant), w.seg, channel))
    w.request = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status, goal, subject_id) "
        "VALUES ($1, $2, $3, $4, 'approved', 'promotion', $5) RETURNING request_id",
        uuid.UUID(w.tenant), w.atom_a, uuid.UUID(w.a.tour), channel, uuid.UUID(w.subject)))
    w.piece = str(await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, channel) VALUES ($1, $2, 1, 'A page about the lake.', 'approved', $3) RETURNING piece_id",
        uuid.UUID(w.tenant), uuid.UUID(w.request), channel))
    w.post = str(await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, 'blog', 'published', '57', 'https://agency.example/lumen-lake', now()) "
        "RETURNING publish_id", uuid.UUID(w.piece), uuid.UUID(w.tenant)))
    return w


async def _event_key(conn, t, event):
    return await conn.fetchval("SELECT idempotency_key FROM gold_aa_internal.tour_lifecycle_event "
                               "WHERE source_id = $1 AND event = $2 ORDER BY id DESC LIMIT 1", t.group, event)


async def _flags(conn, publish_id):
    return await conn.fetch("SELECT flag_id::text AS flag_id, state, resolution, tour_id::text AS tour_id, detail "
                            "FROM acp_shared.content_compliance_flag WHERE kind = 'tour_retired' "
                            "AND publish_id = $1::uuid", uuid.UUID(publish_id))


async def _notices(conn, tenant):
    return await conn.fetch("SELECT event, delivery FROM acp_shared.tenant_tour_notice WHERE tenant_id = $1::uuid",
                            uuid.UUID(tenant))


# ── GAP 1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retiring_the_representative_while_another_member_is_served_changes_nothing(conn):
    w = await _world(conn, "g1-alive")
    pool = l2._OneConnPool(conn)
    await tl.trash_master(conn, w.a.tour)
    assert (await l2._state(conn, w.a))[0] == "retired"

    # no 409 on write / goal / publish: the request is about the served member's tour
    tours = await tlv.request_tours(conn, w.tenant, [w.request])
    assert tours == {w.request: w.b.tour}
    await tlv.ensure_served(conn, tours[w.request], "write new content")
    state = (await tlv.request_served_states(conn, w.tenant, [w.request]))[w.request]
    assert state.served and state.notice is None

    # no derivative of A for this tenant: no bell, no queued-job cancel, no notice
    assert w.tenant not in await dv.tenant_derivatives(conn, [w.a.tour])
    assert w.tenant not in await l2._notified(conn, "tour.discontinued", [w.a.tour])

    # the fan-out of A's retirement: no flag on the live page, no notice, no mail
    out = await fanout.fan_out(pool, await _event_key(conn, w.a, "retired"))
    assert out["status"] == "done" and out["live_posts"] == 0 and out["flags_created"] == 0
    assert await _flags(conn, w.post) == []
    assert await _notices(conn, w.tenant) == []
    assert out["immediate_notices"] == []

    # A4 reads the live post as served; the AA-subject gate still sees the Subject
    assert await conn.fetchval(tlv.LIVE_POST_TOUR_STATE_SQL, w.post) == "served"
    assert (await sg.load_subject(conn, w.request, w.tenant)).ok


@pytest.mark.asyncio
async def test_pausing_the_representative_keeps_the_honest_tour_paused_answer(conn):
    """A paused representative is not substituted (the retarget stage never re-seeds it): the write / goal gate
    answers 409 tour_paused, not a pass followed by atom_replaced at T8 / T9; the pause is a derivative of the
    request (its queued T8 / T9 jobs are cancelled), and A4 reads the post as paused."""
    w = await _world(conn, "g1-paused")
    await tl.pause_master(conn, w.a.tour)
    assert (await l2._state(conn, w.a))[0] == "paused"

    tours = await tlv.request_tours(conn, w.tenant, [w.request])
    assert tours == {w.request: w.a.tour}
    with pytest.raises(tlv.TourNotServedError) as err:
        await tlv.ensure_served(conn, tours[w.request], "write new content")
    assert err.value.code == tlv.TOUR_PAUSED
    state = (await tlv.request_served_states(conn, w.tenant, [w.request]))[w.request]
    assert state.state == "paused" and not state.served
    assert w.tenant in await dv.tenant_derivatives(conn, [w.a.tour])
    assert await conn.fetchval(tlv.LIVE_POST_TOUR_STATE_SQL, w.post) == "paused"
    # the AA-subject gate still sees the Subject (a Segment with a served member)
    assert (await sg.load_subject(conn, w.request, w.tenant)).ok


@pytest.mark.asyncio
async def test_a_request_without_a_segment_subject_still_follows_its_tour(conn):
    w = await _world(conn, "g1-plain")
    plain = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status) "
        "VALUES ($1, $2, $3, 'blog', 'approved') RETURNING request_id",
        uuid.UUID(w.tenant), w.atom_a, uuid.UUID(w.a.tour)))
    await tl.trash_master(conn, w.a.tour)
    tours = await tlv.request_tours(conn, w.tenant, [plain])
    assert tours == {plain: w.a.tour}
    with pytest.raises(tlv.TourNotServedError):
        await tlv.ensure_served(conn, tours[plain], "write new content")
    assert w.tenant in await dv.tenant_derivatives(conn, [w.a.tour])


@pytest.mark.asyncio
async def test_the_last_member_leaving_flags_and_mails_and_a_member_coming_back_stops_the_draft(conn):
    w = await _world(conn, "g1-last")
    await conn.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, auto_draft, updated_by) "
                       "VALUES ($1, 7, true, 'test')", w.tenant)   # S276: automatic drafts are the agency's opt-in
    pool = l2._OneConnPool(conn)
    await tl.trash_master(conn, w.a.tour)
    assert (await fanout.fan_out(pool, await _event_key(conn, w.a, "retired")))["flags_created"] == 0

    await tl.trash_master(conn, w.b.tour)                   # the last served member leaves
    assert w.tenant in await dv.tenant_derivatives(conn, [w.b.tour])
    with pytest.raises(tlv.TourNotServedError):
        await tlv.ensure_served(conn, (await tlv.request_tours(conn, w.tenant, [w.request]))[w.request], "publish")
    out = await fanout.fan_out(pool, await _event_key(conn, w.b, "retired"))
    assert out["flags_created"] == 1 and out["live_posts"] == 1 and len(out["immediate_notices"]) == 1
    flags = await _flags(conn, w.post)
    assert len(flags) == 1 and flags[0]["state"] == "open" and flags[0]["tour_id"] == w.b.tour
    assert [(n["event"], n["delivery"]) for n in await _notices(conn, w.tenant)] == [("retired", "immediate")]

    # A comes back before the notice period ends: the Subject is alive again, the page is not moved
    await tl.restore_master(conn, w.a.tour)
    await tl.activate_master(conn, w.a.tour)
    await conn.execute("UPDATE acp_shared.content_compliance_flag SET draft_due_at = $2 WHERE flag_id = $1::uuid",
                       uuid.UUID(flags[0]["flag_id"]), datetime.now(timezone.utc) - timedelta(minutes=1))

    async def _no_secret(_name):
        raise AssertionError("the page must not be touched")

    res = await fanout.move_to_draft(pool, flags[0]["flag_id"], read_secret=_no_secret, adapter_for=_no_secret)
    assert res == {"status": "resolved", "resolution": "restored", "subject_alive": True}
    row = (await _flags(conn, w.post))[0]
    assert row["state"] == "resolved" and row["resolution"] == "restored"
    assert await conn.fetchval("SELECT status FROM acp_shared.publish_log WHERE publish_id = $1::uuid",
                               uuid.UUID(w.post)) == "published"


@pytest.mark.asyncio
async def test_the_subject_gate_reads_a_merged_segment_on_its_live_id(conn):
    w = await _world(conn, "g1-merged")
    await tl.trash_master(conn, w.a.tour)
    old = f"zz1057-old-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                       "VALUES ($1, $2, 'walk')", old, PLACE)
    await conn.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                       "VALUES ($1, $2, now())", old, w.seg)
    await conn.execute("UPDATE acp_shared.subject SET segment_id = $2 WHERE subject_id = $1::uuid",
                       uuid.UUID(w.subject), old)
    assert (await sg.load_subject(conn, w.request, w.tenant)).ok
    assert (await tlv.request_tours(conn, w.tenant, [w.request]))[w.request] == w.b.tour


# ── GAP 2: the retarget stage ──────────────────────────────────────────────────────────────────────────────────────

async def _route(conn, route_id, tour, first, last, segs, *, superseded=False, version=1):
    await conn.execute(
        "INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day, last_day, "
        "version, superseded_at) VALUES ($1, $2, 'Lumen Loop', $3::jsonb, $4, $5, $6, "
        "CASE WHEN $7 THEN now() END)", route_id, uuid.UUID(tour), json.dumps(segs), first, last,
        version, superseded)


def _by(decisions, key, value):
    return next(d for d in decisions if d[key] == value)


@pytest.mark.asyncio
async def test_retarget_moves_subjects_and_requests_onto_live_ids(conn):
    pool = l2._OneConnPool(conn)
    tenant = await l2._tenant(conn, "g2")

    # a used Route Subject on a superseded Route; the current version has the same span
    t = await l2._tour(conn, "g2-route")
    base = f"zz1057-r-{uuid.uuid4().hex[:8]}"
    await _route(conn, base, t.tour, 1, 3, ["s1", "s2"], superseded=True)
    await _route(conn, f"{base}:v2", t.tour, 1, 3, ["s1", "s3"], version=2)
    subject = str(await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, route_id, channel, state, cleared_bar_reason) "
        "VALUES ($1, $2, 'blog', 'used', '{}'::jsonb) RETURNING subject_id", uuid.UUID(tenant), base))

    # a request on an atom a rewrite replaced; the live atom of the same place is on the same tour
    gone = await _atom(conn, t.tour, "Quill Gorge", day=2, deleted=True)
    new = await _atom(conn, t.tour, "Quill Gorge", day=2)
    replaced = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status) "
        "VALUES ($1, $2, $3, 'facebook', 'pending_choice') RETURNING request_id",
        uuid.UUID(tenant), gone, uuid.UUID(t.tour)))

    # a Segment request whose seed tour retired while another member is served: re-seeded like a pick today
    w = await _world(conn, "g2-seed", channel="facebook")
    await tl.trash_master(conn, w.a.tour)

    plan = await rt.build_plan(conn)
    assert _by(plan.subjects, "subject_id", subject)["outcome"] == "retarget"
    assert _by(plan.requests, "request_id", replaced)["outcome"] == "repoint"
    assert _by(plan.requests, "request_id", w.request)["outcome"] == "reseed"

    out = await rt.run_retarget(pool, "report")             # report: nothing written
    assert out["changed"] == {"subjects": 0, "requests": 0}
    assert await conn.fetchval("SELECT route_id FROM acp_shared.subject WHERE subject_id = $1::uuid",
                               uuid.UUID(subject)) == base

    out = await rt.run_retarget(pool, "apply")
    assert out["changed"]["subjects"] >= 1 and out["changed"]["requests"] >= 2
    # the restore trail of the write: the old route, the old atom, and for the re-seed the old trip and walk
    restore = {r["request_id"]: r for r in out["restore"]["requests"]}
    assert {"subject_id": subject, "route_id": base, "new_route_id": f"{base}:v2"} in out["restore"]["subjects"]
    assert restore[replaced]["atom_id"] == gone and "trip_id" not in restore[replaced]
    assert (restore[w.request]["atom_id"], restore[w.request]["trip_id"], restore[w.request]["route_segment_ids"],
            restore[w.request]["new_trip_id"]) == (w.atom_a, w.a.tour, None, w.b.tour)
    assert await conn.fetchval("SELECT route_id FROM acp_shared.subject WHERE subject_id = $1::uuid",
                               uuid.UUID(subject)) == f"{base}:v2"
    assert await conn.fetchval("SELECT atom_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid",
                               uuid.UUID(replaced)) == new
    row = await conn.fetchrow("SELECT atom_id, trip_id::text AS trip_id, route_segment_ids FROM "
                              "acp_shared.angle_gate_request WHERE request_id = $1::uuid", uuid.UUID(w.request))
    assert (row["atom_id"], row["trip_id"], row["route_segment_ids"]) == (w.atom_b, w.b.tour, None)

    again = await rt.build_plan(conn)                        # nothing left to move for these rows
    assert subject not in {d["subject_id"] for d in again.subjects}
    assert not {replaced, w.request} & {d["request_id"] for d in again.requests}


@pytest.mark.asyncio
async def test_retarget_leaves_a_taken_route_and_a_paused_seed_tour_alone(conn):
    tenant = await l2._tenant(conn, "g2-keep")
    t = await l2._tour(conn, "g2-keep")
    base = f"zz1057-k-{uuid.uuid4().hex[:8]}"
    await _route(conn, base, t.tour, 1, 2, ["s1"], superseded=True)
    await _route(conn, f"{base}:v2", t.tour, 1, 2, ["s1"], version=2)
    stale = str(await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, route_id, channel, state, cleared_bar_reason) "
        "VALUES ($1, $2, 'blog', 'used', '{}'::jsonb) RETURNING subject_id", uuid.UUID(tenant), base))
    await conn.execute("INSERT INTO acp_shared.subject (tenant_id, route_id, channel, state, cleared_bar_reason) "
                       "VALUES ($1, $2, 'blog', 'picked', '{}'::jsonb)", uuid.UUID(tenant), f"{base}:v2")

    w = await _world(conn, "g2-paused", channel="facebook")
    await tl.pause_master(conn, w.a.tour)

    plan = await rt.build_plan(conn)
    assert _by(plan.subjects, "subject_id", stale)["outcome"] == "taken"
    assert _by(plan.requests, "request_id", w.request)["outcome"] == "paused"
    await rt.run_retarget(l2._OneConnPool(conn), "apply")
    assert await conn.fetchval("SELECT route_id FROM acp_shared.subject WHERE subject_id = $1::uuid",
                               uuid.UUID(stale)) == base
    assert await conn.fetchval("SELECT atom_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid",
                               uuid.UUID(w.request)) == w.atom_a


@pytest.mark.asyncio
async def test_the_one_off_script_dry_run_writes_nothing_and_apply_matches_it(conn):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1057_retarget_once.py")
    spec = importlib.util.spec_from_file_location("aa1057_retarget_once", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    tenant = await l2._tenant(conn, "g2-script")
    t = await l2._tour(conn, "g2-script")
    gone = await _atom(conn, t.tour, "Amber Steps", day=3, deleted=True)
    await _atom(conn, t.tour, "Amber Steps", day=3)
    req = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, $2, 'facebook', 'approved') RETURNING request_id", uuid.UUID(tenant), gone))

    dry = await script.dry_run(conn)
    assert dry["mode"] == "dry_run" and dry["requests"]["repoint"] >= 1
    assert await conn.fetchval("SELECT atom_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid",
                               uuid.UUID(req)) == gone

    async def _same_conn():
        class _Keep:
            def __getattr__(self, name):
                return getattr(conn, name)

            async def close(self):
                return None
        return _Keep()

    s_pin, r_pin = dry["subjects"]["pin"], dry["requests"]["pin"]
    with pytest.raises(SystemExit):
        await script.apply(conn, dry["subjects"]["retarget"] + 1, dry["requests"]["repoint"], s_pin, r_pin, None,
                           reconnect=_same_conn)
    plan = await rt.build_plan(conn)
    n_s, n_r = len(plan.subject_changes), len(plan.request_changes)
    with pytest.raises(SystemExit, match="not the reviewed ones"):
        await script.apply(conn, n_s, n_r, s_pin, "0" * 16, None, reconnect=_same_conn)
    assert await conn.fetchval("SELECT atom_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid",
                               uuid.UUID(req)) == gone
    out = await script.apply(conn, n_s, n_r, s_pin, r_pin, None, reconnect=_same_conn)
    assert out["changed"] == {"subjects": n_s, "requests": n_r}
    assert out["after"]["requests"]["repoint"] == 0 and out["after"]["requests"]["reseed"] == 0
