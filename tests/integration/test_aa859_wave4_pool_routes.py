"""AA-859 wave 4 — the moved routes against real Postgres, as aa_app_user under 265 / 266 (and 229 / 239 / 241).

  - forgotten WHERE, per route: the module SQL constant patched so its tenant condition is gone; with the flag ON the
    tenant pool still shows the caller nothing of another tenant's, with the flag OFF the admin pool leaks it (so the
    check can fail): Slate subjects, angle-gate request, review list, marketplace atom count.
  - pick on the tenant pool: the real statements (row lock, members on the base table with the 203 rule inlined, alias
    cards in a savepoint, tenant tours, state flip, request insert) run as aa_app_user; another tenant's Subject is a
    404, a second pick a 409; the audit row goes to MAIN after the tenant block.
  - cut: the conditional UPDATE; another tenant's Subject is a 404.
  - pick: a Subject on a Segment whose only member is another tenant's own atom has no seed (409), also with the
    owner_scope filter gone (review S247).
  - trip-reallocation confirm accept=true: save + approve in one tenant transaction under 265's column grants; and
    the forgotten WHERE of _APPROVED_PLAN_SQL / _QUARTER_PLAN_ID_SQL at route level (review S247).
  - the tenant trip query's LATERAL trip page: the caller's own page over the master's, one row per tour, on both pools.

Same harness as test_aa859_t2_version_reads_pool.py (D5a): the "tenant pool" is the test connection with SET LOCAL
ROLE aa_app_user; everything runs in one transaction that is rolled back. Two concurrent confirms (the advisory lock
serializes them) are not exercised here: the harness has one connection; test_aa859_trip_reallocation_pool covers the
409 mapping of a unique violation.
"""
import json
import os
import sys
import uuid
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
from api.routers import v1_angle_gate, v1_marketplace, v1_planning  # noqa: E402
from services.acp_angle_gate import service as ag_service  # noqa: E402
from services.acp_content_writing import service as cw_service  # noqa: E402
from services.acp_planning import tenant_pool as planning_pool  # noqa: E402
from services.acp_planning import trip_reallocation  # noqa: E402
from services.acp_shared import slate  # noqa: E402
from test_aa859_t2_version_reads_pool import (  # noqa: E402
    MASTER, TTV_LIVE_STANDIN_DDL, _master_tour, _OneConnPool, _Redis, _tenant, _version,
)

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"


class _MainPool(_OneConnPool):
    """The admin pool over the test connection, with Pool.execute (write_audit_log(pool, ...))."""

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_MainPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE), redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _forget(monkeypatch, module, name, old, new):
    value = getattr(module, name)
    assert old in value, (name, old)
    monkeypatch.setattr(module, name, value.replace(old, new))


