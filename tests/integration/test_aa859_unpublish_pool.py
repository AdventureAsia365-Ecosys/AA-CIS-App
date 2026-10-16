"""AA-859 W4 — DELETE /v1/publish-log/{publish_id} (unpublish) on the tenant pool, against real Postgres as
aa_app_user under the live policies (229 publish_log, 237 shared.tenant_integrations). The remote delete and
Secrets Manager are mocked.

  - own row: flipped to 'unpublished' by tenant:<A> through the tenant pool, audit row written on MAIN;
  - tenant B on A's publish_id: 404, A's row stays 'published', no remote call;
  - forgotten WHERE on the publish_log SELECT + UPDATE: B still gets 404 on the tenant pool (RLS), while the
    admin pool (owner, exempt) lets B flip A's row, which proves the check can fail;
  - forgotten WHERE on the tenant_integrations SELECT: B (not connected) does not borrow A's connection;
  - double unpublish: a row another request flipped while this one was on the remote delete makes the
    status-guarded UPDATE 0 rows (404); a second unpublish after the first is 404 at the first block.

Same harness as test_aa817_tenant_pool_routes.py (imported): one connection, SET LOCAL ROLE aa_app_user for
the tenant-pool blocks, everything rolled back.
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import test_aa817_tenant_pool_routes as routes  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from test_aa817_tenant_pool_routes import FORGOT, _integration, _published_piece, _request  # noqa: E402

ctx = routes.ctx  # the shared fixture: two tenants, one connection, everything rolled back

WP_CREDS = {"wp_url": "https://tenant-a.example.com", "username": "u", "app_password": "p"}


async def _publish_row(conn, tid):
    piece = await _published_piece(conn, tid)
    publish_id = await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, 'blog', 'published', '42', 'https://tenant-a.example.com/p/42/', now()) "
        "RETURNING publish_id", piece, tid)
    return piece, publish_id


async def _status(conn, publish_id):
    return await conn.fetchrow("SELECT status, unpublished_by FROM acp_shared.publish_log WHERE publish_id = $1",
                               publish_id)


async def _unpublish(c, tid, publish_id, flag_on, remote=None):
    remote = remote or AsyncMock(return_value="trashed")
    with patch.object(pub_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(pub_mod, "_delete_remote", remote):
        return await pub_mod.unpublish(publish_id, _request(c.conn, flag_on), tenant={"sub": str(tid)})


@pytest.mark.asyncio
async def test_unpublish_on_tenant_pool_flips_own_row_and_audits_on_main(ctx):
    piece, publish_id = await _publish_row(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.a)
    out = await _unpublish(ctx, ctx.a, publish_id, flag_on=True)
    assert out["status"] == "unpublished" and out["remote"] == "trashed"
    row = await _status(ctx.conn, publish_id)
    assert row["status"] == "unpublished" and row["unpublished_by"] == f"tenant:{ctx.a}"
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                                   "AND action = 'content_piece.unpublished' AND resource_id = $2",
                                   str(ctx.a), str(piece)) == 1


@pytest.mark.asyncio
async def test_other_tenant_gets_404_and_the_row_stays_published(ctx):
    _, publish_id = await _publish_row(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.b)
    remote = AsyncMock(side_effect=AssertionError("no remote call"))
    with pytest.raises(HTTPException) as exc:
        await _unpublish(ctx, ctx.b, publish_id, flag_on=True, remote=remote)
    assert exc.value.status_code == 404
    assert (await _status(ctx.conn, publish_id))["status"] == "published"


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_rls_holds_when_the_publish_log_where_is_forgotten(ctx, monkeypatch, flag_on):
    _, publish_id = await _publish_row(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.b)  # B is connected, to the same site, so only the tenant filter stops it
    for name in ("_UNPUBLISH_SELECT_SQL", "_UNPUBLISH_UPDATE_SQL"):
        sql = getattr(pub_mod, name)
        assert "tenant_id = $2::uuid" in sql
        monkeypatch.setattr(pub_mod, name, sql.replace("tenant_id = $2::uuid", FORGOT.replace("$1", "$2")))
    remote = AsyncMock(return_value="trashed")
    if flag_on:
        with pytest.raises(HTTPException) as exc:
            await _unpublish(ctx, ctx.b, publish_id, flag_on=True, remote=remote)
        assert exc.value.status_code == 404, "tenant B saw tenant A's publish_log row on the tenant pool"
        remote.assert_not_awaited()
        assert (await _status(ctx.conn, publish_id))["status"] == "published"
    else:  # sanity: the mutation is real; the owner-exempt admin pool lets B take A's post down
        await _unpublish(ctx, ctx.b, publish_id, flag_on=False, remote=remote)
        assert (await _status(ctx.conn, publish_id))["unpublished_by"] == f"tenant:{ctx.b}"


@pytest.mark.asyncio
async def test_rls_holds_when_the_integration_where_is_forgotten(ctx, monkeypatch):
    """B owns a published row but never connected WordPress; A did. B must not use A's credentials."""
    _, publish_id = await _publish_row(ctx.conn, ctx.b)
    await _integration(ctx.conn, ctx.a)
    monkeypatch.setattr(pub_mod, "_UNPUBLISH_INTEGRATION_SQL",
                        pub_mod._UNPUBLISH_INTEGRATION_SQL.replace("tenant_id = $1::uuid", FORGOT))
    with pytest.raises(HTTPException) as exc:
        await _unpublish(ctx, ctx.b, publish_id, flag_on=True,
                         remote=AsyncMock(side_effect=AssertionError("no remote call")))
    assert exc.value.status_code == 422 and "Connect WordPress" in exc.value.detail


@pytest.mark.asyncio
async def test_row_flipped_during_the_remote_delete_is_404_from_the_status_guard(ctx):
    _, publish_id = await _publish_row(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.a)

    async def concurrent_unpublish(*args):  # the other request's UPDATE lands while this one is remote
        await ctx.conn.execute("UPDATE acp_shared.publish_log SET status = 'unpublished', unpublished_at = now(), "
                               "unpublished_by = 'tenant:other-request' WHERE publish_id = $1", publish_id)
        return "already_gone"

    with pytest.raises(HTTPException) as exc:
        await _unpublish(ctx, ctx.a, publish_id, flag_on=True, remote=AsyncMock(side_effect=concurrent_unpublish))
    assert exc.value.status_code == 404
    assert (await _status(ctx.conn, publish_id))["unpublished_by"] == "tenant:other-request", "flipped once"


@pytest.mark.asyncio
async def test_second_unpublish_is_404_at_the_first_block(ctx):
    _, publish_id = await _publish_row(ctx.conn, ctx.a)
    await _integration(ctx.conn, ctx.a)
    await _unpublish(ctx, ctx.a, publish_id, flag_on=True)
    remote = AsyncMock(side_effect=AssertionError("no second remote call"))
    with pytest.raises(HTTPException) as exc:
        await _unpublish(ctx, ctx.a, publish_id, flag_on=True, remote=remote)
    assert exc.value.status_code == 404
