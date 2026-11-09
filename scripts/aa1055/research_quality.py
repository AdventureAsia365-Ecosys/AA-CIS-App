#!/usr/bin/env python3
"""AA-1055 — quality of one search-demand research run (is the cheaper Standard queue as good?). READ ONLY.

Runs in the CIS api (or worker) container (python3 + asyncpg, DATABASE_URL = cis_app, ssl=require), through
tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1055/research_quality.py JOB=<segment_research job id> [SAMPLE=30]

The run's window is the job's created_at .. finished_at (now while it runs): a store row written in it is the run's.
For that job it prints, per market:
  (a) volume agreement: the keywords whose volume the run bought (search_demand.retrieved_on in the window) that S1
      also bought live before the run, for the same market (silver_aa_internal.seo_context keyword ideas, read as the
      free copy reads them, services/acp_contract/seo_context_copy.plan_copy): how many, the share with an equal
      volume, the share within 10 %, the median relative difference. Research's own older live volumes are not kept
      (the store has one row per keyword and market), so S1's purchase is the only live reference left;
  (b) SERP richness: the SERPs the run stored (paa_retrieved_on in the window): share with >= 3 People Also Ask
      questions, with >= 1 related search, with >= 1 organic domain, the median / mean organic domains per SERP and
      the distinct domains;
  (c) Jev keyword map: for the places the run logged (segment_research_log.researched_at in the window), the keywords
      of their map (acp_contract.place_keyword) kept vs rejected by an enforced Jev reject, and the other zones;
  (d) demand landing: the Segments of those places (atom_segment.canonical_place) whose current ranking row
      (atom_ranking, superseded_at NULL) in that market carries a demand volume, and how many were recomputed since
      the run started;
  plus the run itself (status, queue, estimate) and its DataForSEO bill per stage and endpoint (dfs_call_log.job_id).

Only counts, shares, money and stage / endpoint names are printed, as `RQ <metric>=<value>` lines, EXCEPT one random
sample of at most SAMPLE (<= 30) `keyword|place|kept|rejected` lines per market for a relevance panel, printed between
`=== RQ SAMPLE BEGIN market=<m> n=<k> ===` and `=== RQ SAMPLE END market=<m> ===`. The session is READ ONLY (a write
raises); no job, DataForSEO, LLM or Jev call. Every section is isolated: a failure prints `ERR <section> <Exception>`
and the script goes on. A later run that researched the same place again moves its log row out of this window.

Tables / columns: AA-Ecosys docs/architecture/db-schema-reference.md (shared.job, shared.dfs_call_log,
acp_contract.search_demand, acp_contract.segment_research_log, acp_contract.place_keyword, acp_contract.atom_segment,
acp_contract.atom_ranking, silver_aa_internal.seo_context).
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import uuid

sys.path.insert(0, os.getcwd())

MAX_SAMPLE = 30
NEAR = 0.10

JOB_SQL = """
    SELECT id, kind, status, payload, result, created_at, started_at,
           coalesce(finished_at, now()) AS window_end, finished_at IS NULL AS open
    FROM shared.job WHERE id = $1::uuid
"""

BILL_SQL = """
    SELECT coalesce(stage, '(none)') AS stage, endpoint, count(*) AS n,
           coalesce(sum(cost_usd), 0)::float AS cost, coalesce(sum(keyword_count), 0) AS keywords
    FROM shared.dfs_call_log WHERE job_id = $1::uuid
    GROUP BY 1, 2 ORDER BY cost DESC, stage, endpoint
"""

JOB_VOLUMES_SQL = """
    SELECT keyword, market, search_volume
    FROM acp_contract.search_demand
    WHERE market = ANY($3::text[]) AND search_volume IS NOT NULL AND retrieved_on BETWEEN $1 AND $2
"""

SEO_CONTEXT_SQL = """
    SELECT keyword_search, keyword_ideas, people_also_ask, related_keywords, cache_key, fetched_at
    FROM silver_aa_internal.seo_context
    WHERE fetched_at < $1
