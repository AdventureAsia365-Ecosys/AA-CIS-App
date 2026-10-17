"""AA-859 W9 (PR-I) — integration status / connect / test-connection on the tenant pool, against real
Postgres as aa_app_user under 237's policy (FOR ALL, USING = WITH CHECK = own tenant) and 241's column
grants (INSERT tenant_id, integration_type, config, secret_key, connected_at; UPDATE config, secret_key,
updated_at, last_verified_at, last_verify_error).

  - GET status: a forgotten WHERE gives tenant B nothing of tenant A's on the tenant pool; the admin pool
    (owner, exempt) leaks it, which proves the check can fail.
  - POST connect: the route's UPSERT inserts, then updates, only the own row (connected_at kept); an
    UPSERT carrying another tenant's id is refused by WITH CHECK (also on the conflict path).
  - POST test: success / failure land on the own row only, also with a forgotten WHERE; a row removed
    between the two blocks is a 404.
  - A write of a column outside the 241 grant (in the UPSERT's DO UPDATE SET or the verify UPDATE) is
    42501 on the tenant pool and works on the admin pool, so the route SQL stays inside the grant.

Harness as in test_aa817_tenant_pool_routes.py: the "tenant pool" is the test connection with SET LOCAL
ROLE aa_app_user; everything runs in one transaction that is rolled back. Secrets Manager and the remote
HTTP call are mocked.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import v1_integrations as integ_mod  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
FORGOT = "$1::uuid IS NOT NULL"
WP_CREDS = {"wp_url": "https://blog.example.com", "username": "u", "app_password": "p"}


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on and key == integ_mod.INTEGRATION_CONNECT_FLAG else "false"


class _OneConnPool:
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


async def _tenant(conn):
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                       tid, f"AA-859 W9 {tid.hex[:8]}", f"aa859w9-{tid.hex[:8]}")
    return tid


async def _seed(conn, tid, kind="wordpress"):
    config = {"site_url": f"https://{tid.hex[:8]}.example.com"} if kind == "wordpress" else {"page_id": tid.hex}
    await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                       "connected_at) VALUES ($1, $2, $3::jsonb, $4, now() - interval '1 day')",
                       tid, kind, json.dumps(config), f"acp/x/{tid}/{kind}")


async def _row(conn, tid, kind="wordpress"):
    return await conn.fetchrow("SELECT config, secret_key, connected_at, last_verified_at, last_verify_error "
                               "FROM shared.tenant_integrations WHERE tenant_id = $1 AND integration_type = $2",
                               tid, kind)


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        a, b = await _tenant(conn), await _tenant(conn)
        yield SimpleNamespace(conn=conn, a=a, b=b)
    finally:
        await tr.rollback()
        await conn.close()


def _wp_body(url="https://blog.example.com"):
    return integ_mod.SaveWordPressRequest(wp_url=url, username="u", app_password="p")


async def _connect_wp(c, tid, flag_on, url="https://blog.example.com"):
    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        return await integ_mod.save_wordpress(_wp_body(url), _request(c.conn, flag_on), tenant={"sub": str(tid)})


def _http(status=200, body='{"id": 1}', on_enter=None):
    """aiohttp.ClientSession() replacement; `on_enter` (async) runs when the remote GET is entered."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"content-type": "application/json"}
    resp.text = AsyncMock(return_value=body)

    async def enter(*_a):
        if on_enter:
            await on_enter()
        return resp

    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(side_effect=enter)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_ctx)


async def _test_wp(c, tid, flag_on, status=200, on_enter=None):
    with patch.object(integ_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(integ_mod.aiohttp, "ClientSession", _http(status, on_enter=on_enter)):
        return await integ_mod.test_wordpress(_request(c.conn, flag_on), tenant={"sub": str(tid)})


# ── premise ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_241_column_grants_are_applied_in_this_db(ctx):
    """conftest swallows migration errors (lesson AA-741): without 241 every write below would be a 42501
    for the wrong reason."""
    for col, priv in (("connected_at", "INSERT"), ("last_verified_at", "UPDATE"), ("updated_at", "UPDATE")):
        assert await ctx.conn.fetchval("SELECT has_column_privilege($1, 'shared.tenant_integrations', $2, $3)",
                                       TENANT_ROLE, col, priv), (col, priv)
    assert not await ctx.conn.fetchval(
        "SELECT has_column_privilege($1, 'shared.tenant_integrations', 'connected_at', 'UPDATE')", TENANT_ROLE)


# ── GET status ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["wordpress", "facebook"])
@pytest.mark.asyncio
async def test_status_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch, kind):
    await _seed(ctx.conn, ctx.a, kind)
    get = integ_mod.get_wordpress_status if kind == "wordpress" else integ_mod.get_facebook_status
    own = await get(_request(ctx.conn, True), tenant={"sub": str(ctx.a)})
    assert own["connected"] is True
    monkeypatch.setattr(integ_mod, "_STATUS_SQL", integ_mod._STATUS_SQL.replace("tenant_id = $1::uuid", FORGOT))
    other = await get(_request(ctx.conn, True), tenant={"sub": str(ctx.b)})
    assert other["connected"] is False, "tenant B saw tenant A's integration on the tenant pool"
    leaked = await get(_request(ctx.conn, False), tenant={"sub": str(ctx.b)})
    assert leaked["connected"] is True, "sanity: the admin pool is not RLS-filtered"


