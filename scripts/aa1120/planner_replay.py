#!/usr/bin/env python3
"""AA-1120 — $0 replay of the DataForSEO research plans of one day: what each run bought, and what the AA-1120 planner
(research cache read first, one task per distinct (keyword, market), cumulative across the day's runs, paid queue
tasks collected instead of posted again) would buy for the same places. READ ONLY.

Runs in the CIS api (or worker) container (python3 + asyncpg, DATABASE_URL = cis_app, ssl=require), through
tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1120/planner_replay.py DAY=2026-11-09 [DAYS=1] [MONTH=2026-11-01]

Self-contained (no app import): it runs before or after the AA-1120 deploy. Per segment_research job created in
[DAY, DAY + DAYS), in creation order:
  - before: its rows in shared.dfs_call_log (job_id): calls (ledger rows), paid calls, USD, and the keywords / SERP
    tasks its paid posts carried (sum keyword_count), by endpoint;
  - stored: the (keyword, market) volumes and the SERPs it wrote (search_demand.retrieved_on / paa_retrieved_on in the
    job window): what reached the store. Paid keywords minus stored keywords = bought but never stored (a task paid
    and not collected, or bought twice);
  - after: the plan rebuilt from the DB: the places the job logged (segment_research_log tier volume / serp in its
    window), their buyable map keywords (acp_contract.place_keyword, rank order, first 4, enforced Jev rejects and
    rule-dropped rows left out), minus every (keyword, market) already fresh in the store before the job started or
    planned by an earlier job of the replay; volume tasks = per market ceil(distinct / 1000); SERPs = the distinct
    SERP keywords it stored (posts of 100 in queue mode). Calls = volume tasks + SERP requests.
Then the day's totals (before vs after, reduction %) and, for MONTH .. now, the "repeated endpoint + keyword +
location" count split by keyword NULL (a queue post or a bulk volume task logs no keyword, so every such row of one
location looks like a repeat of the same pair) vs a real keyword.

Caveat: a store row rewritten after the job (a later run) moves out of the job window; with the 365-day windows the
rows of a first purchase are not rewritten. Output: `RP <scope> <metric>=<value>` lines only (counts, USD, endpoint
names, job ids); no keyword text. Every section is isolated: a failure prints `ERR <section> <Exception>`.

Tables / columns: AA-Ecosys docs/architecture/db-schema-reference.md (shared.job, shared.dfs_call_log,
acp_contract.search_demand, acp_contract.segment_research_log, acp_contract.place_keyword).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import date, datetime, timedelta, timezone

VOLUME_TASK_MAX = 1000
SERP_POST_MAX = 100
KEYWORDS_PER_PLACE = 4
FRESH_DAYS = 365   # the seeded default of cis.seo.research_volume_fresh_days (a replay of the past, not policy)
RULE_PREFIX = "rule:"
VOLUME_ENDPOINTS = ("search_volume_bulk", "search_volume_bulk_queue")
SERP_ENDPOINTS = ("serp_advanced", "serp_advanced_queue")

JOBS_SQL = """
    SELECT id::text AS id, status, attempt, created_at, started_at, finished_at, payload
    FROM shared.job WHERE kind = 'segment_research' AND created_at >= $1 AND created_at < $2
    ORDER BY created_at
"""
LEDGER_SQL = """
    SELECT endpoint, cache_hit, fetched_live, count(*) AS n, coalesce(sum(cost_usd), 0)::float AS usd,
           coalesce(sum(keyword_count), 0)::int AS kws
    FROM shared.dfs_call_log WHERE job_id = $1::uuid GROUP BY 1, 2, 3 ORDER BY 1
"""
STORED_VOLUMES_SQL = """
    SELECT market, count(*)::int AS n FROM acp_contract.search_demand
    WHERE retrieved_on >= $1 AND retrieved_on <= $2 GROUP BY market
"""
STORED_SERPS_SQL = """
    SELECT count(*)::int AS n FROM acp_contract.search_demand
    WHERE paa_retrieved_on >= $1 AND paa_retrieved_on <= $2
