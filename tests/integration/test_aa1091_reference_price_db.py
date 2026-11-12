"""AA-1091 item 5 — services/reference_price/store.py against real Postgres: the scope SQL (AA master active rows with a
catalog key, the live flag from published_tours, the LEFT JOIN of CIS 401's table), save / accept / parse / clear /
import through the real UPSERT and the status rule after each write, as cis_app would run them.

Needs CIS 401 (silver_aa_internal.tour_reference_price). It ships in the migration-only PR before this code; until that
PR is on the branch the module is skipped (it then runs on every CI round: the conftest replays 401).
raw_tours.source_key (CIS 258, a catalog-recipe file the replay skips) is added inside the test transaction when the
replay lacks it.
Invented tour names only; every test runs in one transaction that is rolled back."""
import os
import sys
import uuid
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.reference_price import store  # noqa: E402
from services.reference_price.store import ImportRow  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = store.AA_MASTER_TENANT
OTHER_TENANT = "00000000-0000-4000-8000-0000000a1091"   # an agency tenant made inside the test transaction


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    if await c.fetchval("SELECT to_regclass('silver_aa_internal.tour_reference_price') IS NULL AS ok"):
        await c.close()
        pytest.skip("CIS 401 is not on this branch yet (migration-only PR first)")
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("ALTER TABLE silver_aa_internal.raw_tours ADD COLUMN IF NOT EXISTS source_key text "
                        "GENERATED ALWAYS AS ((COALESCE(source_group_id, tour_id))::text) STORED")
        await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
                        "is_active) VALUES ($1::uuid, 'Invented Agency', 'zz-aa1091-agency', 'internal', 7, 60, true) "
                        "ON CONFLICT DO NOTHING", OTHER_TENANT)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(c, name, *, price_raw=None, tenant=MASTER, published=True, status="active", master_status="active"):
    tour_id, group = str(uuid.uuid4()), str(uuid.uuid4())
    await c.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, source_group_id, "
        "source_status, price_raw) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Laos', $5::uuid, "
        "$6::silver_aa_internal.source_status_enum, $7)", tour_id, tenant, BATCH_ID, name, group, status, price_raw)
    if published:
        await c.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "aa_summary, master_status) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Summary', "
            "$5::gold_aa_internal.master_status_enum)", tour_id, str(uuid.uuid4()), tenant, name, master_status)
    return group   # = source_key (COALESCE(source_group_id, tour_id))


async def _items(c):
    return {i["source_key"]: i for i in await store.list_items(c)}


@pytest.mark.asyncio
async def test_scope_live_flag_and_status_after_each_write(conn):
    tag = uuid.uuid4().hex[:6]
    priced = await _tour(conn, f"Invented Ridge Trek {tag}", price_raw="USD 1,450 per person")
    request = await _tour(conn, f"Invented Lake Loop {tag}", price_raw="On request")
    hidden = await _tour(conn, f"Invented Cave Day {tag}", price_raw="USD 700", master_status="inactive")
    draft = await _tour(conn, f"Invented Hill Camp {tag}", price_raw="USD 300 pp", published=False)
    other = await _tour(conn, f"Invented Other Tenant {tag}", price_raw="USD 1", tenant=OTHER_TENANT)
    old = await _tour(conn, f"Invented Old Version {tag}", price_raw="USD 2", status="superseded")
    items = await _items(conn)
    assert items[priced]["live"] and items[priced]["status"] == "suggested"
    assert items[request]["status"] == "missing"
    assert not items[hidden]["live"] and not items[draft]["live"]
    assert other not in items and old not in items

    async with conn.transaction():
        change = await store.save(conn, priced, store.validate("1500", "USD", "from"), actor="admin:pytest")
    assert change.after["amount"] == "1500.00"
    assert (await _items(conn))[priced]["status"] == "confirmed"

    await conn.execute("UPDATE silver_aa_internal.raw_tours SET price_raw = 'USD 1,600 per person' "
                       "WHERE source_key = $1", priced)
    assert (await _items(conn))[priced]["status"] == "needs_review"        # the text changed since

    changes, skipped = await store.confirm(conn, [priced, request], actor="admin:pytest")
    assert [c.source_key for c in changes] == [priced] and skipped[0]["reason"] == "nothing_to_accept"
    row = await conn.fetchrow("SELECT amount, price_raw_seen, reviewed_by, origin FROM "
                              "silver_aa_internal.tour_reference_price WHERE source_key = $1", priced)
    assert row["amount"] == Decimal("1500.00") and row["price_raw_seen"] == "USD 1,600 per person"
    assert row["reviewed_by"] == "admin:pytest" and row["origin"] == "staff"

    summary, parsed = await store.parse_missing(conn, actor="admin:pytest", dry_run=False)
    keys = {c.source_key for c in parsed}
    assert {hidden, draft} <= keys and priced not in keys and request not in keys
    assert (await _items(conn))[draft]["status"] == "parsed"

    await store.clear(conn, priced)
    assert (await _items(conn))[priced]["status"] == "suggested"


@pytest.mark.asyncio
async def test_import_plan_and_apply_on_real_rows(conn):
    tag = uuid.uuid4().hex[:6]
    a = await _tour(conn, f"Invented Alpha Loop {tag}")
    b = await _tour(conn, f"Invented Beta Walk {tag}", price_raw="from EUR 900")
    plan, values = await store.plan_import(conn, [
        ImportRow(2, a, 2300, "eur", "per group", "Private jeep"),
        ImportRow(3, b, None, None, None),
        ImportRow(4, "zz-unknown", 1, "USD", "from"),
    ])
    assert [r["action"] for r in plan.rows] == ["create", "skip", "error"]
    applied = await store.apply_import(conn, values, actor="admin:pytest")
    assert [c.source_key for c in applied] == [a]
    item = (await _items(conn))[a]
    assert item["status"] == "confirmed" and item["saved"]["basis"] == "per_group"
    assert item["saved"]["origin"] == "import" and item["saved"]["note"] == "Private jeep"
    plan, values = await store.plan_import(conn, [ImportRow(2, a, 2300, "EUR", "per_group", "Private jeep")])
    assert plan.rows[0]["action"] == "unchanged" and not values
