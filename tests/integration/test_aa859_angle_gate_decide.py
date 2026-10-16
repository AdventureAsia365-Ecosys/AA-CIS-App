"""AA-859 W3 (plan PR-D3) — POST /v1/angle-gate/requests/{id}/choose and /reopen against real Postgres, as
aa_app_user under the live policies (229 on angle_gate_request, 241 on angle_gate_option through its parent).

  - flag on: A chooses / reopens / re-chooses its own request on the tenant pool; the response is the main-pool
    re-read after commit; B on A's request is a 404 for both routes, A's rows untouched.
  - forgotten WHERE: with the tenant filters removed from the request statements, B still cannot reach A's
    request on the tenant pool (RLS), while the main pool (owner, exempt) does: proof each check can fail.
  - the 241 option policy, run directly (the route's request lock already 404s for B, so it cannot test the
    option policy): as aa_app_user under B's GUC, _OPTION_CHOOSE_SQL on A's request updates 0 rows; the
    mutation check runs the same statement with RLS disabled in the transaction and must update rows.
  - two concurrent chooses on one request (two real connections): one 200, one 409 from the lock step, never
    two approvals; without the FOR UPDATE (mutation) the loser is caught only by the approve backstop.

Same harness as test_aa817_tenant_pool_routes.py: the "tenant pool" is the test connection with SET LOCAL ROLE
aa_app_user (CI's aa_app_user has no LOGIN); everything runs in one rolled-back transaction, except the
concurrency tests, which need committed rows on two connections and delete them in `finally`.
"""
import asyncio
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
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import v1_angle_gate as ag  # noqa: E402
from services.acp_angle_gate import service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
FORGOT = "$2::uuid IS NOT NULL"
# The live CHECK (migration 124, which needs pgvector and does not apply in CI) allows 'reusable'.
LIVE_STATUS_CHECK = """
    ALTER TABLE acp_shared.angle_gate_request DROP CONSTRAINT IF EXISTS angle_gate_request_status_check;
    ALTER TABLE acp_shared.angle_gate_request ADD CONSTRAINT angle_gate_request_status_check
        CHECK (status = ANY (ARRAY['pending_goal', 'pending_choice', 'approved', 'reusable']))
"""


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == ag.ANGLE_GATE_DECIDE_FLAG else None


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block."""

    def __init__(self, conn, as_role=None, local=True):
        self.conn, self.as_role, self.local = conn, as_role, local

    @asynccontextmanager
    async def acquire(self, timeout=None):
        if self.as_role:
            await self.conn.execute(f"SET {'LOCAL ' if self.local else ''}ROLE {self.as_role}")
        try:
            yield self.conn
        finally:
            if self.as_role:
                await self.conn.execute("RESET ROLE")


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _tenant(conn):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"AA-859 W3 {tid.hex[:8]}", f"aa859w3-{tid.hex[:8]}")
    return tid


async def _angle_request(conn, tid, status="pending_choice", chosen=None):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, goal, status) "
                              "VALUES ($1, 'aa859-atom', 'blog', 'promotion', $2) RETURNING request_id", tid, status)
    for idx in range(3):
        await conn.execute("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, "
                           "formula_fit, best_final_style, chosen) VALUES ($1, $2, $3, 'w', 'f', 's', $4)",
                           req, idx, f"Angle {idx}", idx == chosen)
    return req


async def _state(conn, req):
    status = await conn.fetchval("SELECT status FROM acp_shared.angle_gate_request WHERE request_id = $1", req)
    rows = await conn.fetch("SELECT chosen FROM acp_shared.angle_gate_option WHERE request_id = $1 ORDER BY idx", req)
    return status, [r["chosen"] for r in rows]


async def _choose(conn, tid, req, idx, flag_on=True):
    return await ag.choose(req, ag.ChooseBody(idx=idx), _request(conn, flag_on), tenant={"sub": str(tid)})


async def _reopen(conn, tid, req, flag_on=True):
    return await ag.reopen(req, _request(conn, flag_on), tenant={"sub": str(tid)})


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(LIVE_STATUS_CHECK)
        a, b = await _tenant(conn), await _tenant(conn)
        yield SimpleNamespace(conn=conn, a=a, b=b)
    finally:
        await tr.rollback()
        await conn.close()


# ── preconditions ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_241_is_applied_so_the_option_tests_can_fail(ctx):
    """conftest swallows migration errors (lesson AA-741): without 241 the option policy tests pass vacuously."""
    rls, force = await ctx.conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                         "WHERE oid = 'acp_shared.angle_gate_option'::regclass")
    assert rls is True and force is False
    assert await ctx.conn.fetchval("SELECT count(*) FROM pg_policies WHERE schemaname = 'acp_shared' "
                                   "AND tablename = 'angle_gate_option' AND policyname = 'tenant_isolation'") == 1
    assert await ctx.conn.fetchval("SELECT has_column_privilege($1, 'acp_shared.angle_gate_option', 'chosen', "
                                   "'UPDATE')", TENANT_ROLE) is True


# ── the routes on the tenant pool ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_choose_reopen_rechoose_on_tenant_pool(ctx):
    req = await _angle_request(ctx.conn, ctx.a)
    with capture_logs() as logs:
        out = await _choose(ctx.conn, ctx.a, req, 1)
    assert out["status"] == "approved" and [a["chosen"] for a in out["angles"]] == [False, True, False]
    assert await _state(ctx.conn, req) == ("approved", [False, True, False])
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_choose"] == [True]

    with pytest.raises(HTTPException) as exc:  # approved is not choosable until reopened
        await _choose(ctx.conn, ctx.a, req, 2)
    assert exc.value.status_code == 409

    with capture_logs() as logs:
        out = await _reopen(ctx.conn, ctx.a, req)
    assert out["status"] == "reusable"
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_reopen"] == [True]
    with pytest.raises(HTTPException) as exc:  # a second reopen (double click) is a 409, not a silent 200
        await _reopen(ctx.conn, ctx.a, req)
    assert exc.value.status_code == 409

    out = await _choose(ctx.conn, ctx.a, req, 2)
    assert out["status"] == "approved"
    assert await _state(ctx.conn, req) == ("approved", [False, False, True])

    await _reopen(ctx.conn, ctx.a, req)
    await _choose(ctx.conn, ctx.a, req, 1)  # back to the original choice
    assert await _state(ctx.conn, req) == ("approved", [False, True, False])


@pytest.mark.asyncio
async def test_choose_missing_option_is_422_and_rolls_back(ctx):
    req = await _angle_request(ctx.conn, ctx.a)
    await ctx.conn.execute("DELETE FROM acp_shared.angle_gate_option WHERE request_id = $1 AND idx = 2", req)
    with pytest.raises(HTTPException) as exc:
        await _choose(ctx.conn, ctx.a, req, 2)
    assert exc.value.status_code == 422
    assert await _state(ctx.conn, req) == ("pending_choice", [False, False])


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "main_pool"])
@pytest.mark.asyncio
async def test_foreign_tenant_gets_404_and_changes_nothing(ctx, flag_on):
    pending = await _angle_request(ctx.conn, ctx.a)
    approved = await _angle_request(ctx.conn, ctx.a, status="approved", chosen=0)
    for call in (lambda: _choose(ctx.conn, ctx.b, pending, 1, flag_on),
                 lambda: _reopen(ctx.conn, ctx.b, approved, flag_on)):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 404
    assert await _state(ctx.conn, pending) == ("pending_choice", [False, False, False])
    assert await _state(ctx.conn, approved) == ("approved", [True, False, False])


@pytest.mark.asyncio
async def test_choose_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    req = await _angle_request(ctx.conn, ctx.a)
    for name in ("_CHOOSE_LOCK_SQL", "_REQUEST_APPROVE_SQL"):
        forgot = getattr(service, name).replace("tenant_id = $2::uuid", FORGOT)
        assert forgot != getattr(service, name)
        monkeypatch.setattr(service, name, forgot)
    with pytest.raises(HTTPException) as exc:
        await ag.choose(req, ag.ChooseBody(idx=1), _request(ctx.conn, True), tenant={"sub": str(ctx.b)})
    assert exc.value.status_code == 404, "tenant B reached tenant A's request on the tenant pool"
    assert await _state(ctx.conn, req) == ("pending_choice", [False, False, False])

    # sanity: the main pool (owner, RLS-exempt) does reach it with the WHERE forgotten. The re-read keeps its
    # own tenant filter (fetch_request), so B's response is a 404 after A's request was changed.
    with pytest.raises(service.RequestNotFoundError):
        await ag._decide(_request(ctx.conn, False), ctx.b, req, "aa859_angle_gate_choose",
                         lambda conn: service.choose_angle(conn, ctx.b, req, 1))
    assert await _state(ctx.conn, req) == ("approved", [False, True, False]), "sanity: the main pool leaks"


@pytest.mark.asyncio
async def test_reopen_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    req = await _angle_request(ctx.conn, ctx.a, status="approved", chosen=0)
    for name in ("_REOPEN_SQL", "_REQUEST_STATUS_SQL"):
        forgot = getattr(service, name).replace("tenant_id = $2::uuid", FORGOT)
        assert forgot != getattr(service, name)
        monkeypatch.setattr(service, name, forgot)
    with pytest.raises(HTTPException) as exc:
        await ag.reopen(req, _request(ctx.conn, True), tenant={"sub": str(ctx.b)})
    assert exc.value.status_code == 404, "tenant B reopened tenant A's request on the tenant pool"
    assert (await _state(ctx.conn, req))[0] == "approved"

    with pytest.raises(service.RequestNotFoundError):  # sanity, as above
        await ag._decide(_request(ctx.conn, False), ctx.b, req, "aa859_angle_gate_reopen",
                         lambda conn: service.reopen_request(conn, ctx.b, req))
    assert (await _state(ctx.conn, req))[0] == "reusable", "sanity: the main pool leaks"


# ── the 241 option policy, directly ───────────────────────────────────────────

async def _option_choose_as_tenant(conn, guc_tenant, req, disable_rls=False):
    """_OPTION_CHOOSE_SQL as aa_app_user under `guc_tenant`'s GUC, in a savepoint that is rolled back."""
    tr = conn.transaction()
    await tr.start()
    try:
        if disable_rls:  # as the owner (superuser here), before switching role
            await conn.execute("ALTER TABLE acp_shared.angle_gate_option DISABLE ROW LEVEL SECURITY")
        await conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(guc_tenant))
        status = await conn.execute(service._OPTION_CHOOSE_SQL, req, 1)
        await conn.execute("RESET ROLE")
        after = await _state(conn, req)
        return int(status.split()[-1]), after
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_option_policy_blocks_a_foreign_tenant(ctx):
    req_a = await _angle_request(ctx.conn, ctx.a, status="approved", chosen=0)
    updated, after = await _option_choose_as_tenant(ctx.conn, ctx.b, req_a)
    assert updated == 0, "tenant B's GUC updated tenant A's angle_gate_option rows"
    assert after == ("approved", [True, False, False])

    updated, after = await _option_choose_as_tenant(ctx.conn, ctx.a, req_a)  # positive control: the owner tenant
    assert updated == 3 and after == ("approved", [False, True, False])


