"""AA-1055 (stale guard) — the research stale share and the reatomized scope on the real schema: the scope query's
atom-touched column, the newest research row per place, the active tenants' markets, and the whole measure through
the READ ONLY pool. One asyncpg connection inside a transaction that is rolled back; no DataForSEO, LLM or Jev call
(stored picks only). Invented names and places only."""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402
from services.acp_contract import research_coverage as rc  # noqa: E402
from services.acp_contract import segment_research as sr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class _Pool:
    """One connection for every acquire (the test transaction); ReadOnlyPool opens a savepoint on it."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetch(self, *a):
        return await self.conn.fetch(*a)


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_ID)  # raw_tours RLS
    yield c
    await t.rollback()
    await c.close()


async def _tour(conn) -> uuid.UUID:
    tour_id = uuid.uuid4()
    await conn.execute("""INSERT INTO silver_aa_internal.raw_tours
                          (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                          VALUES ($1, $2, $3, 'Zarvok trek zz1055s', 'Mongolia', $4, 'active')""",
                       tour_id, uuid.UUID(TENANT_ID), uuid.UUID(BATCH_ID), uuid.uuid4())
    await conn.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name,
                          master_status) VALUES ($1, gen_random_uuid(), 'Zarvok trek zz1055s', 'active')""", tour_id)
    return tour_id


async def _segment(conn, tour_id, seg: str, place: str, touched: str) -> None:
    atom = f"{seg}_atom"
    await conn.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place,
                          activity_type, created_at, updated_at)
                          VALUES ($1, $2, 'atom text', 2, $3, 'sightseeing', $4::text::timestamptz,
                                  $4::text::timestamptz)""",
                       atom, tour_id, place, touched)
    await conn.execute("""INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action)
                          VALUES ($1, $2, 'visit')""", seg, place)
    await conn.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                       seg, atom)


async def _tenant(conn, target_market) -> None:
    tenant_id = uuid.uuid4()
    await conn.execute("""INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm,
                          is_active) VALUES ($1, 'Zarvok Travel zz1055s', $2, 'internal', 7, 60, true)""",
                       tenant_id, f"zz1055s-{tenant_id.hex[:8]}")
    if target_market is not None:
        await conn.execute("""INSERT INTO shared.tenant_seo_config (tenant_id, seo_provider, target_market)
                              VALUES ($1, 'dataforseo', $2::jsonb)""", tenant_id, json.dumps(target_market))


@pytest.mark.asyncio
async def test_reatomized_scope_on_the_real_tables(tx):
    tour = await _tour(tx)
    await _segment(tx, tour, "zz1055s_new", "Zarvok Lake", "2026-11-05")
    await _segment(tx, tour, "zz1055s_old", "Melnith Gorge", "2026-10-01")
    await _segment(tx, tour, "zz1055s_never", "Torvan Pass", "2026-10-01")
    await tx.execute("""INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
                        VALUES ('Zarvok Lake', 'US', 'volume', '2026-11-01'),
                               ('Melnith Gorge', 'US', 'serp', '2026-11-01')""")
    pool = _Pool(tx)
    rows = await sr.scoped_rows(pool, [str(tour)], None, cached_only=True)
    assert {r["canonical_place"] for r in rows} == {"Zarvok Lake", "Melnith Gorge", "Torvan Pass"}
    assert all(r["atoms_touched"] is not None for r in rows)
    newest = await sr.newest_research(pool, ["Zarvok Lake", "Melnith Gorge", "Torvan Pass"])
    assert set(newest) == {"Zarvok Lake", "Melnith Gorge"}
    by_place = await sr._places_in_scope(pool, None, [str(tour)], None, cached_only=True, reatomized=True)
    assert set(by_place) == {"Zarvok Lake", "Torvan Pass"}


@pytest.mark.asyncio
async def test_measure_through_the_read_only_pool(tx):
    tour = await _tour(tx)
    await _segment(tx, tour, "zz1055s_a", "Zarvok Lake", "2026-10-01")
    await _segment(tx, tour, "zz1055s_b", "Quellan Falls", "2026-11-05")
    await tx.execute("""INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
                        VALUES ('Zarvok Lake', 'UK', 'volume', now()), ('Zarvok Lake', 'US', 'volume', now()),
                               ('Zarvok Lake', 'US', 'serp', now())""")
    await _tenant(tx, {"countries": ["UK", "XX"]})
    await _tenant(tx, None)                                   # no SEO config row = US, like the Slate
    out = await rc.measure(rc.ReadOnlyPool(_Pool(tx), "10s", "1s"))
    assert {"US", "UK"} <= set(out["markets"])
    assert out["places_in_scope"] >= 2
    assert await tx.fetchval("SELECT count(*) FROM shared.unmapped_market_requests WHERE country_code = 'XX'") == 0
    # Zarvok Lake is fresh in both markets, Quellan Falls in none; other rows of this database only add to the counts
    by_place = await sr._places_in_scope(_Pool(tx), None, [str(tour)], None, cached_only=True)
    fresh = await sr._fresh_tiers(_Pool(tx), sorted(by_place))
    mine = rc.summarize(by_place, fresh, ["US", "UK"],
                        sr.reatomized_places(await sr.scoped_rows(_Pool(tx), [str(tour)], cached_only=True),
                                             await sr.newest_research(_Pool(tx), sorted(by_place))))
    assert mine["by_market"]["UK"]["places_stale"] == 1 and mine["by_market"]["US"]["places_stale"] == 1
    assert mine["waiting_after_reatomize"] == 1
    values = rc.coverage_values(out)
    assert values["targeted_UK"] == 1 and values["stale_UK"] >= 1
