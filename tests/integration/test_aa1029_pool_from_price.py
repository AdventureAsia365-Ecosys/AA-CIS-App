"""AA-1029 R4 — GET /v1/tours/pool carries the public "from" price (price.from.v1), against real Postgres.

The Content studio Marketplace shows "From US$X per person" on each AA tour. The price is AA-Booking's, read only
through its contract view booking.v_from_prices (Booking bk-039; SELECT to cis_app by bk-040), on MAIN after the tenant
block, by the catalog key (raw_tours.source_key = COALESCE(source_group_id, tour_id)::text).

Checked here: both pools give the same answer; a priced tour gets {amount, currency, basis, pax, season, valid_to},
an unpriced one null; the key follows source_group_id; the internal key never reaches the tenant; a view that is not
there (bk-039 not applied) or not granted (bk-040 not applied) gives null prices, no error, and leaves the caller's
transaction usable; cis_app reads the view once granted.

booking.v_from_prices is a stand-in table with the contract's exact columns (the CIS replay has no Booking schema),
created inside the test transaction and rolled back. Harness: tests/integration/test_aa859_t2_version_reads_pool.py.
"""
import os
import sys
import uuid
from datetime import date
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _rls_fixture_helpers import migration_body  # noqa: E402
from test_aa859_t2_version_reads_pool import (  # noqa: E402
    BATCH_ID, DSN, MASTER, TTV_LIVE_STANDIN_DDL, _OneConnPool, _request, _tenant,
)
from api.routers import v1_tours  # noqa: E402

# price.from.v1 columns (root docs/contracts/price.from.v1.md), in the view's order.
FROM_PRICES_STANDIN_DDL = """
    CREATE TABLE booking.v_from_prices (
        source_system text NOT NULL, source_id text NOT NULL, amount numeric(12,2), currency text, basis text,
        pax integer, season text, valid_from date, valid_to date, price_kind text, priced_at timestamptz
    )
"""
PRICE_SQL = ("INSERT INTO booking.v_from_prices VALUES ('cis', $1, $2, 'USD', 'per_person', 2, $3, $4, $5, 'rrp', "
             "now())")


async def _tour(conn, name, *, group_id=None):
    tour_id = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                       "duration, source_status, source_group_id) VALUES ($1, $2, $3, $4, 'Vietnam', '5 days', "
                       "'active', $5)", tour_id, MASTER, BATCH_ID, name, group_id)
    pt = await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status, published_at) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, 'active', "
        "now()) RETURNING id", tour_id, uuid.uuid4(), MASTER, name)
    return tour_id, pt


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        await conn.execute("CREATE SCHEMA IF NOT EXISTS booking")
        await conn.execute("DROP TABLE IF EXISTS booking.v_from_prices")
        await conn.execute("DROP VIEW IF EXISTS booking.v_from_prices")
        tag = uuid.uuid4().hex[:8]
        a = await _tenant(conn)
        t1, pt1 = await _tour(conn, f"AA1029 {tag} priced")
        _t2, pt2 = await _tour(conn, f"AA1029 {tag} unpriced")
        group = uuid.uuid4()
        _t3, pt3 = await _tour(conn, f"AA1029 {tag} grouped", group_id=group)
        yield SimpleNamespace(conn=conn, a=a, tag=tag, t1=t1, pt1=pt1, pt2=pt2, pt3=pt3, group=group)
    finally:
        await tr.rollback()
        await conn.close()


async def _priced(c):
    await c.conn.execute(FROM_PRICES_STANDIN_DDL)
    await c.conn.execute(PRICE_SQL, str(c.t1), 235, "Low", date(2026, 1, 1), date(9999, 12, 31))
    await c.conn.execute(PRICE_SQL, str(c.group), 410.5, "High / Peak", date(2026, 6, 1), date(2027, 3, 31))


async def _browse(c, flag_on):
    return await v1_tours.browse_pool(_request(c.conn, flag_on), {"sub": c.a}, page=1, page_size=20, country=None,
                                      search=f"AA1029 {c.tag}", duration_min=None, duration_max=None, sort="newest")


def _by_id(answer):
    return {d["id"]: d for d in answer["data"]}


@pytest.mark.asyncio
async def test_pool_rows_carry_the_from_price_on_both_pools(ctx):
    await _priced(ctx)
    on, off = await _browse(ctx, True), await _browse(ctx, False)
    assert on == off
    rows = _by_id(on)
    assert set(rows) == {ctx.pt1, ctx.pt2, ctx.pt3}
    assert rows[ctx.pt1]["from_price"] == {"amount": "235.00", "currency": "USD", "basis": "per_person", "pax": 2,
                                           "season": "Low", "valid_to": "9999-12-31"}
    assert rows[ctx.pt2]["from_price"] is None
    # the catalog key of a grouped source is its group id, not the tour id
    assert rows[ctx.pt3]["from_price"]["amount"] == "410.50"
    assert rows[ctx.pt3]["from_price"]["valid_to"] == "2027-03-31"
    for row in rows.values():
        assert "catalog_source_id" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant pool", "admin pool"])
async def test_missing_view_gives_no_price_and_keeps_the_transaction(ctx, flag_on):
    """bk-039 not applied (relation missing): every from_price is null, the page answers, and the caller's (here the
    test's) transaction is still usable: the failed read rolled back to its own savepoint."""
    answer = await _browse(ctx, flag_on)
    assert {d["from_price"] for d in answer["data"]} == {None}
    assert len(answer["data"]) == 3
    assert await ctx.conn.fetchval("SELECT 1") == 1


@pytest.mark.asyncio
async def test_cis_app_reads_the_view_only_once_granted(ctx):
    """bk-040 not applied: cis_app gets 42501, the reader answers {}; with the grant it reads the price."""
    await _priced(ctx)
    main = _OneConnPool(ctx.conn, "cis_app")
    keys = [str(ctx.t1)]
    await ctx.conn.execute("REVOKE ALL ON SCHEMA booking FROM cis_app")
    assert await v1_tours._from_prices(main, keys) == {}
    assert await ctx.conn.fetchval("SELECT 1") == 1
    await ctx.conn.execute("GRANT USAGE ON SCHEMA booking TO cis_app")
    await ctx.conn.execute("GRANT SELECT ON booking.v_from_prices TO cis_app")
    got = await v1_tours._from_prices(main, keys)
    assert got[str(ctx.t1)]["amount"] == "235.00"
