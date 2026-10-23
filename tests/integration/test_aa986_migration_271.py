"""AA-986 L1 — CIS 271 (v_active_tour_atoms requires a live source) and the publish gates, on real Postgres.

conftest replays 271 like every file. Each test runs in one transaction that is rolled back.

  * migration: the view is first put back to its 208 body (Master join only), tours are seeded (live; source
    trashed; source superseded; source active but deleted_at set; Master inactive), then the file body is
    applied (errors not swallowed): the dead-source atoms leave the view, the live tour's stay, the columns
    (information_schema) and the grants are the same before and after; re-applying is a no-op; a runner that does
    not own the view is refused (guard 1).
  * the shared fragment (subject_seed / route_guard) returns the same atoms as the view after 271.
  * approve on a trashed / superseded source: 409 source_not_live, nothing changes (review pending, version not
    approved, no Master); on a live source the approve publishes.
  * process_export on an approved version of a trashed source: refused, no Master row.
  * _enqueue_review files a dead-source review 'dismissed'; the admin review list hides pending dead-source rows and
    flags closed ones (source_live false).
"""
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

from shared.tour_liveness import SourceNotLiveError, served_tour_joins_sql  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"

PRE_271_VIEW = """
CREATE OR REPLACE VIEW acp_contract.v_active_tour_atoms AS
SELECT ta.atom_id, ta.tour_id, ta.owner_scope, ta.text, ta.activity_type, ta.emotional_hook, ta.visual_potential,
       ta.persona_fit, ta.season_note, ta.media, ta.starred, ta.deleted, ta.usage_log, ta.cooldown_until,
       ta.human_seam_notes, ta.weight, ta.created_at, ta.updated_at, ta.source_hash, ta.is_empty_marker,
       ta.itinerary_day, ta.place, ta.action, ta.evidence
FROM acp_contract.tour_atoms ta
JOIN gold_aa_internal.published_tours pt
  ON pt.tour_id = ta.tour_id AND pt.master_status = 'active' AND pt.deleted_at IS NULL
WHERE NOT ta.deleted AND NOT ta.is_empty_marker
"""

COLUMNS_SQL = """
SELECT column_name, data_type, ordinal_position FROM information_schema.columns
 WHERE table_schema = 'acp_contract' AND table_name = 'v_active_tour_atoms' ORDER BY ordinal_position
"""
ACL_SQL = """
SELECT pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS relacl,
       (SELECT string_agg(a.attname || '=' || a.attacl::text, ';' ORDER BY a.attnum) FROM pg_attribute a
         WHERE a.attrelid = c.oid AND a.attnum > 0 AND a.attacl IS NOT NULL) AS attacl
  FROM pg_class c WHERE c.oid = 'acp_contract.v_active_tour_atoms'::regclass
"""


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


