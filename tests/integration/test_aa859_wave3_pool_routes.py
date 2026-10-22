"""AA-859 wave 3 — the brand identity tenant branch and publish on the tenant pool against real Postgres, as
aa_app_user under the live policies (262 on tenant_brand_rules / tenant_seo_config; 229 / 237 / 241 on the publish
tables).

  - GET /admin/brand-identity (tenant): same answer on both pools; with the tenant condition forgotten a tenant
    without brand rows still reads none on the tenant pool, while the admin pool leaks another tenant's (so the
    check can fail).
  - POST /admin/brand-identity (tenant): the real statements run under 262's grants (no 42501), MAX + 1 under the
    advisory lock, the markets upsert lands in the caller's tenant_seo_config row, the other tenant is untouched.
  - POST /v1/publish-log/{piece_id}/publish: the real claim() / finish() run as aa_app_user (claim's transaction
    nested as a savepoint) and write the caller's publish_log row; another tenant's piece is a 404, also with the
    tenant condition forgotten (the admin pool lets it through to the next check, so the check can fail).

Same harness as test_aa859_t2_version_reads_pool.py (D5a): the "tenant pool" is the test connection with SET LOCAL
ROLE aa_app_user; everything runs in one transaction that is rolled back.
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
from api.routers import admin_pipeline as ap  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from test_aa859_t2_version_reads_pool import _OneConnPool, _Redis, _tenant  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"
WP_CREDS = {"wp_url": "https://b.invalid", "username": "u", "app_password": "p"}


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _brand(conn, tid, version, active, prompt):
    await conn.execute("INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name, version, is_active, "
                       "system_prompt) VALUES ($1::uuid, 'default', $2, $3, $4)", tid, version, active, prompt)


async def _piece(conn, tid):
    req = await conn.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                              "VALUES ($1::uuid, 'aa859w3', 'blog', 'approved') RETURNING request_id", tid)
    opt = await conn.fetchval("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, "
                              "formula_fit, best_final_style, chosen) VALUES ($1, 0, 'Angle', 'w', 'f', 's', true) "
                              "RETURNING option_id", req)
    return await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, angle_gate_option_id, attempt_number, "
        "content_text, status, gate_ledger, repair_log) VALUES ($1::uuid, $2, $3, 1, $4, 'approved', '[]'::jsonb, "
        "'[]'::jsonb) RETURNING piece_id", tid, req, opt, f"## Body of {tid}")


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        a, b, c = await _tenant(conn), await _tenant(conn), await _tenant(conn)
        await _brand(conn, a, 1, False, "a v1")
        await _brand(conn, a, 2, True, "a v2")
        await _brand(conn, b, 1, True, "b v1")
        await conn.execute("INSERT INTO shared.tenant_seo_config (tenant_id, target_market) VALUES ($1::uuid, $2)",
                           b, json.dumps({"countries": ["UK"]}))
        piece_a = await _piece(conn, a)
        await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key) "
                           "VALUES ($1::uuid, 'wordpress', '{}'::jsonb, $2)", a, f"acp/cms/{a}")
        yield SimpleNamespace(conn=conn, a=a, b=b, c=c, piece_a=piece_a)
    finally:
        await tr.rollback()
        await conn.close()


def _forget(monkeypatch, module, name, old, new):
    value = getattr(module, name)
    assert old in value
    monkeypatch.setattr(module, name, value.replace(old, new))


# ── brand identity (tenant Bearer) ────────────────────────────────────────────

async def _get(c, tid, flag_on):
    return await ap.get_brand_identity(_request(c.conn, flag_on), tenant_id=tid, tenant_caller=True)


async def _post(c, tid, flag_on, **body):
    body = ap.BrandIdentityUpdate(**({"system_prompt": "Plain, warm words.", "target_markets": ["Australia"]}
                                     | body))
    return await ap.update_brand_identity(body, _request(c.conn, flag_on), tenant_id=tid, tenant_caller=True)


@pytest.mark.asyncio
async def test_brand_get_on_tenant_pool_matches_admin_pool(ctx):
    on = await _get(ctx, ctx.a, flag_on=True)
    assert on == await _get(ctx, ctx.a, flag_on=False)
    assert on["configured"] is True and on["system_prompt"] == "a v2"
    assert [h["version"] for h in on["history"]] == [2, 1]


@pytest.mark.asyncio
async def test_brand_get_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch):
    _forget(monkeypatch, ap, "_BRAND_TENANT_COND", "tenant_id = $1", "$1::uuid IS NOT NULL")
    on = await _get(ctx, ctx.c, flag_on=True)
    assert on["configured"] is False, "tenant C read another tenant's brand rows on the tenant pool"
    leaked = await _get(ctx, ctx.c, flag_on=False)
    assert leaked["configured"] is True, "sanity: the admin pool is not filtered"


@pytest.mark.asyncio
async def test_brand_post_on_tenant_pool_writes_the_next_version_and_markets(ctx):
    out = await _post(ctx, ctx.a, flag_on=True)
    assert out["version"] == 3 and out["markets"]["countries"] == ["AU"]
    out = await _post(ctx, ctx.a, flag_on=True, system_prompt="Second save.")
    assert out["version"] == 4
    rows = await ctx.conn.fetch("SELECT version, is_active FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid "
                                "ORDER BY version", ctx.a)
    assert [(r["version"], r["is_active"]) for r in rows] == [(1, False), (2, False), (3, False), (4, True)]
    tm = await ctx.conn.fetchval("SELECT target_market FROM shared.tenant_seo_config WHERE tenant_id = $1::uuid",
                                 ctx.a)
    assert json.loads(tm)["countries"] == ["AU"]
    assert await ctx.conn.fetchval("SELECT count(*) FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid "
                                   "AND is_active", ctx.b) == 1, "the other tenant's active row is untouched"
    tm_b = await ctx.conn.fetchval("SELECT target_market FROM shared.tenant_seo_config WHERE tenant_id = $1::uuid",
                                   ctx.b)
    assert json.loads(tm_b)["countries"] == ["UK"]


@pytest.mark.asyncio
async def test_brand_post_on_tenant_pool_updates_an_existing_markets_row(ctx):
    out = await _post(ctx, ctx.b, flag_on=True, target_markets=["Singapore"])
    assert out["version"] == 2
    tm = await ctx.conn.fetchval("SELECT target_market FROM shared.tenant_seo_config WHERE tenant_id = $1::uuid",
                                 ctx.b)
    assert json.loads(tm)["countries"] == ["SG"]


@pytest.mark.asyncio
async def test_brand_post_on_tenant_pool_for_a_tenant_without_rows(ctx):
    out = await _post(ctx, ctx.c, flag_on=True)
    assert out["version"] == 1
    got = await _get(ctx, ctx.c, flag_on=True)
    assert got["configured"] is True and got["version"] == 1


# ── publish ───────────────────────────────────────────────────────────────────

async def _publish(c, tid, piece_id, flag_on):
    with patch.object(pub_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(pub_mod, "_call_adapter", AsyncMock(return_value=("4242", "https://b.invalid/?p=4242"))):
        return await pub_mod.publish(piece_id, _request(c.conn, flag_on), tenant={"sub": tid})


@pytest.mark.asyncio
async def test_publish_on_tenant_pool_claims_and_finishes_as_the_tenant_role(ctx):
    out = await _publish(ctx, ctx.a, ctx.piece_a, flag_on=True)
    assert out["success"] is True and out["status"] == "published" and out["external_id"] == "4242"
    rows = await ctx.conn.fetch("SELECT tenant_id::text, status, external_id, idempotency_key IS NOT NULL AS keyed "
                                "FROM acp_shared.publish_log WHERE piece_id = $1", ctx.piece_a)
    assert [tuple(r) for r in rows] == [(ctx.a, "published", "4242", True)]
    again = await _publish(ctx, ctx.a, ctx.piece_a, flag_on=True)
    assert again["replayed"] is True, "the same revision is replayed from publish_log, not posted twice"


@pytest.mark.asyncio
async def test_publish_foreign_piece_is_404_even_with_the_where_forgotten(ctx, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await _publish(ctx, ctx.b, ctx.piece_a, flag_on=True)
    assert exc.value.status_code == 404
    _forget(monkeypatch, pub_mod, "_PUBLISH_PIECE_TENANT_COND", "cp.tenant_id = $2::uuid", "$2::uuid IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await _publish(ctx, ctx.b, ctx.piece_a, flag_on=True)
    assert exc.value.status_code == 404, "tenant B reached tenant A's piece on the tenant pool"
    with pytest.raises(HTTPException) as exc:
        await _publish(ctx, ctx.b, ctx.piece_a, flag_on=False)
    assert exc.value.status_code == 422, "sanity: on the admin pool B passes ownership (then has no connection)"
    assert await ctx.conn.fetchval("SELECT count(*) FROM acp_shared.publish_log WHERE piece_id = $1",
                                   ctx.piece_a) == 0
