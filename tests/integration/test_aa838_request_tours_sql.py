"""AA-838 (S291, C4.3 W48 finding 1) on real Postgres: the faster REQUEST_TOURS_SQL (shared/tour_liveness.py: the
representative tour once per request row, the key's state through a LATERAL one-key lookup of v_tour_liveness) returns
exactly the rows of the statement it replaced, frozen below as OLD_REQUEST_TOURS_SQL.

Fixture rows cover every branch: a served tour; a retired representative whose Segment still has a served member (the
AA-1057 Subject rule substitutes that member); a retired representative whose Segment has none left (kept); a paused
representative (kept, never substituted); grouped versions (a request on the superseded version of a served key, and on
the live version); a tour never served (no state); a request with an atom only (no trip); a request whose atom is gone
(no tour). Every test runs in one rolled-back transaction (the seed helpers of test_aa986_l2_tour_lifecycle and
test_aa1057_segment_liveness). Invented names and places.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)
import test_aa1057_segment_liveness as s1057  # noqa: E402  (fixture + seeds)

from services import tour_lifecycle as tl  # noqa: E402
from shared import tour_liveness as tlv  # noqa: E402

# The statement before S291, byte for byte in its logic (the fragments are the module's own, unchanged).
OLD_REQUEST_TOURS_SQL = f"""
    SELECT r.request_id::text AS request_id,
           (CASE WHEN tl_rq.state = 'retired'
                 THEN COALESCE({tlv.segment_served_member_tour_sql('r')}, {tlv.request_tour_sql()})
                 ELSE {tlv.request_tour_sql()} END)::text AS tour_id
      FROM acp_shared.angle_gate_request r
      LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
      LEFT JOIN silver_aa_internal.raw_tours rt_rq ON rt_rq.tour_id = {tlv.request_tour_sql()}
      LEFT JOIN gold_aa_internal.v_tour_liveness tl_rq
        ON tl_rq.source_id = (COALESCE(rt_rq.source_group_id, rt_rq.tour_id))::text
     WHERE r.request_id = ANY($1::uuid[]) AND r.tenant_id = $2::uuid
