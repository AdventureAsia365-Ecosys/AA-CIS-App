"""AA-1120 — the research cache's queries on the real schema: fresh volume / SERP reads within the windows, the
write-back upserts, the 0 USD hit row under the queue's label, and the paid-but-uncollected queue tasks read back from
shared.dfs_call_log meta ``posted`` within the recovery window.

One asyncpg connection inside a transaction that is rolled back. No DataForSEO, LLM or Jev call. Invented keywords
only."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.seo_intelligence import research_cache as rc  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
WINDOWS = rc.Windows(timedelta(days=30), timedelta(days=60), timedelta(days=7))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    yield c
    await t.rollback()
    await c.close()


def _cache(conn) -> rc.ResearchCache:
    return rc.ResearchCache(_Pool(conn), stage="a3_search_demand", windows=WINDOWS, meta={"job": "zz1120"})


@pytest.mark.asyncio
async def test_volumes_read_only_fresh_rows_and_write_back(tx):
    await tx.execute("""INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask,
                        retrieved_on) VALUES
                        ('zz1120 zarvok lake', 'US', 900, '[]', now() - interval '5 days'),
                        ('zz1120 melnith gorge', 'US', 40, '[]', now() - interval '45 days'),
                        ('zz1120 quorra pass', 'UK', NULL, '[]', now())""")
    cache = _cache(tx)
    got = await cache.volumes(["zz1120 zarvok lake", "zz1120 melnith gorge", "zz1120 quorra pass"], "US")
    assert got == {"zz1120 zarvok lake": 900}                     # 45 days is past the 30-day window; UK is not US
    assert await cache.volumes(["zz1120 quorra pass"], "UK") == {"zz1120 quorra pass": None}   # measured, no data
    await cache.put_volumes("US", {"zz1120 melnith gorge": 55, "zz1120 new place": None})
    got = await cache.volumes(["zz1120 melnith gorge", "zz1120 new place"], "US")
    assert got == {"zz1120 melnith gorge": 55, "zz1120 new place": None}


@pytest.mark.asyncio
async def test_serps_fresh_only_with_related_searches_and_write_back(tx):
    cache = _cache(tx)
    await cache.put_serp("zz1120 zarvok lake", "US", ["Is zarvok lake cold?"], ["zarvok lake map"], ["example.test"])
    await cache.put_serp("zz1120 melnith gorge", "US", [], [], [])
    await tx.execute("""UPDATE acp_contract.search_demand SET paa_retrieved_on = now() - interval '90 days'
                        WHERE keyword = 'zz1120 melnith gorge'""")
    got = await cache.serps(["zz1120 zarvok lake", "zz1120 melnith gorge"], "US")
    assert list(got) == ["zz1120 zarvok lake"]                    # 90 days is past the 60-day SERP window
    hit = got["zz1120 zarvok lake"]
    assert (hit.paa, hit.related, hit.domains) == (["Is zarvok lake cold?"], ["zarvok lake map"], ["example.test"])
    row = await tx.fetchrow("SELECT search_volume, serp_location, retrieved_on < now() - interval '1 year' AS stale "
                            "FROM acp_contract.search_demand WHERE keyword = 'zz1120 zarvok lake'")
    assert row["search_volume"] is None and row["serp_location"] == "US" and row["stale"]   # volume still to buy


@pytest.mark.asyncio
async def test_hit_row_carries_the_queue_label_and_the_run_stage(tx):
    cache = _cache(tx)
    await cache.log_hits("search_volume_bulk_queue", count=12, location_code=2840, meta={"phase": "volumes"})
    await cache.log_hits("serp_advanced_queue", count=0, location_code=2840)          # nothing for 0
    rows = await tx.fetch("SELECT endpoint, cache_hit, fetched_live, cost_usd::float AS usd, keyword_count, stage, "
                          "meta FROM shared.dfs_call_log WHERE meta->>'job' = 'zz1120'")
    assert len(rows) == 1
    r = rows[0]
    assert (r["endpoint"], r["cache_hit"], r["fetched_live"], r["usd"], r["keyword_count"], r["stage"]) == (
        "search_volume_bulk_queue", True, False, 0.0, 12, "a3_search_demand")
    assert json.loads(r["meta"])["source"] == "research_cache"


async def _post_row(conn, endpoint, posted, age_days=0):
    await conn.execute(
        """INSERT INTO shared.dfs_call_log (endpoint, location_code, cost_usd, cache_hit, fetched_live, keyword_count,
           meta, created_at, stage) VALUES ($1, 2840, 0.06, false, true, 1, $2::jsonb,
           now() - make_interval(days => $3), 'a3_search_demand')""",
        endpoint, json.dumps({"queue": "standard", "posted": posted}), age_days)


@pytest.mark.asyncio
async def test_pending_tasks_come_from_posts_within_the_recovery_window(tx):
    await _post_row(tx, "search_volume_bulk_queue",
                    [{"id": "zz1120-t1", "keywords": ["zz1120 zarvok lake", "zz1120 melnith gorge"],
                      "location_code": 2840}], age_days=1)
    await _post_row(tx, "search_volume_bulk_queue",
                    [{"id": "zz1120-t-old", "keywords": ["zz1120 quorra pass"], "location_code": 2840}], age_days=20)
    await _post_row(tx, "search_volume_bulk_queue",
                    [{"id": "zz1120-t-uk", "keywords": ["zz1120 zarvok lake"], "location_code": 2826}], age_days=1)
    await _post_row(tx, "serp_advanced_queue",
                    [{"id": "zz1120-s1", "keywords": ["zz1120 zarvok lake"], "location_code": 2840}], age_days=1)
    cache = _cache(tx)
    got = await cache.pending_tasks("search_volume_bulk_queue", 2840,
                                    ["zz1120 zarvok lake", "zz1120 quorra pass"])
    assert [(p.task_id, p.keywords) for p in got] == [
        ("zz1120-t1", ["zz1120 zarvok lake", "zz1120 melnith gorge"])]     # 20 days old and the UK task left out
    assert await cache.pending_tasks("search_volume_bulk_queue", 2840, ["zz1120 zarvok lake"],
                                     exclude={"zz1120-t1"}) == []
    off = rc.ResearchCache(_Pool(tx), stage="x", windows=rc.Windows(WINDOWS.volume, WINDOWS.serp, timedelta(0)))
    assert await off.pending_tasks("search_volume_bulk_queue", 2840, ["zz1120 zarvok lake"]) == []
