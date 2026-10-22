"""AA-859 D5b — the T2 detail reads on the tenant pool against real Postgres, as aa_app_user under the live
policies (229 on tenant_tour_versions, 239 master read on published_tours / raw_tours / seo_context).

  - GET /v1/tours/{id} and the tenant branch of GET /v1/tours/{id}/full: tenant A gets the same answer on the
    tenant pool as on the admin pool (raw + seo included); a tour A has no version of is a 404 for A; with the
    tenant condition forgotten, tenant B still gets a 404 for A's tour on the tenant pool while the admin pool
    leaks it (so each check can fail).
  - GET /v1/tours/versions/{id}/export-docx: same file name on both pools for A; A's version is a 404 for B,
    also with the tenant condition forgotten.

Same harness as test_aa859_t2_version_reads_pool.py (D5a): the "tenant pool" is the test connection with SET
LOCAL ROLE aa_app_user, one transaction rolled back at the end, a live-shape tenant_tour_versions stand-in with
229 re-applied.
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from api.routers import v1_tours  # noqa: E402
from test_aa859_t2_version_reads_pool import (  # noqa: E402
    BATCH_ID, MASTER, TTV_LIVE_STANDIN_DDL, _OneConnPool, _Redis, _tenant, _version,
)

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT_ROLE = "aa_app_user"


def _request(conn, flag_on):
    state = SimpleNamespace(pool=_OneConnPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE),
                            redis=_Redis(flag_on))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _master_tour_with_seo(conn, name):
    tour_id = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                       "duration, inclusions) VALUES ($1, $2, $3, $4, 'Vietnam', '3 days', 'Boat')",
                       tour_id, MASTER, BATCH_ID, name)
    await conn.execute("INSERT INTO silver_aa_internal.seo_context (tenant_id, tour_id, keyword_search) "
                       "VALUES ($1, $2, $3)", MASTER, tour_id, f"kw {name}")
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, 'active') "
        "RETURNING id", tour_id, uuid.uuid4(), MASTER, name)


@pytest_asyncio.fixture
async def ctx(monkeypatch):
    monkeypatch.setattr(v1_tours, "verify_tenant_jwt", lambda token: {"sub": token})
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        name = f"AA859-D5b {uuid.uuid4().hex[:8]}"
        a, b = await _tenant(conn), await _tenant(conn)
        pt = await _master_tour_with_seo(conn, name)               # only A rewrote it
        va = await _version(conn, a, pt, 1)
        yield SimpleNamespace(conn=conn, a=a, b=b, pt=str(pt), va=va, name=name)
    finally:
        await tr.rollback()
        await conn.close()


async def _tour(c, tid, flag_on):
    return await v1_tours.get_tour(c.pt, _request(c.conn, flag_on), {"sub": tid})


async def _full(c, tid, flag_on):
    return await v1_tours.get_tour_full(c.pt, _request(c.conn, flag_on), x_admin_secret=None,
                                        credentials=SimpleNamespace(credentials=tid))


async def _export(c, tid, version_id, flag_on):
    return await v1_tours.export_version_docx(str(version_id), _request(c.conn, flag_on), {"sub": tid})


def _forget(monkeypatch, name, old, new):
    value = getattr(v1_tours, name)
    assert old in value
    monkeypatch.setattr(v1_tours, name, value.replace(old, new))


# ── /{id} and /{id}/full ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tour_on_tenant_pool_matches_admin_pool(ctx):
    on = await _tour(ctx, ctx.a, flag_on=True)
    assert on == await _tour(ctx, ctx.a, flag_on=False)
    assert on["aa_name"] == ctx.name


@pytest.mark.asyncio
async def test_full_on_tenant_pool_matches_admin_pool_with_raw_and_seo(ctx):
    on = await _full(ctx, ctx.a, flag_on=True)
    assert on == await _full(ctx, ctx.a, flag_on=False)
    assert on["raw"]["country"] == "Vietnam", "239 master read: the master raw row is visible"
    assert on["seo"]["keyword_search"] == f"kw {ctx.name}", "239 master read: the master seo row is visible"
    assert set(on) == {"published", "raw", "seo"}


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_tour, _full])
async def test_tour_without_an_own_version_is_404(ctx, call):
    with pytest.raises(HTTPException) as exc:
        await call(ctx, ctx.b, True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_tour, _full])
async def test_tour_rls_holds_when_the_where_is_forgotten(ctx, monkeypatch, call):
    _forget(monkeypatch, "_TOUR_VIA_VERSION_TENANT_COND", "ttv.tenant_id = $2::uuid", "$2::uuid IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await call(ctx, ctx.b, True)
    assert exc.value.status_code == 404, "tenant B reached a tour only tenant A rewrote, on the tenant pool"
    leaked = await call(ctx, ctx.b, False)
    assert leaked, "sanity: the admin pool is not filtered"


# ── export-docx ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_on_tenant_pool_matches_admin_pool(ctx):
    on = await _export(ctx, ctx.a, ctx.va, flag_on=True)
    off = await _export(ctx, ctx.a, ctx.va, flag_on=False)
    assert on.status_code == off.status_code == 200
    assert on.headers["content-disposition"] == off.headers["content-disposition"]
    assert on.headers["content-disposition"].endswith("_v1.docx")


@pytest.mark.asyncio
async def test_export_foreign_version_is_404_even_with_the_where_forgotten(ctx, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await _export(ctx, ctx.b, ctx.va, flag_on=True)
    assert exc.value.status_code == 404
    _forget(monkeypatch, "_EXPORT_VERSION_TENANT_COND", "ttv.tenant_id = $2::uuid", "$2::uuid IS NOT NULL")
    with pytest.raises(HTTPException) as exc:
        await _export(ctx, ctx.b, ctx.va, flag_on=True)
    assert exc.value.status_code == 404, "tenant B exported tenant A's version on the tenant pool"
    leaked = await _export(ctx, ctx.b, ctx.va, flag_on=False)
    assert leaked.status_code == 200, "sanity: the admin pool is not filtered"