"""
NEEDS_SQL = """
    SELECT l.market, k.subject AS place, k.keyword, k.rank, k.jev_zone, k.jev_mode
    FROM acp_contract.segment_research_log l
    JOIN acp_contract.place_keyword k ON k.subject_kind = 'place' AND k.subject = l.canonical_place
    WHERE l.tier = 'volume' AND l.researched_at >= $1 AND l.researched_at <= $2
"""
FRESH_BEFORE_SQL = """
    SELECT keyword, market FROM acp_contract.search_demand
    WHERE keyword = ANY($1::text[]) AND retrieved_on < $2 AND retrieved_on > $2 - make_interval(days => $3)
"""
REPEATS_SQL = """
    SELECT endpoint, (keyword IS NULL) AS no_keyword, count(*)::int AS pairs, sum(n)::int AS calls,
           coalesce(sum(n) FILTER (WHERE n > 1), 0)::int AS calls_on_repeated_pairs, max(n)::int AS max_repeats
    FROM (SELECT endpoint, keyword, location_code, count(*) AS n FROM shared.dfs_call_log
          WHERE created_at >= $1 AND fetched_live GROUP BY 1, 2, 3) s
    GROUP BY 1, 2 ORDER BY calls DESC
"""


# ── pure ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def buyable(rows: list[dict]) -> dict[tuple[str, str], list[str]]:
    """(place, market) -> its buyable keywords: rank order, enforced Jev rejects and rule-dropped rows left out, the
    first KEYWORDS_PER_PLACE (segment_research_batch.keywords_from_map)."""
    by: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for r in rows:
        if r.get("jev_zone") == "reject" and r.get("jev_mode") == "enforce":
            continue
        if str(r.get("jev_mode") or "").startswith(RULE_PREFIX):
            continue
        by.setdefault((r["place"], r["market"]), []).append((int(r.get("rank") or 0), r["keyword"]))
    return {k: [kw for _r, kw in sorted(v)][:KEYWORDS_PER_PLACE] for k, v in by.items()}


def plan_after(needed: dict[tuple[str, str], list[str]], fresh_before: set[tuple[str, str]],
               planned: set[tuple[str, str]], serps: int, queue: bool) -> dict:
    """The AA-1120 plan of one job: distinct (keyword, market) pairs not fresh before it and not planned by an earlier
    job, in tasks of VOLUME_TASK_MAX per market; SERPs = the distinct SERP keywords (posts of 100 in queue mode).
    Updates `planned`. Pure apart from that set."""
    asked = sum(len(v) for v in needed.values())
    pairs = {(kw, m) for (_p, m), kws in needed.items() for kw in kws}
    to_buy = pairs - fresh_before - planned
    planned.update(to_buy)
    per_market: dict[str, int] = {}
    for _kw, m in to_buy:
        per_market[m] = per_market.get(m, 0) + 1
    volume_tasks = sum(math.ceil(n / VOLUME_TASK_MAX) for n in per_market.values())
    serp_requests = math.ceil(serps / SERP_POST_MAX) if queue else serps
    return {"keywords_asked": asked, "pairs_distinct": len(pairs), "pairs_fresh_before": len(pairs & fresh_before),
            "pairs_to_buy": len(to_buy), "volume_tasks": volume_tasks, "serps": serps,
            "serp_requests": serp_requests, "calls": volume_tasks + serp_requests}


def before_of(ledger: list[dict]) -> dict:
    """What a job's ledger rows say: rows, paid rows, USD, paid volume keywords and paid SERP tasks."""
    paid = [r for r in ledger if r.get("fetched_live")]
    return {
        "rows": sum(int(r["n"]) for r in ledger),
        "calls": sum(int(r["n"]) for r in paid),
        "usd": round(sum(float(r["usd"] or 0) for r in paid), 4),
        "volume_tasks": sum(int(r["n"]) for r in paid if r["endpoint"] in VOLUME_ENDPOINTS),
        "volume_keywords_paid": sum(int(r["kws"]) for r in paid if r["endpoint"] in VOLUME_ENDPOINTS),
        "serp_requests": sum(int(r["n"]) for r in paid if r["endpoint"] in SERP_ENDPOINTS),
        "serp_tasks_paid": sum(int(r["kws"]) for r in paid if r["endpoint"] in SERP_ENDPOINTS),
        "cache_hit_rows": sum(int(r["n"]) for r in ledger if r.get("cache_hit")),
    }