"""

SERP_SQL = """
    WITH s AS (
        SELECT coalesce(serp_location, market) AS market,
               CASE WHEN jsonb_typeof(people_also_ask) = 'array' THEN jsonb_array_length(people_also_ask) ELSE 0 END
                   AS paa_n,
               CASE WHEN jsonb_typeof(related_searches) = 'array' THEN jsonb_array_length(related_searches) ELSE 0 END
                   AS rel_n,
               CASE WHEN jsonb_typeof(serp_domains) = 'array' THEN jsonb_array_length(serp_domains) ELSE 0 END
                   AS dom_n
        FROM acp_contract.search_demand
        WHERE paa_retrieved_on BETWEEN $1 AND $2
    )
    SELECT market, count(*) AS serps,
           count(*) FILTER (WHERE paa_n >= 3) AS paa3,
           count(*) FILTER (WHERE paa_n >= 1) AS paa1,
           count(*) FILTER (WHERE rel_n >= 1) AS rel1,
           count(*) FILTER (WHERE dom_n >= 1) AS dom1,
           coalesce(avg(dom_n), 0)::float AS dom_mean,
           coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY dom_n), 0)::float AS dom_median
    FROM s GROUP BY market ORDER BY market
"""

SERP_DOMAINS_SQL = """
    SELECT coalesce(d.serp_location, d.market) AS market, count(DISTINCT e.domain) AS distinct_domains
    FROM acp_contract.search_demand d
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(d.serp_domains) = 'array' THEN d.serp_domains ELSE '[]'::jsonb END) AS e(domain)
    WHERE d.paa_retrieved_on BETWEEN $1 AND $2
    GROUP BY 1 ORDER BY 1
"""

# The places the run logged, per market (a place logged for both tiers counts once per market).
_PLACES_CTE = """
    WITH p AS (
        SELECT DISTINCT canonical_place, market
        FROM acp_contract.segment_research_log
        WHERE researched_at BETWEEN $1 AND $2
    )
"""

MAP_SQL = _PLACES_CTE + """
    SELECT p.market, count(DISTINCT p.canonical_place) AS places,
           count(DISTINCT k.subject) AS places_with_map,
           count(k.keyword) AS keywords,
           count(k.keyword) FILTER (WHERE k.jev_zone = 'reject' AND k.jev_mode = 'enforce') AS rejected,
           count(k.keyword) FILTER (WHERE k.jev_zone = 'reject' AND k.jev_mode IS DISTINCT FROM 'enforce')
               AS rejected_not_enforced,
           count(k.keyword) FILTER (WHERE k.jev_zone = 'accept') AS zone_accept,
           count(k.keyword) FILTER (WHERE k.jev_zone = 'grey') AS zone_grey,
           count(k.keyword) FILTER (WHERE k.jev_zone IN ('error', 'skipped', 'not_asked') OR k.jev_zone IS NULL)
               AS zone_other
    FROM p
    LEFT JOIN acp_contract.place_keyword k ON k.subject_kind = 'place' AND k.subject = p.canonical_place
    GROUP BY p.market ORDER BY p.market
"""

SAMPLE_SQL = _PLACES_CTE + """
    SELECT k.keyword, p.canonical_place AS place,
           (k.jev_zone = 'reject' AND k.jev_mode = 'enforce') AS rejected
    FROM p
    JOIN acp_contract.place_keyword k ON k.subject_kind = 'place' AND k.subject = p.canonical_place
    WHERE p.market = $3
    ORDER BY random() LIMIT $4
"""

LANDING_SQL = _PLACES_CTE + """
    SELECT r.market, count(DISTINCT r.segment_id) AS segments,
           count(DISTINCT r.segment_id) FILTER (WHERE r.demand_volume IS NOT NULL) AS with_demand,
           count(DISTINCT r.segment_id) FILTER (WHERE r.computed_at >= $1) AS recomputed,
           count(DISTINCT r.segment_id) FILTER (WHERE r.computed_at >= $1 AND r.demand_volume IS NOT NULL)
               AS recomputed_with_demand
    FROM p
    JOIN acp_contract.atom_segment s ON s.canonical_place = p.canonical_place
    JOIN acp_contract.atom_ranking r ON r.segment_id = s.segment_id AND r.market = p.market
    WHERE r.superseded_at IS NULL
    GROUP BY r.market ORDER BY r.market
"""

LANDING_PLACES_SQL = _PLACES_CTE + """
    SELECT p.market, count(DISTINCT s.segment_id) AS segments_of_places
    FROM p JOIN acp_contract.atom_segment s ON s.canonical_place = p.canonical_place
    GROUP BY p.market ORDER BY p.market
