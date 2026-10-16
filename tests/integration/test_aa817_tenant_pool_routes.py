"""AA-817 PR-A — the other tenant-pool routes against real Postgres, as aa_app_user under the live policies
(229 for content_piece / angle_gate_request / publish_log, 237 for shared.tenant_integrations):

  - DELETE /v1/integrations/{type} (_disconnect): own row deleted, another tenant's row untouched; a
    forgotten WHERE still cannot reach another tenant (RLS); a DELETE that RLS lets remove 0 rows is a 503
    with the secret untouched and the row kept (critique C3); a tombstone failure rolls the DELETE back.
  - GET /pieces/{id} (fetch_piece), GET /v1/publish-log/pending, GET /v1/publish-log: a forgotten WHERE
    gives tenant B nothing of tenant A's on the tenant pool, while the admin pool (owner, exempt) leaks it,
    which proves each check can fail.

Same harness as test_aa817_tenant_pool_content.py: the "tenant pool" is the test connection with
SET LOCAL ROLE aa_app_user (CI's aa_app_user has no LOGIN); everything runs in one transaction that is
rolled back. Secrets Manager is mocked.
"""
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from botocore.exceptions import ClientError
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import aa544_tenant_pool as tp  # noqa: E402
from api.routers import v1_integrations as integ_mod  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from services.acp_content_writing import service  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
FORGOT = "$1::uuid IS NOT NULL"


class _Redis:
    def __init__(self, on):
        self.on = on

    async def get(self, key):
        return "true" if self.on else None


class _OneConnPool:
    """pool.acquire() over the test connection; as_role switches to the tenant role for the block."""

    def __init__(self, conn, as_role=None):
        self.conn, self.as_role = conn, as_role
        self.execute = conn.execute  # write_audit_log(pool, ...)

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
                       tid, f"AA-817 routes {tid.hex[:8]}", f"aa817r-{tid.hex[:8]}")
    return tid


async def _integration(conn, tid, kind="wordpress"):
    await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                       "connected_at) VALUES ($1, $2, '{}'::jsonb, $3, now())", tid, kind, f"acp/x/{tid}/{kind}")


async def _integrations_of(conn, tid):
    rows = await conn.fetch("SELECT integration_type FROM shared.tenant_integrations WHERE tenant_id = $1 "
                            "ORDER BY 1", tid)
    return [r["integration_type"] for r in rows]


async def _published_piece(conn, tid):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1, 'aa817-atom', 'blog', 'approved') RETURNING request_id", tid)
    opt = await conn.fetchval("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, "
                              "formula_fit, best_final_style, chosen) VALUES ($1, 0, $2, 'w', 'f', 's', true) "
                              "RETURNING option_id", req, f"Angle of {tid}")
    piece = await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, "
        "attempt_number, content_text, status, gate_ledger, repair_log, channel) "
        "VALUES ($1, $2, $3, 1, $4, 'approved', '[]'::jsonb, '[]'::jsonb, 'blog') RETURNING piece_id",
        tid, req, opt, f"Body of {tid}")
    return piece


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        # columns the queries read that the CI DB may lack (124 needs pgvector, absent here)
        await conn.execute("""
            ALTER TABLE acp_shared.content_piece
                ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid, ADD COLUMN IF NOT EXISTS channel text,
                ADD COLUMN IF NOT EXISTS seo_title text, ADD COLUMN IF NOT EXISTS meta_description text,
                ADD COLUMN IF NOT EXISTS slug text, ADD COLUMN IF NOT EXISTS route_hub_name text,
                ADD COLUMN IF NOT EXISTS route_segment_count integer,
                ADD COLUMN IF NOT EXISTS flags jsonb""")
        a, b = await _tenant(conn), await _tenant(conn)
        yield SimpleNamespace(conn=conn, a=a, b=b)
    finally:
        await tr.rollback()
        await conn.close()


async def _disconnect(c, tid, flag_on, kind="wordpress", sm=None):
    with patch.object(integ_mod, "_sm_client", return_value=sm or MagicMock()):
        return await integ_mod._disconnect(_request(c.conn, flag_on), str(tid), kind)


# ── disconnect ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disconnect_on_tenant_pool_deletes_only_own_row(ctx):
    for kind in ("wordpress", "facebook"):
        await _integration(ctx.conn, ctx.a, kind)
    await _integration(ctx.conn, ctx.b, "wordpress")
    sm = MagicMock()
    out = await _disconnect(ctx, ctx.a, flag_on=True, sm=sm)
    assert out["disconnected"] is True
    assert sm.put_secret_value.call_args.kwargs["SecretId"] == f"acp/x/{ctx.a}/wordpress"
    assert await _integrations_of(ctx.conn, ctx.a) == ["facebook"]
    assert await _integrations_of(ctx.conn, ctx.b) == ["wordpress"]
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                                   "AND action = 'integration.disconnected'", str(ctx.a)) == 1