"""

conn = s1057.conn                     # the rolled-back transaction fixture
_atom, _segment = s1057._atom, s1057._segment

PLACE_ALIVE, PLACE_DEAD = "Quill Harbour", "Ferrow Pass"


async def _request(conn, tenant, atom, trip, subject=None):
    return str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status, subject_id) "
        "VALUES ($1, $2, $3, 'blog', 'approved', $4) RETURNING request_id",
        uuid.UUID(tenant), atom, uuid.UUID(trip) if trip else None, uuid.UUID(subject) if subject else None))


async def _subject(conn, tenant, seg):
    return str(await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, segment_id, channel, state, cleared_bar_reason) "
        "VALUES ($1, $2, 'blog', 'used', '{}'::jsonb) RETURNING subject_id", uuid.UUID(tenant), seg))


async def _both(conn, tenant, ids):
    old = {r["request_id"]: r["tour_id"] for r in await conn.fetch(OLD_REQUEST_TOURS_SQL, ids, tenant)}
    new = {r["request_id"]: r["tour_id"] for r in await conn.fetch(tlv.REQUEST_TOURS_SQL, ids, tenant)}
    return old, new


@pytest.mark.asyncio
async def test_new_request_tours_sql_returns_the_old_rows_on_every_branch(conn):
    tenant = await l2._tenant(conn, "aa838")
    other_tenant = await l2._tenant(conn, "aa838-other")
    exp = {}

    # served tour, with a trip
    live = await l2._tour(conn, "aa838-live")
    a_live = await _atom(conn, live.tour, PLACE_ALIVE)
    rq_live = await _request(conn, tenant, a_live, live.tour)
    exp[rq_live] = live.tour

    # retired representative, Segment still served elsewhere -> the served member (lowest tour id of the served ones)
    rep = await l2._tour(conn, "aa838-rep")
    mem = await l2._tour(conn, "aa838-member")
    a_rep, a_mem = await _atom(conn, rep.tour, PLACE_ALIVE), await _atom(conn, mem.tour, PLACE_ALIVE)
    seg_alive = await _segment(conn, [a_rep, a_mem, a_live], place=PLACE_ALIVE)
    subj_alive = await _subject(conn, tenant, seg_alive)
    rq_alive = await _request(conn, tenant, a_rep, rep.tour, subj_alive)
    await tl.trash_master(conn, rep.tour)
    exp[rq_alive] = min(mem.tour, live.tour)

    # retired representative, no served member left -> kept
    lone = await l2._tour(conn, "aa838-lone")
    gone = await l2._tour(conn, "aa838-gone")
    a_lone, a_gone = await _atom(conn, lone.tour, PLACE_DEAD), await _atom(conn, gone.tour, PLACE_DEAD)
    seg_dead = await _segment(conn, [a_lone, a_gone], place=PLACE_DEAD)
    rq_dead = await _request(conn, tenant, a_lone, lone.tour, await _subject(conn, tenant, seg_dead))
    await tl.trash_master(conn, lone.tour)
    await tl.trash_master(conn, gone.tour)
    exp[rq_dead] = lone.tour

    # paused representative with a served member -> kept (never substituted)
    paused = await l2._tour(conn, "aa838-paused")
    a_paused = await _atom(conn, paused.tour, PLACE_ALIVE)
    await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias) "
                       "VALUES ($1, $2, false)", seg_alive, a_paused)
    rq_paused = await _request(conn, tenant, a_paused, paused.tour, subj_alive)
    await tl.pause_master(conn, paused.tour)
    exp[rq_paused] = paused.tour

    # grouped versions: v1 superseded (old Master switched off), v2 live with its own Master; the key is served
    group = uuid.uuid4()
    v1 = await l2._tour(conn, "aa838-v1", group=group, version=1, source_status="superseded", master="inactive")
    v2 = await l2._tour(conn, "aa838-v2", group=group, version=2)
    rq_v1 = await _request(conn, tenant, await _atom(conn, v1.tour, PLACE_DEAD), v1.tour)
    rq_v2 = await _request(conn, tenant, await _atom(conn, v2.tour, PLACE_DEAD), v2.tour)
    exp[rq_v1], exp[rq_v2] = v1.tour, v2.tour

    # a tour never served (no Master, no state); an atom-only request; a request whose atom is gone
    raw = await l2._tour(conn, "aa838-raw", master=None)
    rq_raw = await _request(conn, tenant, await _atom(conn, raw.tour, PLACE_DEAD), raw.tour)
    rq_atom_only = await _request(conn, tenant, a_mem, None)
    rq_no_tour = await _request(conn, tenant, f"zz838-missing-{uuid.uuid4().hex[:8]}", None)
    exp[rq_raw], exp[rq_atom_only], exp[rq_no_tour] = raw.tour, mem.tour, None

    # another tenant's request is never returned
    rq_other = await _request(conn, other_tenant, a_live, live.tour)

    ids = sorted(exp) + [rq_other]
    old, new = await _both(conn, tenant, ids)
    assert new == old
    assert new == exp
    # the states the fixture meant to cover
    assert (await l2._state(conn, rep))[0] == "retired" and (await l2._state(conn, paused))[0] == "paused"
    assert (await l2._state(conn, v1))[0] == "served" and (await l2._state(conn, raw)) == (None, None)
    # and the module helper reads the same
    assert await tlv.request_tours(conn, tenant, ids) == exp


@pytest.mark.asyncio
async def test_the_key_state_lookup_stays_a_one_key_read(conn):
    """The plan guard of the speed-up: the key filter must be pushed into v_tour_liveness's GROUP BY (a filter on the
    raw_tours scan inside the view), not left as a join filter over the whole view per request row, which is what
    the planner does when the LATERAL subquery loses its OFFSET 0 fence (it flattens it back into a join)."""
    tenant = str(uuid.uuid4())
    rows = await conn.fetch("EXPLAIN " + tlv.REQUEST_TOURS_SQL.replace("$1::uuid[]", f"ARRAY['{uuid.uuid4()}']::uuid[]")
                            .replace("$2::uuid", f"'{tenant}'::uuid"))
    plan = "\n".join(r[0] for r in rows)
    assert "Join Filter: (g.source_key" not in plan, plan
    assert "= (COALESCE(rt_rq.source_group_id, rt_rq.tour_id))::text)" in plan, plan


@pytest.mark.asyncio
async def test_new_request_tours_sql_returns_the_old_rows_on_the_other_subject_rule_branches(conn):
    """AA-1179 (S293): the branches the first test does not reach. An atom-only request (no trip) whose atom's tour is
    retired while its Segment is served elsewhere (substituted); a Subject stored on a Segment id that merged into
    another (atom_segment_alias: the surviving Segment's served member counts); a representative retired by a
    source delete (retire_source, reason source_deleted) with a served member; a Segment whose only other member is a
    deleted atom (not served: kept); a Route Subject (no Segment) on a retired tour (kept)."""
    tenant = await l2._tenant(conn, "aa1179")
    exp = {}
    place_a, place_b, place_c, place_d = "Orrin Cove", "Vessel Ridge", "Talmer Ford", "Brask Hollow"

    # atom-only request on a retired tour, Segment served on another tour -> that member
    rep = await l2._tour(conn, "aa1179-rep")
    mem = await l2._tour(conn, "aa1179-mem")
    a_rep, a_mem = await _atom(conn, rep.tour, place_a), await _atom(conn, mem.tour, place_a)
    seg_a = await _segment(conn, [a_rep, a_mem], place=place_a)
    rq_atom_only = await _request(conn, tenant, a_rep, None, await _subject(conn, tenant, seg_a))
    await tl.trash_master(conn, rep.tour)
    exp[rq_atom_only] = mem.tour

    # the Subject's Segment merged into another: the members of the surviving Segment count
    old_rep = await l2._tour(conn, "aa1179-merged-rep")
    survivor = await l2._tour(conn, "aa1179-survivor")
    a_old, a_surv = await _atom(conn, old_rep.tour, place_b), await _atom(conn, survivor.tour, place_b)
    seg_old = await _segment(conn, [a_old], place=place_b)
    seg_new = await _segment(conn, [a_surv], place=place_b)
    await conn.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                       "VALUES ($1, $2, now())", seg_old, seg_new)
    rq_merged = await _request(conn, tenant, a_old, old_rep.tour, await _subject(conn, tenant, seg_old))
    await tl.trash_master(conn, old_rep.tour)
    exp[rq_merged] = survivor.tour

    # representative retired by a source delete, Segment served elsewhere -> the member
    src = await l2._tour(conn, "aa1179-src")
    src_mem = await l2._tour(conn, "aa1179-src-mem")
    a_src, a_src_mem = await _atom(conn, src.tour, place_c), await _atom(conn, src_mem.tour, place_c)
    seg_c = await _segment(conn, [a_src, a_src_mem], place=place_c)
    rq_src = await _request(conn, tenant, a_src, src.tour, await _subject(conn, tenant, seg_c))
    await tl.retire_source(conn, src.tour)
    exp[rq_src] = src_mem.tour

    # the only other member is a deleted atom (not served) -> kept
    lone = await l2._tour(conn, "aa1179-lone")
    other = await l2._tour(conn, "aa1179-deleted-atom")
    a_lone, a_del = await _atom(conn, lone.tour, place_d), await _atom(conn, other.tour, place_d, deleted=True)
    seg_d = await _segment(conn, [a_lone, a_del], place=place_d)
    rq_lone = await _request(conn, tenant, a_lone, lone.tour, await _subject(conn, tenant, seg_d))
    await tl.trash_master(conn, lone.tour)
    exp[rq_lone] = lone.tour

    # a Route Subject (segment_id NULL) on a retired tour -> kept (the Subject rule is for Segment Subjects)
    route_tour = await l2._tour(conn, "aa1179-route")
    a_route = await _atom(conn, route_tour.tour, place_a)
    route_id = f"zz1179-route-{uuid.uuid4().hex[:8]}"
    await s1057._route(conn, route_id, route_tour.tour, 1, 2, [seg_a])
    route_subject = str(await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, route_id, channel, state, cleared_bar_reason) "
        "VALUES ($1, $2, 'blog', 'used', '{}'::jsonb) RETURNING subject_id",
        uuid.UUID(tenant), route_id))
    rq_route = await _request(conn, tenant, a_route, route_tour.tour, route_subject)
    await tl.trash_master(conn, route_tour.tour)
    exp[rq_route] = route_tour.tour

    ids = sorted(exp)
    old, new = await _both(conn, tenant, ids)
    assert new == old
    assert new == exp
    for t in (rep, old_rep, lone, route_tour):
        assert (await l2._state(conn, t))[0] == "retired", t
    assert await l2._state(conn, src) == ("retired", "source_deleted")
