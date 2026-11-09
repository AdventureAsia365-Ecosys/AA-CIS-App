#!/usr/bin/env python3
"""AA-1055 — DataForSEO Standard queue trial: ONE volume task and ONE SERP task through the queue, with the cost
DataForSEO reports for each (approved S273, about 0.1 USD in all). Decides whether research runs on the queue
(services/seo_intelligence/dfs_queue.py, job payload "dfs_queue": "standard").

What it does:
  1. picks up to KEYWORDS (default 50) keywords that already have a live-bought US volume in acp_contract.search_demand
     (so the queue's answer can be compared with the live one), and the best of them for the SERP;
  2. posts ONE search-volume task (US, English) and ONE organic SERP task (US, English) to the Standard queue, waits
     for both (free polls), and prints per task: the cost from the task_post answer, the wait, and for volume how
     many answers equal the stored live value; for the SERP how many PAA questions / related searches / organic
     domains came back;
  3. prints the live unit costs of the last 30 days from shared.dfs_call_log, for the comparison.
Writes nothing to the demand store. Each post is logged to shared.dfs_call_log (endpoints search_volume_bulk_queue /
serp_advanced_queue, stage adhoc_aa1055) by the client, so the spend is on the External Spend page.

Run by the orchestrator in the CIS api container with the root repo's ECS exec helper:
  tools/ecs/ecsx.sh <this file> [KEYWORDS=50] [TIMEOUT_S=1800]
Needs the AA-1055 PR 2 image (the queue client) deployed. Refuses to run when the DataForSEO connection is switched
off in the admin or the shared circuit is open (the client's own checks).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

KEYWORDS = int(os.environ.get("KEYWORDS", "50"))
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "1800"))

PICK_SQL = """
    SELECT keyword, search_volume FROM acp_contract.search_demand
    WHERE market = 'US' AND search_volume IS NOT NULL AND retrieved_on > now() - interval '365 days'
    ORDER BY search_volume DESC, keyword LIMIT $1
"""
LIVE_COST_SQL = """
    SELECT endpoint, count(*)::int AS calls, round(avg(cost_usd)::numeric, 6)::float8 AS avg_cost_usd
    FROM shared.dfs_call_log
    WHERE endpoint = ANY($1::text[]) AND fetched_live AND status = 'ok' AND created_at > now() - interval '30 days'
    GROUP BY endpoint ORDER BY endpoint
"""


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=2)


async def main() -> dict:
    from services.seo_intelligence import dfs_queue as q
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP

    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            picked = [dict(r) for r in await conn.fetch(PICK_SQL, KEYWORDS)]
            live = [dict(r) for r in await conn.fetch(LIVE_COST_SQL, ["search_volume_bulk", "serp_advanced"])]
    finally:
        await pool.close()
    if not picked:
        return {"error": "no US keyword with a live volume in the store"}

    client = DataForSEOClient(stage="adhoc_aa1055")
    client.meta = {"job": "adhoc_aa1055_queue_trial"}
    queue = q.StandardQueue(client, poll_s=15.0, timeout_s=TIMEOUT_S, direct_after_s=120.0)
    loc = DFS_LOCATION_MAP["US"][0]
    out: dict = {"keywords": len(picked), "live_unit_cost_30d": live}

    # 1. one volume task
    kws = [r["keyword"] for r in picked]
    started = time.monotonic()
    posted = await queue._post(q._VOLUME, q.VOLUME_ENDPOINT, [{
        "keywords": kws, "location_code": loc, "language_code": "en", "tag": "aa1055-volume",
    }], keyword_count=len(kws), location_code=loc)
    answers = await queue.wait(q._VOLUME, posted)
    got = answers.get("aa1055-volume")
    vol = {"task_id": posted[0].task_id, "cost_usd": posted[0].cost_usd,
           "wait_s": round(time.monotonic() - started, 1)}
    if isinstance(got, dict):
        volumes = q.parse_volumes(got, kws)
        stored = {r["keyword"]: r["search_volume"] for r in picked}
        vol.update({"answered": sum(1 for v in volumes.values() if v is not None),
                    "equal_to_live": sum(1 for k, v in volumes.items() if v == stored.get(k)),
                    "task_get_cost_usd": got.get("cost")})
    else:
        vol["error"] = str(got)
    out["volume_task"] = vol

    # 2. one SERP task, for the keyword with the highest stored volume
    seed = picked[0]["keyword"]
    started = time.monotonic()
    posted = await queue._post(q._SERP, q.SERP_ENDPOINT, [{
        "keyword": seed, "location_code": loc, "language_code": "en", "tag": "aa1055-serp",
    }], keyword_count=1, location_code=loc, keyword=seed)
    answers = await queue.wait(q._SERP, posted)
    got = answers.get("aa1055-serp")
    serp = {"task_id": posted[0].task_id, "cost_usd": posted[0].cost_usd,
            "wait_s": round(time.monotonic() - started, 1)}
    if isinstance(got, dict):
        serp.update({"paa": len(client._parse_paa(got)), "related": len(client._parse_related(got)),
                     "organic_domains": len(client._parse_organic_domains(got)),
                     "task_get_cost_usd": got.get("cost")})
    else:
        serp["error"] = str(got)
    out["serp_task"] = serp
    out["total_cost_usd"] = round(sum(x.get("cost_usd") or 0.0 for x in (vol, serp)), 6)
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, default=str))