@pytest.mark.asyncio
async def test_disconnect_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    await _integration(ctx.conn, ctx.a)
    monkeypatch.setattr(integ_mod, "_SELECT_INTEGRATION_SQL",
                        integ_mod._SELECT_INTEGRATION_SQL.replace("tenant_id = $1::uuid", FORGOT))
    monkeypatch.setattr(integ_mod, "_DELETE_INTEGRATION_SQL",
                        integ_mod._DELETE_INTEGRATION_SQL.replace("tenant_id = $1::uuid", FORGOT))
    sm = MagicMock()
    out = await _disconnect(ctx, ctx.b, flag_on=True, sm=sm)
    assert out["disconnected"] is False, "tenant B saw tenant A's integration on the tenant pool"
    assert not sm.put_secret_value.called
    assert await _integrations_of(ctx.conn, ctx.a) == ["wordpress"]

    leaked = await _disconnect(ctx, ctx.b, flag_on=False, sm=sm)
    assert leaked["disconnected"] is True, "sanity: the admin pool is not RLS-filtered"
    assert await _integrations_of(ctx.conn, ctx.a) == []


@pytest.mark.asyncio
async def test_disconnect_zero_row_delete_keeps_row_and_secret(ctx):
    """Critique C3: a policy that lets the tenant read the row but not delete it (DELETE 0) must not
    tombstone the secret: 503, row still connected, credentials intact."""
    await _integration(ctx.conn, ctx.a)
    await ctx.conn.execute("CREATE POLICY aa817_test_no_delete ON shared.tenant_integrations AS RESTRICTIVE "
                           f"FOR DELETE TO {TENANT_ROLE} USING (false)")
    sm = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await _disconnect(ctx, ctx.a, flag_on=True, sm=sm)
    assert exc.value.status_code == 503
    assert not sm.put_secret_value.called
    assert await _integrations_of(ctx.conn, ctx.a) == ["wordpress"]


@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_disconnect_tombstone_failure_rolls_the_delete_back(ctx, flag_on):
    await _integration(ctx.conn, ctx.a)
    sm = MagicMock()
    sm.put_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutSecretValue")
    with pytest.raises(HTTPException) as exc:
        await _disconnect(ctx, ctx.a, flag_on=flag_on, sm=sm)
    assert exc.value.status_code == 502
    assert await _integrations_of(ctx.conn, ctx.a) == ["wordpress"], "the DELETE must roll back"
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1",
                                   str(ctx.a)) == 0


@pytest.mark.asyncio
async def test_disconnect_without_row_is_idempotent_on_tenant_pool(ctx):
    sm = MagicMock()
    out = await _disconnect(ctx, ctx.a, flag_on=True, kind="facebook", sm=sm)
    assert out["disconnected"] is False and not sm.put_secret_value.called


# ── read routes: forgotten WHERE ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_piece_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    piece_a = await _published_piece(ctx.conn, ctx.a)
    own = await service.fetch_piece(ctx.a, piece_a, None, request=_request(ctx.conn, True))
    assert own["content_text"] == f"Body of {ctx.a}"
    monkeypatch.setattr(service, "_FETCH_PIECE_QUERY",
                        service._FETCH_PIECE_QUERY.replace("AND tenant_id = $2", "AND $2::uuid IS NOT NULL"))
    with pytest.raises(service.ContentWritingError):
        await service.fetch_piece(ctx.b, piece_a, None, request=_request(ctx.conn, True))
    leaked = await service.fetch_piece(ctx.b, piece_a, None, request=_request(ctx.conn, False))
    assert leaked["content_text"] == f"Body of {ctx.a}", "sanity: the admin pool is not RLS-filtered"


@pytest.mark.asyncio
async def test_pending_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    piece_a = await _published_piece(ctx.conn, ctx.a)
    own = await pub_mod.list_pending(_request(ctx.conn, True), {"sub": str(ctx.a)})
    assert [r["piece_id"] for r in own["data"]] == [str(piece_a)]
    forgot = re.sub(r"(\w+\.)?tenant_id = \$1::uuid", FORGOT, pub_mod._PENDING_SQL)
    assert forgot.count(FORGOT) == 2  # both tenant filters gone (the latest-piece subquery and the outer WHERE)
    monkeypatch.setattr(pub_mod, "_PENDING_SQL", forgot)
    other = await pub_mod.list_pending(_request(ctx.conn, True), {"sub": str(ctx.b)})
    assert other["data"] == [], "tenant pool leaked another tenant's pending piece"
    leaked = await pub_mod.list_pending(_request(ctx.conn, False), {"sub": str(ctx.b)})
    assert str(piece_a) in [r["piece_id"] for r in leaked["data"]], "sanity: the admin pool is not RLS-filtered"


@pytest.mark.asyncio
async def test_publish_log_list_on_tenant_pool_is_rls_scoped(ctx):
    """The list query's WHERE is inline (no module constant), so the forgotten-WHERE case is shown on the
    tables it reads: as aa_app_user with tenant B's GUC, tenant A's publish_log rows are invisible."""
    piece_a = await _published_piece(ctx.conn, ctx.a)
    await ctx.conn.execute(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, 'blog', 'published', '42', 'https://b.com/p/42', now())", piece_a, ctx.a)
    own = await pub_mod.list_publish_log(_request(ctx.conn, True), tenant={"sub": str(ctx.a)}, limit=50)
    assert own["total"] == 1 and own["data"][0]["piece_id"] == str(piece_a)
    other = await pub_mod.list_publish_log(_request(ctx.conn, True), tenant={"sub": str(ctx.b)}, limit=50)
    assert other["total"] == 0
    async with tp.acquire_scoped_conn(_request(ctx.conn, True), str(ctx.b), tp.PUBLISH_LOG_LIST_FLAG,
                                      fail_closed=True) as (conn, used):
        assert used is True
        seen = await conn.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1", piece_a)
    assert seen == 0, "tenant pool leaked another tenant's publish_log row"