@pytest.mark.asyncio
async def test_option_policy_check_fails_without_rls(ctx):
    """Mutation check: the statement and grant are the same, only RLS is off: B's GUC now reaches A's rows,
    so the assertion of test_option_policy_blocks_a_foreign_tenant would fail."""
    req_a = await _angle_request(ctx.conn, ctx.a, status="approved", chosen=0)
    updated, after = await _option_choose_as_tenant(ctx.conn, ctx.b, req_a, disable_rls=True)
    assert updated == 3 and after == ("approved", [False, True, False])
    assert (await ctx.conn.fetchrow("SELECT relrowsecurity FROM pg_class "
                                    "WHERE oid = 'acp_shared.angle_gate_option'::regclass"))[0] is True


# ── two concurrent chooses on two real connections ────────────────────────────

# The option check sleeps while the winner holds the request lock, so both requests are in flight at once.
SLOW_OPTION_EXISTS_SQL = ("SELECT 1 FROM acp_shared.angle_gate_option, pg_sleep(0.5) "
                          "WHERE request_id = $1 AND idx = $2")


async def _race(monkeypatch, lock_sql=None):
    monkeypatch.setattr(service, "_OPTION_EXISTS_SQL", SLOW_OPTION_EXISTS_SQL)
    if lock_sql is not None:
        monkeypatch.setattr(service, "_CHOOSE_LOCK_SQL", lock_sql)
    seed = await asyncpg.connect(DSN)
    conns = [await asyncpg.connect(DSN) for _ in range(4)]  # tenant + main connection per request
    tid = uuid.uuid4()
    try:
        await seed.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) "
                           "VALUES ($1, $2, $3, 3)", tid, f"AA-859 W3 race {tid.hex[:8]}", f"aa859w3r-{tid.hex[:8]}")
        req = await _angle_request(seed, tid)  # committed: both connections must see it

        def request(i):
            state = SimpleNamespace(pool=_OneConnPool(conns[2 * i + 1]),
                                    tenant_pool=_OneConnPool(conns[2 * i], TENANT_ROLE, local=False),
                                    redis=_Redis(True))
            return SimpleNamespace(app=SimpleNamespace(state=state))

        async def choose(i, idx):
            try:
                return await ag.choose(req, ag.ChooseBody(idx=idx), request(i), tenant={"sub": str(tid)})
            except HTTPException as exc:
                return exc

        with capture_logs() as logs:
            results = await asyncio.wait_for(asyncio.gather(choose(0, 0), choose(1, 2)), timeout=30)
        return results, await _state(seed, req), logs
    finally:
        await seed.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)
        await seed.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        for conn in conns + [seed]:
            await conn.close()