class _NoClose:
    """process_export opens and closes its own connection: hand it the test connection, keep it open."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


async def _tour(conn, label, *, source_status="active", deleted=False, master="active", group=None):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, deleted_at, source_group_id) "
        "VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', $5::silver_aa_internal.source_status_enum, "
        "CASE WHEN $6 THEN now() END, $7)",
        tour_id, MASTER, BATCH_ID, f"aa986 {label}", source_status, deleted, group)
    if master:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, $5)",
            tour_id, uuid.uuid4(), MASTER, f"aa986 {label}", master)
    atom = f"aa986-{label}-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence) "
                       "VALUES ($1, $2, 'platform', 'Walk round the lake', 'Hoan Kiem', 'x')", atom, tour_id)
    return SimpleNamespace(tour=tour_id, atom=atom)


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


async def _seed_all(conn):
    group = uuid.uuid4()
    return {
        "live": await _tour(conn, "live"),
        "trashed": await _tour(conn, "trashed", source_status="trashed", deleted=True),
        "superseded": await _tour(conn, "superseded", source_status="superseded", group=group),
        "deleted_at": await _tour(conn, "deletedat", deleted=True),
        "master_off": await _tour(conn, "masteroff", master="inactive"),
    }


async def _view_atoms(conn, atoms):
    rows = await conn.fetch("SELECT atom_id FROM acp_contract.v_active_tour_atoms WHERE atom_id = ANY($1)", atoms)
    return {r["atom_id"] for r in rows}


# ── migration 271 ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_271_drops_dead_source_atoms_and_keeps_columns_and_grants(conn):
    await conn.execute(PRE_271_VIEW)
    before_cols = [tuple(r) for r in await conn.fetch(COLUMNS_SQL)]
    before_acl = dict(await conn.fetchrow(ACL_SQL))
    assert len(before_cols) == 24
    t = await _seed_all(conn)
    atoms = [x.atom for x in t.values()]
    # the leak 271 closes: before, a dead source with an active Master still serves its atoms
    assert await _view_atoms(conn, atoms) == {t[k].atom for k in ("live", "trashed", "superseded", "deleted_at")}

    await conn.execute(migration_body(271))

    assert await _view_atoms(conn, atoms) == {t["live"].atom}
    assert [tuple(r) for r in await conn.fetch(COLUMNS_SQL)] == before_cols
    assert dict(await conn.fetchrow(ACL_SQL)) == before_acl
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '271'") == 1

    await conn.execute(migration_body(271))                      # re-apply: guards pass, nothing changes
    assert await _view_atoms(conn, atoms) == {t["live"].atom}
    assert [tuple(r) for r in await conn.fetch(COLUMNS_SQL)] == before_cols


@pytest.mark.asyncio
async def test_271_view_body_is_the_shared_rule(conn):
    """After 271 the view and the shared fragment (subject_seed / route_guard's joins) keep the same atoms."""
    t = await _seed_all(conn)
    atoms = [x.atom for x in t.values()]
    via_fragment = await conn.fetch(
        f"SELECT ta.atom_id FROM acp_contract.tour_atoms ta {served_tour_joins_sql('ta.tour_id')} "
        "WHERE NOT ta.deleted AND NOT ta.is_empty_marker AND ta.atom_id = ANY($1)", atoms)
    assert {r["atom_id"] for r in via_fragment} == await _view_atoms(conn, atoms) == {t["live"].atom}


@pytest.mark.asyncio
async def test_271_refuses_a_runner_that_does_not_own_the_view(conn):
    await conn.execute("GRANT USAGE ON SCHEMA acp_contract, gold_aa_internal, silver_aa_internal, shared TO cis_app")
    with pytest.raises(asyncpg.PostgresError, match="migration 271: cis_app may not replace the view"):
        async with conn.transaction():                           # savepoint: the outer transaction survives
            await conn.execute("SET LOCAL ROLE cis_app")
            await conn.execute(migration_body(271))
    await conn.execute("RESET ROLE")


# ── publish gates ──────────────────────────────────────────────────────────────────────────────────────────────────

async def _review(conn, tour_id, *, gc_status="hitl", review_status="pending"):
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, status) "
        "VALUES ($1, $2, 1, 'aa986', $3::content_status_enum) RETURNING id", tour_id, MASTER, gc_status)
    rq = await conn.fetchval(
        "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status, "
        "score_overall) VALUES ($1, $2, $3, $4::review_status_enum, 6.0) RETURNING id",
        tour_id, gc, MASTER, review_status)
    return gc, rq


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


@pytest.mark.asyncio
@pytest.mark.parametrize("status, deleted", [("trashed", True), ("superseded", False)])
async def test_approve_on_a_dead_source_is_409_and_changes_nothing(conn, status, deleted):
    from api.routers import v1_pipeline
    t = await _tour(conn, status, source_status=status, deleted=deleted, master=None)
    gc, rq = await _review(conn, t.tour)
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        with pytest.raises(HTTPException) as exc:
            await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert exc.value.status_code == 409 and exc.value.detail.startswith("source_not_live")
    assert status in exc.value.detail
    export.assert_not_called()
    assert await conn.fetchval("SELECT review_status::text FROM silver_aa_internal.review_queue WHERE id = $1",
                               rq) == "pending"
    assert await conn.fetchval("SELECT status::text FROM silver_aa_internal.generated_content WHERE id = $1",
                               gc) == "hitl"
    assert await conn.fetchval("SELECT count(*) FROM gold_aa_internal.published_tours WHERE tour_id = $1",
                               t.tour) == 0