async def _subject(conn, tid, segment, state="proposed", channel="blog"):
    return await conn.fetchval(
        "INSERT INTO acp_shared.subject (tenant_id, segment_id, channel, state, cleared_bar_reason, score) "
        "VALUES ($1::uuid, $2, $3, $4, '{}'::jsonb, 1) RETURNING subject_id", tid, segment, channel, state)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid, ADD COLUMN IF NOT EXISTS channel text,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer,
                ADD COLUMN IF NOT EXISTS flags jsonb""")
        a, b, c = await _tenant(conn), await _tenant(conn), await _tenant(conn)
        name = f"AA859-W4 {uuid.uuid4().hex[:8]}"
        pt = await _master_tour(conn, name)
        tour = await conn.fetchval("SELECT tour_id FROM gold_aa_internal.published_tours WHERE id = $1", pt)
        await _version(conn, a, pt, 1)
        await _version(conn, b, pt, 1)
        await _version(conn, c, pt, 1)
        segment = f"aa859w4-{uuid.uuid4().hex[:10]}"
        place = f"Hoan Kiem Lake {segment[-4:]}"
        await conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                           "VALUES ($1, $2, 'walk')", segment, place)
        atom_a = f"aa859w4-a-{uuid.uuid4().hex[:8]}"
        atom_p = f"aa859w4-p-{uuid.uuid4().hex[:8]}"
        for atom, scope in ((atom_a, a), (atom_p, "platform")):
            await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, "
                               "evidence) VALUES ($1, $2, $3, 'Walk round the lake', $4, 'x')",
                               atom, tour, scope, place)
        await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                           segment, atom_p)
        subject_a = await _subject(conn, a, segment)
        subject_a2 = await _subject(conn, a, segment, channel="facebook")
        request_a = await conn.fetchval(
            "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status, subject_id) "
            "VALUES ($1::uuid, $2, $3, 'blog', 'approved', $4) RETURNING request_id", a, atom_p, tour, subject_a)
        opt = await conn.fetchval("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, "
                                  "formula_fit, best_final_style, chosen) VALUES ($1, 0, 'Angle', 'w', 'f', 's', true) "
                                  "RETURNING option_id", request_a)
        await conn.execute(
            "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, "
            "attempt_number, content_text, status, gate_ledger, repair_log) VALUES ($1::uuid, $2, $3, 1, 'Body', "
            "'approved', '[]'::jsonb, '[]'::jsonb)", a, request_a, opt)
        for tid, url in ((MASTER, "https://aa/master"), (a, "https://a/page")):
            await conn.execute("INSERT INTO acp_deliver.tenant_tour_pages (tenant_id, tour_id, url) "
                               "VALUES ($1, $2, $3)", tid, tour, url)
        yield SimpleNamespace(conn=conn, a=a, b=b, c=c, pt=pt, tour=tour, segment=segment, atom_a=atom_a,
                              atom_p=atom_p, subject_a=subject_a, subject_a2=subject_a2, request_a=request_a)
    finally:
        await tr.rollback()
        await conn.close()


# ── forgotten WHERE, per route ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slate_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, slate, "_SLATE_SUBJECTS_SQL", "s.tenant_id = $1::uuid", "$1::uuid IS NOT NULL")

    async def _get(tid, flag_on):
        with patch.object(v1_planning, "request_slate_proposal", new=AsyncMock(return_value=None)), \
                patch.object(v1_planning, "last_proposal", new=AsyncMock(return_value=None)):
            out = await v1_planning.get_slate(_request(ctx.conn, flag_on), tenant={"sub": tid})
        return {s["subject_id"] for ch in out["channels"].values() for s in ch["subjects"]}
    assert not (await _get(ctx.c, True)) & {str(ctx.subject_a)}, "tenant C read A's Subjects on the tenant pool"
    assert str(ctx.subject_a) in await _get(ctx.c, False), "sanity: the admin pool is not filtered"
    assert str(ctx.subject_a) in await _get(ctx.a, True)


@pytest.mark.asyncio
async def test_angle_gate_read_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, ag_service, "_REQUEST_ROW_SQL", "request_id = $1 AND tenant_id = $2",
            "request_id = $1 AND $2::uuid IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await v1_angle_gate.get_request(ctx.request_a, _request(ctx.conn, True), tenant={"sub": ctx.b})
    assert exc.value.status_code == 404
    leaked = await v1_angle_gate.get_request(ctx.request_a, _request(ctx.conn, False), tenant={"sub": ctx.b})
    assert leaked["request_id"] == str(ctx.request_a), "sanity: the admin pool is not filtered"
    own = await v1_angle_gate.get_request(ctx.request_a, _request(ctx.conn, True), tenant={"sub": ctx.a})
    assert own["subject_place"] and own["angles"][0]["name"] == "Angle"


@pytest.mark.asyncio
async def test_reviews_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, cw_service, "_TENANT_REVIEWS_QUERY", "WHERE cp.tenant_id = $1", "WHERE $1::uuid IS NOT NULL")
    on = await cw_service.fetch_review_list(uuid.UUID(ctx.c), None, request=_request(ctx.conn, True))
    assert [r for r in on if r["request_id"] == str(ctx.request_a)] == []
    off = await cw_service.fetch_review_list(uuid.UUID(ctx.c), None, request=_request(ctx.conn, False))
    assert [r for r in off if r["request_id"] == str(ctx.request_a)], "sanity: the admin pool is not filtered"
    own = await cw_service.fetch_review_list(uuid.UUID(ctx.a), None, request=_request(ctx.conn, True))
    row = next(r for r in own if r["request_id"] == str(ctx.request_a))
    assert row["atom"]["text"] == "Walk round the lake" and row["tour"]["name"].startswith("AA859-W4")


@pytest.mark.asyncio
async def test_review_detail_on_the_tenant_pool(ctx):
    review = await cw_service.fetch_review(uuid.UUID(ctx.a), ctx.request_a, None, request=_request(ctx.conn, True))
    assert review["content_text"] == "Body" and review["atom"]["text"] == "Walk round the lake"
    with pytest.raises(ag_service.RequestNotFoundError):
        await cw_service.fetch_review(uuid.UUID(ctx.b), ctx.request_a, None, request=_request(ctx.conn, True))


@pytest.mark.asyncio
async def test_marketplace_rls_holds_when_the_owner_scope_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, v1_marketplace, "_MARKETPLACE_QUERY", "WHERE owner_scope = $2", "WHERE $2::text IS NOT NULL")

    async def _atoms(tid, flag_on):
        out = await v1_marketplace.get_marketplace(_request(ctx.conn, flag_on), tenant={"sub": tid})
        return out["total_atoms"]
    # B owns no atom on the tour; forgotten owner_scope: the tenant pool shows B the platform atom (1), never A's
    assert await _atoms(ctx.b, True) == 1
    assert await _atoms(ctx.b, False) == 2, "sanity: the admin pool counts A's atom too"


# ── pick / cut on the tenant pool ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_on_the_tenant_pool(ctx):
    req = _request(ctx.conn, True)
    with pytest.raises(HTTPException) as exc:
        await v1_planning.post_pick_subject(ctx.subject_a, req, tenant={"sub": ctx.b})
    assert exc.value.status_code == 404
    out = await v1_planning.post_pick_subject(ctx.subject_a, req, tenant={"sub": ctx.a})
    assert out["atom_id"] == ctx.atom_p and out["trip_id"] == str(ctx.tour)
    assert await ctx.conn.fetchval("SELECT state FROM acp_shared.subject WHERE subject_id = $1", ctx.subject_a) \
        == "picked"
    assert await ctx.conn.fetchval("SELECT tenant_id::text FROM acp_shared.angle_gate_request WHERE request_id = $1",
                                   uuid.UUID(out["request_id"])) == ctx.a
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE action = 'slate.subject_picked' "
                                   "AND resource_id = $1", str(ctx.subject_a)) == 1, "audit row on MAIN after commit"
    with pytest.raises(HTTPException) as exc:
        await v1_planning.post_pick_subject(ctx.subject_a, req, tenant={"sub": ctx.a})
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_cut_on_the_tenant_pool(ctx):
    req = _request(ctx.conn, True)
    with pytest.raises(HTTPException) as exc:
        await v1_planning.post_cut_subject(ctx.subject_a2, req, tenant={"sub": ctx.b})
    assert exc.value.status_code == 404
    out = await v1_planning.post_cut_subject(ctx.subject_a2, req, tenant={"sub": ctx.a})
    assert out["state"] == "cut"
    with pytest.raises(HTTPException) as exc:
        await v1_planning.post_cut_subject(ctx.subject_a2, req, tenant={"sub": ctx.a})
    assert exc.value.status_code == 409


# ── trip reallocation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_accept_saves_and_approves_on_the_tenant_pool(ctx):
    req = _request(ctx.conn, True)
    tid = uuid.UUID(ctx.a)
    out = await trip_reallocation.confirm_trip_reallocation_scoped(req, tid, 2031, 3, True, actor=f"tenant:{tid}")
    assert out["accepted"] is True
    row = await ctx.conn.fetchrow(
        "SELECT qp.tenant_id::text AS tenant_id, qpv.approval_status, qpv.version_no FROM acp_shared.quarter_plan qp "
        "JOIN acp_shared.quarter_plan_version qpv ON qpv.version_id = qp.current_version_id "
        "WHERE qpv.version_id = $1", uuid.UUID(out["version_id"]))
    assert dict(row) == {"tenant_id": ctx.a, "approval_status": "approved", "version_no": 1}
    again = await trip_reallocation.confirm_trip_reallocation_scoped(req, tid, 2031, 3, True, actor=f"tenant:{tid}")
    assert await ctx.conn.fetchval("SELECT version_no FROM acp_shared.quarter_plan_version WHERE version_id = $1",
                                   uuid.UUID(again["version_id"])) == 2
    audits = await ctx.conn.fetch("SELECT details FROM acp_shared.audit_log WHERE action = "
                                  "'trip_reallocation_suggestion' AND tenant_id = $1 ORDER BY created_at", ctx.a)
    assert [json.loads(r["details"])["applied"] for r in audits][-2:] == [True, True]


@pytest.mark.parametrize("forget_owner_scope", [False, True])
@pytest.mark.asyncio
async def test_pick_never_seeds_from_another_tenants_atom(ctx, monkeypatch, forget_owner_scope):
    """Review S247: tenant B's Subject on a Segment whose only member is tenant A's own atom has no seed for B: 409 on
    the tenant pool, also with the owner_scope filter of the member reads gone (RLS on tour_atoms, 266, holds it). With
    that filter gone the admin pool seeds B's pick from A's atom (so the check can fail)."""
    from services.acp_shared import route_guard, subject_seed
    seg = f"aa859w4-own-{uuid.uuid4().hex[:8]}"
    await ctx.conn.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                           "SELECT $1, canonical_place, canonical_action FROM acp_contract.atom_segment "
                           "WHERE segment_id = $2", seg, ctx.segment)
    await ctx.conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                           seg, ctx.atom_a)
    subject_b = await _subject(ctx.conn, ctx.b, seg)
    if forget_owner_scope:
        _forget(monkeypatch, subject_seed, "_MEMBERS_SQL", "AND ta.owner_scope IN ('platform', $2::text)",
                "AND $2::text IS NOT NULL")
        _forget(monkeypatch, route_guard, "_TRIP_MEMBER_SQL", "AND ta.owner_scope IN ('platform', $3::text)",
                "AND $3::text IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await v1_planning.post_pick_subject(subject_b, _request(ctx.conn, True), tenant={"sub": ctx.b})
    assert exc.value.status_code == 409, "no seed for B on the tenant pool"
    assert await ctx.conn.fetchval("SELECT state FROM acp_shared.subject WHERE subject_id = $1", subject_b) \
        == "proposed"
    if forget_owner_scope:
        out = await v1_planning.post_pick_subject(subject_b, _request(ctx.conn, False), tenant={"sub": ctx.b})
        assert out["atom_id"] == ctx.atom_a, "sanity: the admin pool seeds from A's atom once the filter is gone"


@pytest.mark.asyncio
async def test_trip_reallocation_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    """Review S247: route-level forgotten WHERE for _APPROVED_PLAN_SQL (suggest / confirm read) and
    _QUARTER_PLAN_ID_SQL (confirm save). A approves 2031-Q4 first; B, with both tenant conditions gone, sees no
    existing plan and saves onto its own plan row on the tenant pool; the admin pool shows B A's plan."""
    from services.acp_planning import quarter
    a, b = uuid.UUID(ctx.a), uuid.UUID(ctx.b)
    own = await trip_reallocation.confirm_trip_reallocation_scoped(_request(ctx.conn, True), a, 2031, 4, True,
                                                                   actor=f"tenant:{a}")
    plan_a = await ctx.conn.fetchval("SELECT plan_id FROM acp_shared.quarter_plan_version WHERE version_id = $1",
                                     uuid.UUID(own["version_id"]))
    _forget(monkeypatch, quarter, "_APPROVED_PLAN_SQL", "WHERE qp.tenant_id = $1 AND", "WHERE $1::uuid IS NOT NULL AND")
    _forget(monkeypatch, quarter, "_QUARTER_PLAN_ID_SQL", "WHERE tenant_id = $1 AND",
            "WHERE $1::uuid IS NOT NULL AND")

    on = await trip_reallocation.suggest_trip_reallocation_scoped(_request(ctx.conn, True), b, 2031, 4)
    assert on["has_existing_plan"] is False, "B read A's approved plan on the tenant pool"
    off = await trip_reallocation.suggest_trip_reallocation_scoped(_request(ctx.conn, False), b, 2031, 4)
    assert off["has_existing_plan"] is True, "sanity: the admin pool is not filtered"

    saved = await trip_reallocation.confirm_trip_reallocation_scoped(_request(ctx.conn, True), b, 2031, 4, True,
                                                                     actor=f"tenant:{b}")
    row = await ctx.conn.fetchrow(
        "SELECT qp.plan_id, qp.tenant_id FROM acp_shared.quarter_plan_version qpv "
        "JOIN acp_shared.quarter_plan qp ON qp.plan_id = qpv.plan_id WHERE qpv.version_id = $1",
        uuid.UUID(saved["version_id"]))
    assert row["tenant_id"] == b and row["plan_id"] != plan_a, "B's version landed on its own plan"
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.quarter_plan_version WHERE plan_id = $1",
                                   plan_a) == 1, "A's plan untouched"
    assert await ctx.conn.fetchval(quarter._QUARTER_PLAN_ID_SQL.replace("SELECT plan_id", "SELECT count(*)"),
                                   b, 2031, 4) == 2, "sanity: without RLS the forgotten statement sees both plans"