# ── POST connect ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_on_tenant_pool_inserts_then_updates_only_own_row(ctx):
    await _seed(ctx.conn, ctx.b)
    b_before = await _row(ctx.conn, ctx.b)

    first = await _connect_wp(ctx, ctx.a, True, "https://one.example.com")
    assert first["connected"] is True and first["site_url"] == "https://one.example.com"
    row = await _row(ctx.conn, ctx.a)
    assert row["secret_key"] == f"acp/cms/{ctx.a}" and row["connected_at"] is not None

    await ctx.conn.execute("UPDATE shared.tenant_integrations SET connected_at = connected_at - interval '1 day' "
                           "WHERE tenant_id = $1", ctx.a)   # so a rewrite of connected_at would show
    connected_at = (await _row(ctx.conn, ctx.a))["connected_at"]
    second = await _connect_wp(ctx, ctx.a, True, "https://two.example.com")
    assert second["site_url"] == "https://two.example.com"
    assert (await _row(ctx.conn, ctx.a))["connected_at"] == connected_at, "ON CONFLICT keeps connected_at"

    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        fb = await integ_mod.save_facebook(integ_mod.SaveFacebookRequest(page_id="123", page_access_token="t"),
                                           _request(ctx.conn, True), tenant={"sub": str(ctx.a)})
    assert fb["page_id"] == "123"
    assert await _row(ctx.conn, ctx.b) == b_before, "the other tenant's row is untouched"