"""


# ── pure helpers (unit-tested) ────────────────────────────────────────────────────────────────────────────────────

def out(metric: str, value) -> None:
    print(f"RQ {metric}={value}")


def share(part, whole) -> str:
    return "null" if not whole else f"{part / whole:.3f}"


def money(v) -> str:
    return f"{float(v or 0.0):.4f}"


def sample_size(raw: str | None) -> int:
    try:
        n = int(raw) if raw not in (None, "") else MAX_SAMPLE
    except ValueError:
        n = MAX_SAMPLE
    return max(0, min(n, MAX_SAMPLE))


def job_id_from(raw: str | None) -> str:
    """The JOB env value as a canonical UUID, or SystemExit (never a query with a bad id)."""
    try:
        return str(uuid.UUID(str(raw or "").strip()))
    except ValueError:
        raise SystemExit("JOB=<segment_research job id> is required (a UUID)") from None


def as_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def rel_diff(queued: int, live: int) -> float:
    """|queued - live| relative to the live value (a live 0 compares against 1, so 0 vs 0 is 0)."""
    return abs(queued - live) / max(abs(live), 1)


def volume_agreement(pairs: list[tuple[int, int]]) -> dict:
    """(queued volume, live volume) pairs -> n, share equal, share within NEAR, median relative difference."""
    if not pairs:
        return {"n": 0, "equal_share": "null", "near_share": "null", "median_rel_diff": "null"}
    diffs = [rel_diff(q, v) for q, v in pairs]
    return {
        "n": len(pairs),
        "equal_share": share(sum(1 for q, v in pairs if q == v), len(pairs)),
        "near_share": share(sum(1 for d in diffs if d <= NEAR), len(pairs)),
        "median_rel_diff": f"{statistics.median(diffs):.3f}",
    }


def agreement_by_market(bought: list[dict], live: dict[tuple[str, str], tuple[int, object]]) -> dict[str, dict]:
    """market -> volume_agreement of the run's bought rows that have a live (keyword, market) reference."""
    pairs: dict[str, list[tuple[int, int]]] = {}
    for r in bought:
        ref = live.get((r["keyword"], r["market"]))
        if ref is not None:
            pairs.setdefault(r["market"], []).append((int(r["search_volume"]), int(ref[0])))
    return {m: volume_agreement(p) for m, p in sorted(pairs.items())}


def sample_line(keyword: str, place: str, rejected: bool) -> str:
    clean = [str(x).replace("|", "/").replace("\n", " ").strip() for x in (keyword, place)]
    return f"{clean[0]}|{clean[1]}|{'rejected' if rejected else 'kept'}"


# ── report ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def section(name: str, coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — one failed query never hides the others
        print(f"ERR {name} {type(e).__name__}")
        return None


def _live_reference(rows) -> dict:
    """(keyword, market) -> (volume, fetched_at) from S1's seo_context rows, as the free copy reads them."""
    from services.acp_contract.seo_context_copy import plan_copy
    return plan_copy(rows).volumes