def _split(results):
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, HTTPException)]
    return wins, losses


@pytest.mark.asyncio
async def test_concurrent_choose_second_waits_for_the_lock_and_gets_409(monkeypatch):
    results, (status, chosen), logs = await _race(monkeypatch)
    wins, losses = _split(results)
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].status_code == 409
    assert "is status='approved'" in losses[0].detail, "the loser saw the winner's status at the lock step"
    winner_idx = [i for i, a in enumerate(wins[0]["angles"]) if a["chosen"]]
    assert status == "approved" and chosen == [i == winner_idx[0] for i in range(3)] and sum(chosen) == 1
    assert not [e for e in logs if e["event"] == "aa859_angle_gate_choose_status_changed"]
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_choose"] == [True, True]


@pytest.mark.asyncio
async def test_concurrent_choose_without_the_lock_is_caught_only_by_the_backstop(monkeypatch):
    """Mutation check: without FOR UPDATE both requests read 'pending_choice'; the loser's approve UPDATE then
    finds no choosable row and the backstop answers 409 (its option change rolled back). The lock-step
    assertion of the test above fails here, so the FOR UPDATE is what that test proves."""
    unlocked = service._CHOOSE_LOCK_SQL.replace("FOR UPDATE", "")
    assert unlocked != service._CHOOSE_LOCK_SQL
    results, (status, chosen), logs = await _race(monkeypatch, lock_sql=unlocked)
    wins, losses = _split(results)
    assert len(wins) == 1 and len(losses) == 1 and losses[0].status_code == 409
    assert "changed while choosing" in losses[0].detail
    assert len([e for e in logs if e["event"] == "aa859_angle_gate_choose_status_changed"]) == 1
    assert status == "approved" and sum(chosen) == 1
