"""AA-1120 — the planner replay script's queries on the real schema (scripts/aa1120/planner_replay.py): one invented
day in 2001 with two segment_research jobs, their ledger rows, the store rows they wrote, their research log and keyword
maps. The second job paid again for a pair the first had stored: the replay shows it as "paid, not stored" before and
plans nothing for it after. One connection in a transaction that is rolled back; invented keywords only."""
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aa1120" / "planner_replay.py"
spec = importlib.util.spec_from_file_location("aa1120_planner_replay_sql", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

DAY = datetime(2001, 1, 7, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    yield c
    await t.rollback()
    await c.close()


async def _job(c, hours: int, minutes: int) -> str:
    job_id = uuid.uuid4()
    start = DAY + timedelta(hours=hours)
    await c.execute(
        "INSERT INTO shared.job (id, kind, payload, status, attempt, max_attempts, run_after, created_by, created_at, "
        "started_at, finished_at) VALUES ($1, 'segment_research', $2::jsonb, 'succeeded', 1, 2, $3, 'zz1120', $3, $3, "
        "$4)", job_id, json.dumps({"markets": ["US"], "dfs_queue": "standard"}), start,
        start + timedelta(minutes=minutes))
    return str(job_id)


async def _ledger(c, job_id: str, endpoint: str, n: int, kws: int, usd: float, at: datetime) -> None:
    for _ in range(n):
        await c.execute(
            "INSERT INTO shared.dfs_call_log (endpoint, location_code, cost_usd, cache_hit, fetched_live, "
            "keyword_count, job_id, created_at, stage) VALUES ($1, 2840, $2, false, true, $3, $4::uuid, $5, "
            "'a3_search_demand')", endpoint, usd, kws, job_id, at)


@pytest.mark.asyncio
async def test_replay_reads_one_day_and_shows_the_repeat_purchase(tx, capsys):
    j1 = await _job(tx, 1, 30)
    j2 = await _job(tx, 5, 30)
    t1, t2 = DAY + timedelta(hours=1, minutes=10), DAY + timedelta(hours=5, minutes=10)
    await _ledger(tx, j1, "search_volume_bulk_queue", 1, 3, 0.06, t1)
    await _ledger(tx, j1, "serp_advanced_queue", 1, 2, 0.0012, t1)
    await _ledger(tx, j2, "search_volume_bulk_queue", 1, 3, 0.06, t2)          # bought the same three again
    await tx.execute(
        "INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on, "
        "related_searches, paa_retrieved_on, serp_location) VALUES "
        "('zz1120 zarvok lake', 'US', 900, '[]', $1, '[]', $1, 'US'), "
        "('zz1120 zarvok pier', 'US', 20, '[]', $1, '[]', $1, 'US'), "
        "('zz1120 melnith gorge', 'US', 0, '[]', $1, NULL, NULL, NULL)", t1)
    await tx.execute(
        "INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at) VALUES "
        "('Zz1120 Zarvok Lake', 'US', 'volume', $1), ('Zz1120 Melnith Gorge', 'US', 'volume', $1)", t1)
    await tx.execute(
        "INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, source, prompt_version, "
        "jev_zone, jev_mode, proposed_at) VALUES "
        "('place', 'Zz1120 Zarvok Lake', 'zz1120 zarvok lake', 0, 'place_name', 'zz', 'not_asked', NULL, $1), "
        "('place', 'Zz1120 Zarvok Lake', 'zz1120 zarvok pier', 1, 'llm_propose', 'zz', 'grey', 'shadow', $1), "
        "('place', 'Zz1120 Melnith Gorge', 'zz1120 melnith gorge', 0, 'place_name', 'zz', 'not_asked', NULL, $1)",
        t1)

    await rp.report(tx, "2001-01-07", 1, "2001-01-01")
    out = [ln for ln in capsys.readouterr().out.splitlines()]
    assert not [ln for ln in out if ln.startswith("ERR")], out
    assert "RP day day=2001-01-07 days=1 jobs=2" in out
    after1 = next(ln for ln in out if ln.startswith(f"RP job={j1} after"))
    assert "pairs_to_buy=3" in after1 and "volume_tasks=1" in after1 and "serp_requests=1" in after1
    stored2 = next(ln for ln in out if ln.startswith(f"RP job={j2} stored"))
    assert "volume_pairs=0" in stored2 and "volume_paid_not_stored=3" in stored2
    after2 = next(ln for ln in out if ln.startswith(f"RP job={j2} after"))
    assert "keywords_asked=0" in after2 and "calls=0" in after2
    total = next(ln for ln in out if ln.startswith("RP total before"))
    assert "calls=3" in total and "volume_tasks=2" in total
    assert "RP total after calls=2 volume_tasks=1 serp_requests=1" in out
    assert any(ln.startswith("RP repeats endpoint=search_volume_bulk_queue no_keyword=True") for ln in out)
