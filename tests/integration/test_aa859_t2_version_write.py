"""AA-859 PR-D6 (W6) — PATCH /v1/tours/versions/{id} against real Postgres, as aa_app_user under migration
229's policy on a live-shape tenant_tour_versions stand-in (_ttv_live_standin.py).

  - edit on the tenant pool inserts the next version (MAX + 1 over the tour, parent = the edited one,
    tenant_edit, pending); approve / reject change the own row;
  - another tenant's version_id is a 404 for edit and for approve, and nothing of the owner changes;
  - a forgotten WHERE on the own-row SELECT still cannot reach another tenant through the route on the
    tenant pool, while the admin pool (owner, RLS exempt) does: the check can fail;
  - a forgotten WHERE on the status UPDATE, run directly as aa_app_user with B's GUC (the route cannot
    reach it: its own-row SELECT already 404s), updates 0 of A's rows; the same statement without the role
    switch updates 1 (mutation);
  - two concurrent edits of the same tour on two real connections get N+1 and N+2, never a 500; with the
    advisory lock removed (mutation) the same race is one 200 and one 409, never two 200s with equal
    numbers.

Harness as test_aa817_tenant_pool_routes.py: the "tenant pool" is the test connection under SET LOCAL ROLE
aa_app_user (CI's aa_app_user has no LOGIN), everything in one transaction that is rolled back. The
concurrency tests need two connections that see each other's commits: they commit the stand-in and their
rows, and drop / delete them in `finally`.
"""
import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from structlog.testing import capture_logs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _ttv_live_standin import install_ttv_live_standin, seed_published_tour  # noqa: E402
from api.routers import v1_tours  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
FORGOT = "AND $2::uuid IS NOT NULL"
OWN_FILTER = "AND tenant_id = $2::uuid"
UPDATE_FILTER = "AND tenant_id = $3::uuid"
TTV = "gold_aa_internal.tenant_tour_versions"


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == v1_tours.T2_VERSION_WRITE_FLAG else "false"


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block."""

    def __init__(self, conn, as_role=None):
        self.conn, self.as_role = conn, as_role

    @asynccontextmanager
    async def acquire(self, timeout=None):
        if self.as_role:
            await self.conn.execute(f"SET LOCAL ROLE {self.as_role}")
        try:
            yield self.conn
        finally:
            if self.as_role:
                await self.conn.execute("RESET ROLE")


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _version(conn, tid, pt, number, status="approved", name="v"):
    return await conn.fetchval(
        f"INSERT INTO {TTV} (tenant_id, published_tour_id, version_number, rewritten_content, status) "
        "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id", tid, pt, number, json.dumps({"name": name}), status)


async def _patch(conn, tid, version_id, flag_on, action, content=None):
    body = v1_tours.VersionActionRequest(action=action, edited_content=content, edited_by="aa859-test")
    return await v1_tours.update_version(str(version_id), body, _request(conn, flag_on), tenant={"sub": str(tid)})


async def _rows(conn, pt):
    return await conn.fetch(f"SELECT id, tenant_id, version_number, parent_version_id, status, edit_source, "
                            f"edited_by_user, rewritten_content FROM {TTV} WHERE published_tour_id = $1 "
                            "ORDER BY tenant_id, version_number", pt)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await install_ttv_live_standin(conn)
        pt = await seed_published_tour(conn)
        a, b = uuid.uuid4(), uuid.uuid4()
        a1 = await _version(conn, a, pt, 1, name="A v1")
        a2 = await _version(conn, a, pt, 2, status="ai_generated", name="A v2")
        b1 = await _version(conn, b, pt, 1, name="B v1")
        yield SimpleNamespace(conn=conn, pt=pt, a=a, b=b, a1=a1, a2=a2, b1=b1)
    finally:
        await tr.rollback()
        await conn.close()


# ── own rows ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_standin_is_live_shape_under_rls(ctx):
    """Premise: RLS on, aa_app_user holds exactly SELECT/INSERT/UPDATE (229), the live UNIQUE exists."""
    c = ctx.conn
    assert await c.fetchval(f"SELECT relrowsecurity AND NOT relforcerowsecurity FROM pg_class "
                            f"WHERE oid = '{TTV}'::regclass")
    privs = {p: await c.fetchval("SELECT has_table_privilege($1, $2, $3)", TENANT_ROLE, TTV, p)
             for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
    assert privs == {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}
    assert await c.fetchval("SELECT count(*) FROM pg_constraint WHERE conrelid = $1::regclass AND contype = 'u' "
                            "AND conname = 'tenant_tour_versions_tenant_id_published_tour_id_version_nu_key'",
                            TTV) == 1


@pytest.mark.asyncio
async def test_edit_on_tenant_pool_inserts_the_next_version(ctx):
    with capture_logs() as logs:
        out = await _patch(ctx.conn, ctx.a, ctx.a1, True, "edit", {"name": "A edited"})
    assert out["status"] == "edited" and out["version_number"] == 3   # MAX over the tour for A (2) + 1
    new = await ctx.conn.fetchrow(f"SELECT * FROM {TTV} WHERE id = $1", uuid.UUID(out["new_version_id"]))
    assert new["tenant_id"] == ctx.a and new["published_tour_id"] == ctx.pt
    assert new["parent_version_id"] == ctx.a1 and new["status"] == "pending"
    assert new["edit_source"] == "tenant_edit" and new["edited_by_user"] == "aa859-test"
    assert json.loads(new["rewritten_content"]) == {"name": "A edited"}
    assert new["edited_at"] is not None
    lines = [e for e in logs if e["event"] == "aa859_t2_version_write"]
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True and lines[0]["action"] == "edit"


@pytest.mark.parametrize("action,status", [("approve", "approved"), ("reject", "rejected")])
@pytest.mark.asyncio
async def test_status_change_on_tenant_pool(ctx, action, status):
    out = await _patch(ctx.conn, ctx.a, ctx.a2, True, action)
    assert out == {"status": status, "version_id": str(ctx.a2)}
    row = await ctx.conn.fetchrow(f"SELECT status, edited_at FROM {TTV} WHERE id = $1", ctx.a2)
    assert row["status"] == status and row["edited_at"] is not None


# ── another tenant ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action,content", [("edit", {"name": "B was here"}), ("approve", None),
                                            ("reject", None)])
@pytest.mark.asyncio
async def test_foreign_version_is_404_and_nothing_changes(ctx, action, content):
    before = await _rows(ctx.conn, ctx.pt)
    with pytest.raises(HTTPException) as exc:
        await _patch(ctx.conn, ctx.b, ctx.a2, True, action, content)
    assert exc.value.status_code == 404
    assert await _rows(ctx.conn, ctx.pt) == before


@pytest.mark.asyncio
async def test_rls_holds_when_the_own_row_where_is_forgotten(ctx, monkeypatch):
    """Through the route: only the own-row SELECT is patched. The status UPDATE is never reached on the
    tenant pool (the SELECT already 404s), so its own forgotten WHERE is tested directly below."""
    assert OWN_FILTER in v1_tours._VERSION_OWN_ROW_SQL
    monkeypatch.setattr(v1_tours, "_VERSION_OWN_ROW_SQL", v1_tours._VERSION_OWN_ROW_SQL.replace(OWN_FILTER, FORGOT))
    before = await _rows(ctx.conn, ctx.pt)
    for action, content in (("reject", None), ("edit", {"name": "B was here"})):
        with pytest.raises(HTTPException) as exc:
            await _patch(ctx.conn, ctx.b, ctx.a2, True, action, content)
        assert exc.value.status_code == 404, f"tenant pool let B {action} A's version"
    assert await _rows(ctx.conn, ctx.pt) == before

    leaked = await _patch(ctx.conn, ctx.b, ctx.a2, False, "reject")
    assert leaked["status"] == "rejected", "sanity: on the admin pool (not RLS-filtered) the patched SELECT finds A"
    # ...and only the UPDATE's own (unpatched) tenant filter keeps A's row as it was.
    assert await _rows(ctx.conn, ctx.pt) == before


@pytest.mark.asyncio
async def test_rls_filters_the_status_update_when_its_where_is_forgotten(ctx):
    """The patched _VERSION_STATUS_UPDATE_SQL itself, as aa_app_user with tenant B's GUC, on A's version:
    UPDATE 0 and A unchanged. Mutation: the same statement as the owner (RLS exempt) is UPDATE 1."""
    assert UPDATE_FILTER in v1_tours._VERSION_STATUS_UPDATE_SQL
    patched = v1_tours._VERSION_STATUS_UPDATE_SQL.replace(UPDATE_FILTER, "AND $3::uuid IS NOT NULL")
    c = ctx.conn
    before = await c.fetchrow(f"SELECT status, edited_at FROM {TTV} WHERE id = $1", ctx.a2)
    await c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
    await c.fetchval("SELECT set_config('app.tenant_id', $1, true)", str(ctx.b))
    try:
        assert await c.execute(patched, "rejected", ctx.a2, ctx.b) == "UPDATE 0"
    finally:
        await c.execute("RESET ROLE")
    assert await c.fetchrow(f"SELECT status, edited_at FROM {TTV} WHERE id = $1", ctx.a2) == before

    assert await c.execute(patched, "rejected", ctx.a2, ctx.b) == "UPDATE 1", "sanity: owner is RLS exempt"
    assert await c.fetchval(f"SELECT status FROM {TTV} WHERE id = $1", ctx.a2) == "rejected"


@pytest.mark.asyncio
async def test_tenant_pool_cannot_write_a_version_for_another_tenant(ctx, monkeypatch):
    """Even a route bug that put another tenant's id into the INSERT is refused by WITH CHECK."""
    own = "VALUES ($1::uuid, $2::uuid, $3,"
    assert own in v1_tours._VERSION_EDIT_INSERT_SQL
    monkeypatch.setattr(v1_tours, "_VERSION_EDIT_INSERT_SQL", v1_tours._VERSION_EDIT_INSERT_SQL.replace(
        own, f"VALUES ('{ctx.b}'::uuid, $2::uuid, $3 + 0 * length($1::text),"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        await _patch(ctx.conn, ctx.a, ctx.a1, True, "edit", {"name": "smuggled"})


# ── concurrency (committed rows, two real connections) ────────────────────────

class _SessionRolePool:
    """A tenant pool of one dedicated connection that runs as aa_app_user for the block."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        await self.conn.execute(f"SET ROLE {TENANT_ROLE}")
        try:
            yield self.conn
        finally:
            await self.conn.execute("RESET ROLE")


def _tenant_request(conn):
    state = SimpleNamespace(pool=None, tenant_pool=_SessionRolePool(conn), redis=_Redis(True))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _wait_until_blocked(admin, pids, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        n = await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = ANY($1::int[]) "
                                 "AND wait_event_type = 'Lock'", pids)
        if n == len(pids):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the two edits never both waited on a lock")


async def _concurrent_edits():
    """Two edits of the same version, interleaved: a blocker holds SHARE on the table (blocks INSERT, not
    SELECT), so without the advisory lock both read the same MAX before either inserts. Returns the two
    outcomes (dict or HTTPException) and the numbers stored."""
    admin = await asyncpg.connect(DSN)
    c1, c2 = await asyncpg.connect(DSN), await asyncpg.connect(DSN)
    pt, blocker, tasks = None, None, []
    try:
        async with admin.transaction():
            await install_ttv_live_standin(admin)
            pt = await seed_published_tour(admin, "AA-859 concurrent edit")
            tid = uuid.uuid4()
            v1 = await _version(admin, tid, pt, 1)
        blocker = admin.transaction()
        await blocker.start()
        await admin.execute(f"LOCK TABLE {TTV} IN SHARE MODE")
        body = v1_tours.VersionActionRequest(action="edit", edited_content={"name": "concurrent"})

        async def edit(conn):
            try:
                return await v1_tours.update_version(str(v1), body, _tenant_request(conn), tenant={"sub": str(tid)})
            except HTTPException as exc:
                return exc

        tasks += [asyncio.create_task(edit(c1)), asyncio.create_task(edit(c2))]
        await _wait_until_blocked(admin, [c1.get_server_pid(), c2.get_server_pid()])
        await blocker.rollback()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        numbers = [r["version_number"] for r in await admin.fetch(
            f"SELECT version_number FROM {TTV} WHERE published_tour_id = $1 ORDER BY 1", pt)]
        return outcomes, numbers
    finally:
        # A failed wait (or a wait_for timeout) leaves the blocker transaction open: roll it back first, or the
        # DROP / DELETE below run inside it and admin.close() undoes them, leaving the live-shape stand-in
        # committed in the CI DB for every later test.
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if blocker is not None and admin.is_in_transaction():
            await blocker.rollback()
        for c in (c1, c2):
            await c.close()
        await admin.execute(f"DROP TABLE IF EXISTS {TTV}")
        if pt is not None:
            await admin.execute("DELETE FROM gold_aa_internal.published_tours WHERE id = $1", pt)
        await admin.close()


@pytest.mark.asyncio
async def test_concurrent_edits_get_consecutive_numbers():
    outcomes, numbers = await _concurrent_edits()
    assert all(isinstance(o, dict) and o["status"] == "edited" for o in outcomes), outcomes
    assert sorted(o["version_number"] for o in outcomes) == [2, 3]
    assert numbers == [1, 2, 3]


@pytest.mark.asyncio
async def test_without_the_lock_the_race_is_a_409_never_a_silent_duplicate(monkeypatch):
    """Mutation check: the advisory lock is what serialises the edits. Without it both read MAX = 1; the
    UNIQUE backstop turns the second INSERT into a 409 (not a 500, not two versions numbered 2)."""
    monkeypatch.setattr(v1_tours, "_VERSION_EDIT_LOCK_SQL", "SELECT $1::int, $2::text, $3::text")
    outcomes, numbers = await _concurrent_edits()
    ok = [o for o in outcomes if isinstance(o, dict)]
    conflicts = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(ok) == 1 and ok[0]["version_number"] == 2
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    assert conflicts[0].detail == v1_tours.VERSION_CONFLICT_DETAIL
    assert numbers == [1, 2]
