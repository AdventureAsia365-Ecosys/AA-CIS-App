"""AA-1127 (C1, S283 UI audit) — SEO intelligence on real Postgres: demand totals count the buyer markets only and carry
the linked volume; the Keywords list puts keywords our content uses first. Both read one "used" definition.

Runs in one transaction that is rolled back; invented keywords only; totals are read as deltas."""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _constants import BATCH_ID, TENANT_ID as MASTER  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_seo  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _demand(c, keyword, market, volume):
    await c.execute("INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, "
                    "retrieved_on) VALUES ($1, $2, $3, '[]'::jsonb, now())", keyword, market, volume)


async def _by_market(c):
    return {r["market"]: dict(r) for r in await c.fetch(admin_seo._DEMAND_BY_MARKET_SQL, MASTER, admin_seo._MARKETS)}


@pytest.mark.asyncio
async def test_demand_is_buyer_markets_only_with_the_linked_volume(conn):
    before = await _by_market(conn)
    tid = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                       "duration, source_status) VALUES ($1, $2, $3, 'Zz Amber Pass Trek', 'Nepal', '6 days', "
                       "'active')", tid, MASTER, BATCH_ID)
    await conn.execute("INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, "
                       "aa_name, aa_summary, aa_highlights, master_status, published_at, seo_keywords_used) VALUES "
                       "($1, $2, $3, 'Zz Amber Pass Trek', 'S', '[]'::jsonb, 'active', now(), "
                       "'[\"Zzkw Amber Pass Trek\"]'::jsonb)", tid, uuid.uuid4(), MASTER)
    await _demand(conn, "zzkw amber pass trek", "US", 300)
    await _demand(conn, "zzkw generic lodging", "US", 90000)
    await _demand(conn, "zzkw amber pass trek", "US-PAA2", 0)
    after = await _by_market(conn)
    assert "US-PAA2" not in after
    us0 = before.get("US", {"keywords": 0, "total_volume": 0, "linked_volume": 0, "linked_keywords": 0})
    us = after["US"]
    assert us["keywords"] - us0["keywords"] == 2
    assert us["total_volume"] - us0["total_volume"] == 90300
    assert us["linked_volume"] - us0["linked_volume"] == 300
    assert us["linked_keywords"] - us0["linked_keywords"] == 1

    rows = [r for r in await conn.fetch(admin_seo._KEYWORDS_SQL, MASTER, admin_seo._MARKETS)
            if r["keyword"].startswith("zzkw ")]
    assert [(r["keyword"], r["tours_using"], r["used_in_content"]) for r in rows] == [
        ("zzkw amber pass trek", 1, True), ("zzkw generic lodging", 0, False)]
    all_rows = await conn.fetch(admin_seo._KEYWORDS_SQL, MASTER, admin_seo._MARKETS)
    first_unlinked = next(i for i, r in enumerate(all_rows) if not r["used_in_content"])
    assert all(r["used_in_content"] for r in all_rows[:first_unlinked])
    assert not any(r["used_in_content"] for r in all_rows[first_unlinked:])