# ── the LATERAL trip page ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trip_page_is_own_over_master_one_row_per_tour(ctx):
    for flag_on in (True, False):
        req = _request(ctx.conn, flag_on)
        pool = req.app.state.tenant_pool if flag_on else req.app.state.pool
        for tid, url in ((ctx.a, "https://a/page"), (ctx.c, "https://aa/master")):
            async with pool.acquire() as conn:
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tid)
                trips = await planning_pool.fetch_tenant_trips_on(conn, uuid.UUID(tid))
            ours = [t for t in trips if t.id == ctx.tour]
            assert len(ours) == 1, (flag_on, tid, "one row per tour, no duplicate per tenant page")
            assert ours[0].trip_url == url, (flag_on, tid)


# ── C9: the base-table member reads return what the v_active_tour_atoms reads returned ──────────────

@pytest.mark.asyncio
async def test_base_table_member_reads_match_the_view(ctx):
    """subject_seed._MEMBERS_SQL and route_guard._TRIP_MEMBER_SQL inline the 203 rule instead of reading the view;
    on the main pool (owner / cis_app: every row) the rows are the same, also for a deleted atom, an empty marker and
    an atom on a tour without a live published row."""
    from services.acp_shared import route_guard, subject_seed
    conn = ctx.conn
    dead_tour = await conn.fetchval("INSERT INTO silver_aa_internal.raw_tours (tenant_id, batch_id, src_name) "
                                    "VALUES ($1::uuid, gen_random_uuid(), 'aa859w4-dead') RETURNING tour_id", MASTER)
    extra = []
    for tour, deleted, marker in ((ctx.tour, True, False), (ctx.tour, False, True), (dead_tour, False, False)):
        atom = f"aa859w4-x-{uuid.uuid4().hex[:8]}"
        await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, place, deleted, "
                           "is_empty_marker) VALUES ($1, $2, 'x', 'p', $3, $4)", atom, tour, deleted, marker)
        await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                           ctx.segment, atom)
        extra.append(atom)
    # AA-986 L1: the inlined rule is shared.tour_liveness.served_tour_joins_sql (live Master AND live source, CIS 271)
    from shared.tour_liveness import served_tour_joins_sql
    view_members = subject_seed._MEMBERS_SQL.replace(
        "JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id\n    " + served_tour_joins_sql("ta.tour_id"),
        "JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id\n"
        "    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id")
    assert view_members != subject_seed._MEMBERS_SQL
    for tid in (ctx.a, ctx.b):
        new = sorted(map(dict, await conn.fetch(subject_seed._MEMBERS_SQL, [ctx.segment], tid)), key=str)
        old = sorted(map(dict, await conn.fetch(view_members, [ctx.segment], tid)), key=str)
        assert new == old and {r["atom_id"] for r in new} & set(extra) == set()
    view_trip = route_guard._TRIP_MEMBER_SQL.replace(
        "JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id\n               "
        + served_tour_joins_sql("ta.tour_id"),
        "JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id")
    assert view_trip != route_guard._TRIP_MEMBER_SQL
    for tour in (ctx.tour, dead_tour):
        args = ([ctx.segment], str(tour), ctx.a)
        assert [dict(r) for r in await conn.fetch(route_guard._TRIP_MEMBER_SQL, *args)] == \
            [dict(r) for r in await conn.fetch(view_trip, *args)]