@pytest.mark.asyncio
async def test_approve_on_a_live_source_publishes(conn):
    from api.routers import v1_pipeline
    t = await _tour(conn, "approve-live", master=None)
    gc, rq = await _review(conn, t.tour)
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=_NoClose(conn))), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()):
        out = await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert out["status"] == "approved" and out["exported"] is True
    assert await conn.fetchval("SELECT master_status::text FROM gold_aa_internal.published_tours WHERE tour_id = $1",
                               t.tour) == "active"


@pytest.mark.asyncio
async def test_process_export_refuses_an_approved_version_of_a_trashed_source(conn):
    from services.export import handler
    t = await _tour(conn, "export-trashed", source_status="trashed", deleted=True, master=None)
    gc, _ = await _review(conn, t.tour, gc_status="approved", review_status="dismissed")
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=_NoClose(conn))), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()) as atomize:
        with pytest.raises(SourceNotLiveError) as exc:
            await handler.process_export(str(gc))
    assert exc.value.state.reason == "trashed"
    atomize.assert_not_called()
    assert await conn.fetchval("SELECT count(*) FROM gold_aa_internal.published_tours WHERE tour_id = $1",
                               t.tour) == 0


# ── review queue in and out ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_review_files_a_dead_source_as_dismissed(conn):
    from api.routers import admin_pipeline
    dead = await _tour(conn, "enq-dead", source_status="trashed", deleted=True, master=None)
    live = await _tour(conn, "enq-live", master=None)
    out = {}
    for name, t in (("dead", dead), ("live", live)):
        gc = await conn.fetchval(
            "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, status) "
            "VALUES ($1, $2, 1, 'aa986', 'hitl') RETURNING id", t.tour, MASTER)
        await admin_pipeline._enqueue_review(conn, str(t.tour), str(gc), {"quality_score": 5.0})
        out[name] = await conn.fetchval(
            "SELECT review_status::text FROM silver_aa_internal.review_queue WHERE generated_content_id = $1", gc)
    assert out == {"dead": "dismissed", "live": "pending"}


@pytest.mark.asyncio
async def test_review_lists_hide_pending_dead_sources(conn, monkeypatch):
    from api.routers import admin_pipeline, v1_pipeline
    monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda *_a, **_k: None)
    live = await _tour(conn, "list-live", master=None)
    dead = await _tour(conn, "list-dead", source_status="superseded", master=None)
    done = await _tour(conn, "list-done", source_status="trashed", deleted=True, master=None)
    _, rq_live = await _review(conn, live.tour)
    _, rq_dead = await _review(conn, dead.tour)
    _, rq_done = await _review(conn, done.tour, gc_status="approved", review_status="approved")
    ours = {str(rq_live), str(rq_dead), str(rq_done)}

    pending = await admin_pipeline.admin_review_queue(_req(conn), x_admin_secret="x", page_size=100,
                                                      status="pending", country=None, score="all", tour_id=None)
    assert {r["id"] for r in pending["data"]} & ours == {str(rq_live)}
    every = await admin_pipeline.admin_review_queue(_req(conn), x_admin_secret="x", page_size=100,
                                                    status="all", country=None, score="all", tour_id=None)
    by_id = {r["id"]: r for r in every["data"] if r["id"] in ours}
    assert set(by_id) == {str(rq_live), str(rq_done)}
    assert by_id[str(rq_live)]["source_live"] is True and by_id[str(rq_done)]["source_live"] is False

    v1 = await v1_pipeline.get_review_queue(_req(conn), tenant=None, page=1, page_size=100)
    assert {str(r["id"]) for r in v1["data"]} & ours == {str(rq_live)}