def reduction(before: int, after: int) -> float:
    return round(100.0 * (before - after) / before, 1) if before else 0.0


def day_bounds(day: str, days: int) -> tuple[datetime, datetime]:
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=max(days, 1))


def line(scope: str, **metrics) -> str:
    return "RP " + scope + " " + " ".join(f"{k}={v}" for k, v in metrics.items())


# ── DB ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def section(name: str, coro):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — one failed read never stops the report
        print(f"ERR {name} {type(exc).__name__}: {str(exc)[:200]}")
        return None


async def replay_job(conn, job: dict, planned: set) -> tuple[dict, dict] | None:
    start = job["started_at"] or job["created_at"]
    end = job["finished_at"] or datetime.now(timezone.utc)
    payload = job["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    queue = (payload or {}).get("dfs_queue") == "standard"
    ledger = [dict(r) for r in await conn.fetch(LEDGER_SQL, job["id"])]
    before = before_of(ledger)
    stored_v = {r["market"]: r["n"] for r in await conn.fetch(STORED_VOLUMES_SQL, start, end)}
    stored_s = await conn.fetchval(STORED_SERPS_SQL, start, end) or 0
    needed = buyable([dict(r) for r in await conn.fetch(NEEDS_SQL, start, end)])
    kws = sorted({kw for v in needed.values() for kw in v})
    fresh = {(r["keyword"], r["market"]) for r in await conn.fetch(FRESH_BEFORE_SQL, kws, start, FRESH_DAYS)} \
        if kws else set()
    after = plan_after(needed, fresh, planned, int(stored_s), queue)
    stored_total = sum(stored_v.values())
    print(line(f"job={job['id']}", status=job["status"], attempt=job["attempt"],
               markets=",".join((payload or {}).get("markets") or []), queue="standard" if queue else "live",
               minutes=round((end - start).total_seconds() / 60, 1)))
    print(line(f"job={job['id']} before", **before))
    print(line(f"job={job['id']} stored", volume_pairs=stored_total, serps=stored_s,
               volume_paid_not_stored=max(before["volume_keywords_paid"] - stored_total, 0),
               serp_paid_not_stored=max(before["serp_tasks_paid"] - int(stored_s), 0)))
    print(line(f"job={job['id']} after", **after))
    return before, after


async def report(conn, day: str, days: int, month: str) -> None:
    start, end = day_bounds(day, days)
    jobs = await section("jobs", conn.fetch(JOBS_SQL, start, end)) or []
    print(line("day", day=day, days=days, jobs=len(jobs)))
    planned: set = set()
    tot_before = {"calls": 0, "usd": 0.0, "volume_tasks": 0, "serp_requests": 0}
    tot_after = {"calls": 0, "volume_tasks": 0, "serp_requests": 0}
    for job in jobs:
        got = await section(f"job_{job['id']}", replay_job(conn, dict(job), planned))
        if got is None:
            continue
        b, a = got
        for k in tot_before:
            tot_before[k] += b[k]
        for k in tot_after:
            tot_after[k] += a[k]
    tot_before["usd"] = round(tot_before["usd"], 4)
    print(line("total before", **tot_before))
    print(line("total after", **tot_after))
    print(line("total", calls_reduction_pct=reduction(tot_before["calls"], tot_after["calls"]),
               volume_tasks_reduction_pct=reduction(tot_before["volume_tasks"], tot_after["volume_tasks"])))
    m_start = datetime.fromisoformat(month).replace(tzinfo=timezone.utc)
    for r in await section("repeats", conn.fetch(REPEATS_SQL, m_start)) or []:
        print(line(f"repeats endpoint={r['endpoint']} no_keyword={r['no_keyword']}", pairs=r["pairs"],
                   calls=r["calls"], calls_on_repeated_pairs=r["calls_on_repeated_pairs"],
                   max_repeats=r["max_repeats"]))


async def main() -> None:
    import asyncpg

    day = os.environ.get("DAY") or "2026-11-09"
    days = int(os.environ.get("DAYS") or 1)
    month = os.environ.get("MONTH") or day[:8] + "01"
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        await report(conn, day, days, month)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