@pytest.mark.parametrize("a_has_row", [False, True], ids=["insert_path", "conflict_path"])
@pytest.mark.asyncio
async def test_connect_upsert_for_a_foreign_tenant_is_refused_by_with_check(ctx, monkeypatch, a_has_row):
    """The route's UPSERT with tenant A's id in VALUES while app.tenant_id is B (a forgotten / wrong
    tenant parameter): WITH CHECK refuses it, before conflict arbitration, so A's row is never reached."""
    if a_has_row:
        await _seed(ctx.conn, ctx.a)
    a_before = await _row(ctx.conn, ctx.a)
    monkeypatch.setattr(integ_mod, "_CONNECT_UPSERT_SQL",
                        integ_mod._CONNECT_UPSERT_SQL.replace(
                            "VALUES ($1::uuid,", f"VALUES (COALESCE('{ctx.a}'::uuid, $1::uuid),"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        await _connect_wp(ctx, ctx.b, True)
    assert await _row(ctx.conn, ctx.a) == a_before
    assert await _row(ctx.conn, ctx.b) is None

    leaked = await _connect_wp(ctx, ctx.b, False)   # same mutation on the admin pool
    assert leaked["connected"] is True, "sanity: the owner (admin pool) is not checked"
    assert (await _row(ctx.conn, ctx.a))["secret_key"] == f"acp/cms/{ctx.b}"


@pytest.mark.parametrize("extra", ["connected_at = now()", "integration_type = 'facebook'",
                                   "tenant_id = EXCLUDED.tenant_id", "created_at = now()"])
@pytest.mark.asyncio
async def test_connect_upsert_setting_a_column_outside_the_grant_is_42501(ctx, monkeypatch, extra):
    await _seed(ctx.conn, ctx.a)
    monkeypatch.setattr(integ_mod, "_CONNECT_UPSERT_SQL",
                        integ_mod._CONNECT_UPSERT_SQL.replace("updated_at = now()", f"updated_at = now(), {extra}"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
        await _connect_wp(ctx, ctx.a, True)
    assert (await _connect_wp(ctx, ctx.a, False))["connected"] is True, "sanity: the owner may"


# ── POST test ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_test_on_tenant_pool_records_only_on_own_row(ctx):
    await _seed(ctx.conn, ctx.a)
    await _seed(ctx.conn, ctx.b)
    b_before = await _row(ctx.conn, ctx.b)

    ok = await _test_wp(ctx, ctx.a, True)
    assert ok["success"] is True and ok["last_verified_at"] is not None and ok["last_verify_error"] is None
    verified_at = (await _row(ctx.conn, ctx.a))["last_verified_at"]
    assert verified_at is not None

    failed = await _test_wp(ctx, ctx.a, True, status=401)
    assert failed["success"] is False and failed["last_verify_error"] == "Wrong username or application password"
    assert (await _row(ctx.conn, ctx.a))["last_verified_at"] == verified_at, "a failed retest keeps it"
    assert await _row(ctx.conn, ctx.b) == b_before


@pytest.mark.parametrize("sql_name,status", [("_VERIFY_OK_SQL", 200), ("_VERIFY_FAILED_SQL", 401)])
@pytest.mark.asyncio
async def test_connection_test_rls_holds_when_the_update_where_is_forgotten(ctx, monkeypatch, sql_name, status):
    await _seed(ctx.conn, ctx.a)
    await _seed(ctx.conn, ctx.b)
    a_before, b_before = await _row(ctx.conn, ctx.a), await _row(ctx.conn, ctx.b)
    monkeypatch.setattr(integ_mod, sql_name, getattr(integ_mod, sql_name).replace("tenant_id = $1::uuid", FORGOT))
    await _test_wp(ctx, ctx.b, True, status=status)
    assert await _row(ctx.conn, ctx.a) == a_before, "tenant B's verify UPDATE reached tenant A's row"
    assert await _row(ctx.conn, ctx.b) != b_before, "the own row was recorded"

    await _test_wp(ctx, ctx.b, False, status=status)
    assert await _row(ctx.conn, ctx.a) != a_before, "sanity: the admin pool is not RLS-filtered"


@pytest.mark.asyncio
async def test_connection_test_block_one_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    """Block 1 with a forgotten WHERE: on the tenant pool RLS hides A's row, so Secrets Manager is never
    asked. The 404 alone cannot tell this apart from a leak (block 2's correct WHERE also 404s for B),
    so the proof is whether _get_secret was called, and with whose key."""
    await _seed(ctx.conn, ctx.a)
    monkeypatch.setattr(integ_mod, "_TEST_SELECT_SQL", integ_mod._TEST_SELECT_SQL.replace("tenant_id = $1::uuid",
                                                                                          FORGOT))

    async def run(flag_on):
        get_secret = MagicMock(return_value=dict(WP_CREDS))
        with patch.object(integ_mod, "_get_secret", get_secret), \
                patch.object(integ_mod.aiohttp, "ClientSession", _http()):
            with pytest.raises(HTTPException) as exc:
                await integ_mod.test_wordpress(_request(ctx.conn, flag_on), tenant={"sub": str(ctx.b)})
        assert exc.value.status_code == 404
        return get_secret

    on_tenant_pool = await run(True)
    assert not on_tenant_pool.called, "tenant B read tenant A's secret_key on the tenant pool"

    on_admin_pool = await run(False)
    assert [c.args for c in on_admin_pool.call_args_list] == [(f"acp/x/{ctx.a}/wordpress",)], \
        "sanity: the same mutation on the admin pool leaks A's row into block 1"


@pytest.mark.parametrize("sql_name,status,extra", [
    ("_VERIFY_OK_SQL", 200, "connected_at = now()"),
    ("_VERIFY_FAILED_SQL", 401, "secret_key = secret_key, config = config, tenant_id = tenant_id"),
])
@pytest.mark.asyncio
async def test_verify_update_of_a_column_outside_the_grant_is_42501(ctx, monkeypatch, sql_name, status, extra):
    await _seed(ctx.conn, ctx.a)
    monkeypatch.setattr(integ_mod, sql_name,
                        getattr(integ_mod, sql_name).replace("updated_at = now()", f"updated_at = now(), {extra}"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="permission denied"):
        await _test_wp(ctx, ctx.a, True, status=status)
    assert "success" in await _test_wp(ctx, ctx.a, False, status=status), "sanity: the owner may"


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_connection_test_row_removed_between_blocks_is_404(ctx, flag_on):
    """A disconnect between block 1 and block 2 (here: while the remote GET runs, no tenant connection
    held): the verify UPDATE matches nothing -> 404 "not connected", not a 500 on a None row."""
    await _seed(ctx.conn, ctx.a)

    async def disconnect():
        await ctx.conn.execute("DELETE FROM shared.tenant_integrations WHERE tenant_id = $1", ctx.a)

    with pytest.raises(HTTPException) as exc:
        await _test_wp(ctx, ctx.a, flag_on, on_enter=disconnect)
    assert exc.value.status_code == 404 and "not connected" in exc.value.detail
