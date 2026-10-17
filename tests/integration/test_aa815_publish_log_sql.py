"""AA-815 — the new tenant SQL against real Postgres (all migrations applied by conftest):
GET /v1/publish-log (list_publish_log), DELETE /v1/publish-log/{id} (unpublish, remote delete
mocked) and DELETE /v1/integrations/{type} (disconnect, Secrets Manager mocked).
"""
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import aa544_tenant_pool as tp  # noqa: E402
from api.routers import v1_integrations as integ_mod  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from services.acp_s4_blog.cms.wordpress import WordPressAdapter  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # Migration 124 (content_piece.angle_gate_option_id) never applies in the test DB: the same
    # file adds a pgvector column and the CI image has no pgvector, and conftest only warns. The
    # list query joins on that column exactly like list_pending() does on Dev, so add it here.
    await p.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS angle_gate_option_id UUID")
    await p.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel TEXT")
    tid = uuid.uuid4()
    await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'AA-815', $2, 3)",
                    tid, f"aa815-{tid.hex[:8]}")
    yield p, tid
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM acp_shared.audit_log WHERE tenant_id = $1", str(tid))
    await p.execute("DELETE FROM shared.tenant_integrations WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    await p.close()


class _FlagsOff:
    """S241 D-12: a tenant-pool flag that cannot be read means ON (tenant pool, RLS), so a bare MagicMock
    redis would now send these routes to the tenant pool (503 here: no tenant pool). These tests check the
    route SQL on the admin pool (the owner connection `p`), so they state it: every flag read is an explicit
    OFF. The same routes on the tenant pool under RLS: test_aa817_tenant_pool_routes.py and
    test_aa859_unpublish_pool.py."""

    async def get(self, key):
        return tp.FLAG_OFF_VALUE if key in tp.ALL_TENANT_POOL_FLAGS else None


def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    req.app.state.tenant_pool = None
    req.app.state.redis = _FlagsOff()  # admin-pool path on purpose, see _FlagsOff
    return req


async def _piece(p, tid, channel="blog"):
    req = await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, 'a815', $2, 'approved') RETURNING request_id", tid, channel)
    return await p.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, channel) "
        "VALUES ($1, $2, 1, 'x', 'approved', '{}'::jsonb, '[]'::jsonb, $3) RETURNING piece_id", tid, req, channel)


async def _log(p, tid, piece, status, channel="blog", external_id="42"):
    return await p.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, $3, $4, $5, $6, now()) RETURNING publish_id",
        piece, tid, channel, status, external_id, f"https://b.com/p/{external_id}")


@pytest.mark.asyncio
async def test_list_returns_only_own_published_and_unpublished_rows(ctx):
    p, tid = ctx
    piece = await _piece(p, tid)
    await _log(p, tid, piece, "published")
    await _log(p, tid, piece, "failed", external_id=None)
    out = await pub_mod.list_publish_log(_request(p), tenant={"sub": str(tid)}, limit=50)
    assert out["total"] == 1
    assert out["data"][0]["status"] == "published" and out["data"][0]["piece_id"] == str(piece)

    other = await pub_mod.list_publish_log(_request(p), tenant={"sub": str(uuid.uuid4())}, limit=50)
    assert other["total"] == 0


@pytest.mark.asyncio
async def test_unpublish_flips_row_after_remote_delete_and_failure_keeps_it(ctx):
    p, tid = ctx
    piece = await _piece(p, tid)
    pub_id = await _log(p, tid, piece, "published")
    await p.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                    "connected_at) VALUES ($1, 'wordpress', '{}'::jsonb, 'acp/cms/x', now())", tid)
    creds = {"wp_url": "https://b.com", "username": "u", "app_password": "p"}

    with patch.object(pub_mod, "_get_secret", return_value=creds), \
         patch.object(WordPressAdapter, "delete_post", AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(HTTPException) as exc_info:
            await pub_mod.unpublish(pub_id, _request(p), tenant={"sub": str(tid)})
    assert exc_info.value.status_code == 502
    assert await p.fetchval("SELECT status FROM acp_shared.publish_log WHERE publish_id = $1", pub_id) == "published"

    with patch.object(pub_mod, "_get_secret", return_value=creds), \
         patch.object(WordPressAdapter, "delete_post", AsyncMock(return_value="trashed")):
        out = await pub_mod.unpublish(pub_id, _request(p), tenant={"sub": str(tid)})
    assert out["status"] == "unpublished" and out["remote"] == "trashed"
    row = await p.fetchrow("SELECT status, unpublished_by FROM acp_shared.publish_log WHERE publish_id = $1", pub_id)
    assert row["status"] == "unpublished" and row["unpublished_by"] == f"tenant:{tid}"
    assert await p.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                            "AND action = 'content_piece.unpublished'", str(tid)) == 1


@pytest.mark.asyncio
async def test_disconnect_deletes_only_that_integration_row(ctx):
    p, tid = ctx
    for kind in ("wordpress", "facebook"):
        await p.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                        "connected_at) VALUES ($1, $2, '{}'::jsonb, $3, now())", tid, kind, f"acp/x/{kind}")
    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        out = await integ_mod.disconnect_facebook(_request(p), tenant={"sub": str(tid)})
    assert out["disconnected"] is True
    left = await p.fetch("SELECT integration_type FROM shared.tenant_integrations WHERE tenant_id = $1", tid)
    assert [r["integration_type"] for r in left] == ["wordpress"]
    assert await p.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1 "
                            "AND action = 'integration.disconnected'", str(tid)) == 1