async def report(conn, job_id: str, sample: int) -> None:
    job = await conn.fetchrow(JOB_SQL, job_id)
    if job is None:
        out("job", "not_found")
        return
    payload, result = as_dict(job["payload"]), as_dict(job["result"])
    start, end = job["created_at"], job["window_end"]
    markets = [str(m).upper() for m in (payload.get("markets") or [])]
    queue = result.get("dfs_queue") or payload.get("dfs_queue") or "live"
    estimate = as_dict(result.get("estimate"))
    out("job.kind", job["kind"])
    out("job.status", job["status"])
    out("job.open", str(bool(job["open"])).lower())
    out("job.markets", ",".join(markets) or "none")
    out("job.queue", queue)
    out("job.places_selected", result.get("places_selected", "null"))
    out("job.estimate_dfs_usd", money(estimate.get("dfs_usd")))
    out("job.estimate_dfs_usd_cap", money(estimate.get("dfs_usd_cap")))
    out("job.unit_usd", json.dumps(estimate.get("unit_usd")) if estimate.get("unit_usd") else "null")
    if job["kind"] != "segment_research":
        out("job.warning", "not_a_segment_research_job")

    bill = await section("bill", conn.fetch(BILL_SQL, job_id))
    if bill is not None:
        for r in bill:
            out(f"bill.{r['stage']}.{r['endpoint']}", f"rows={r['n']} cost={money(r['cost'])} keywords={r['keywords']}")
        out("bill.total", money(sum(r["cost"] for r in bill)))
        out("bill.no_stage_rows", sum(r["n"] for r in bill if r["stage"] in ("(none)", "unattributed")))

    bought = await section("volume_bought", conn.fetch(JOB_VOLUMES_SQL, start, end, markets))
    if bought is not None:
        per_market: dict[str, int] = {}
        for r in bought:
            per_market[r["market"]] = per_market.get(r["market"], 0) + 1
        for m in markets:
            out(f"volume.{m}.bought_keywords", per_market.get(m, 0))
        seo = await section("volume_live_reference", conn.fetch(SEO_CONTEXT_SQL, start))
        if seo is not None:
            agreement = agreement_by_market([dict(r) for r in bought], _live_reference(seo))
            for m in markets:
                for k, v in agreement.get(m, volume_agreement([])).items():
                    out(f"volume.{m}.vs_live.{k}", v)

    serps = await section("serp", conn.fetch(SERP_SQL, start, end))
    for r in serps or []:
        m = r["market"]
        out(f"serp.{m}.serps", r["serps"])
        out(f"serp.{m}.paa_ge3_share", share(r["paa3"], r["serps"]))
        out(f"serp.{m}.paa_ge1_share", share(r["paa1"], r["serps"]))
        out(f"serp.{m}.related_ge1_share", share(r["rel1"], r["serps"]))
        out(f"serp.{m}.domains_ge1_share", share(r["dom1"], r["serps"]))
        out(f"serp.{m}.domains_median", f"{r['dom_median']:.1f}")
        out(f"serp.{m}.domains_mean", f"{r['dom_mean']:.2f}")
    for r in await section("serp_domains", conn.fetch(SERP_DOMAINS_SQL, start, end)) or []:
        out(f"serp.{r['market']}.distinct_domains", r["distinct_domains"])

    maps = await section("keyword_map", conn.fetch(MAP_SQL, start, end))
    map_markets = []
    for r in maps or []:
        m = r["market"]
        map_markets.append(m)
        kept = r["keywords"] - r["rejected"]
        out(f"map.{m}.places", r["places"])
        out(f"map.{m}.places_with_map", r["places_with_map"])
        out(f"map.{m}.keywords", r["keywords"])
        out(f"map.{m}.kept", kept)
        out(f"map.{m}.rejected", r["rejected"])
        out(f"map.{m}.kept_share", share(kept, r["keywords"]))
        out(f"map.{m}.rejected_share", share(r["rejected"], r["keywords"]))
        out(f"map.{m}.rejected_not_enforced", r["rejected_not_enforced"])
        out(f"map.{m}.zones", f"accept={r['zone_accept']} grey={r['zone_grey']} other={r['zone_other']}")

    for r in await section("landing_places", conn.fetch(LANDING_PLACES_SQL, start, end)) or []:
        out(f"landing.{r['market']}.segments_of_places", r["segments_of_places"])
    for r in await section("landing", conn.fetch(LANDING_SQL, start, end)) or []:
        m = r["market"]
        out(f"landing.{m}.segments_ranked", r["segments"])
        out(f"landing.{m}.with_demand", r["with_demand"])
        out(f"landing.{m}.with_demand_share", share(r["with_demand"], r["segments"]))
        out(f"landing.{m}.recomputed_since_run", r["recomputed"])
        out(f"landing.{m}.recomputed_with_demand", r["recomputed_with_demand"])

    if sample <= 0:
        return
    for m in map_markets:
        rows = await section(f"sample_{m}", conn.fetch(SAMPLE_SQL, start, end, m, sample))
        if rows is None:
            continue
        print(f"=== RQ SAMPLE BEGIN market={m} n={len(rows)} ===")
        for r in rows:
            print(sample_line(r["keyword"], r["place"], bool(r["rejected"])))
        print(f"=== RQ SAMPLE END market={m} ===")


async def main() -> None:
    import asyncpg

    job_id = job_id_from(os.environ.get("JOB"))
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        await report(conn, job_id, sample_size(os.environ.get("SAMPLE")))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
