"""services/seo_intelligence/tour_paa.py — AA-1122 item 5: the People Also Ask backfill of published tours.

A tour's PAA questions (``silver_aa_internal.seo_context.people_also_ask``, one row per tour) are what S1 and T2 hand
the writers. About 258 of 892 published tours had an empty list: the one SERP the S1 prefetch bought for the tour's
seed had no PAA box. This module fills them, cheapest first:

  1. **store** ($0) — the PAA segment research already bought for the tour's places (``acp_contract.search_demand``,
     the SERP market US and the US-PAA2 top-up rows): the rows whose keyword names one of the tour's strong places
     (its title places and its atom places, the s1_prefetch rule), best volume first, at most
     ``cis.seo.tour_paa_per_keyword`` per keyword and ``cis.seo.tour_paa_max`` in all (``s1_prefetch.paa_from_rows``);
  2. **serp** (paid, only when the run is ``paid`` and the per-run cap ``cis.spend.tour_paa_backfill_max_usd_per_run``
     and the research month cap allow it) — ONE live SERP for the tour's best place keyword whose SERP is not fresh in
     the store, through the research cache (read before, written back, the purchase checked against the run budget and
     the shared caps before it is posted);
  3. **none** (paid runs only) — nothing found (no stored question and no keyword left to buy, or a bought SERP
     with no PAA box): the row is marked so the next run waits for the SERP freshness window before it looks again
     (critic C4 of the S282
     design: an answered-but-empty SERP is never bought again in a loop).

Rules: a list S1 or T2 wrote (not empty) is never overwritten; ``fetched_at`` is never touched (it drives the S1 reuse
window); a row the backfill owns is refreshed when its ``paa_filled_at`` is older than the SERP freshness window
(``cis.seo.research_serp_fresh_days``), the same window as the research store. Writes provenance in ``paa_source`` /
``paa_filled_at`` (CIS 451).

Who owns a row (:data:`OWNED_SQL`): the backfill, when it set ``paa_source`` AND no S1 / T2 write came after it
(``fetched_at <= paa_filled_at``). The S1 upsert (shared/repository/seo_context_repository.py) sets ``fetched_at`` to
now() and leaves ``paa_source`` / ``paa_filled_at`` as they were, so a later S1 list takes the row back even though
its old provenance is still there: the backfill never overwrites it (S282 review: a stale ``paa_source`` alone would
let a paid run write [] 'none' over a list S1 bought). The row is S1's again until S1 leaves it empty.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import structlog

from services.seo_intelligence import research_settings as rs

logger = structlog.get_logger()

SERP_MARKET = "US"
STORE_MARKETS = ("US", "US-PAA2")    # the SERP market and the AA-779 top-up rows (real_questions)
STAGE = "a1_tour_paa"                # shared.dfs_call_log stage of the paid SERPs (and the cache hit rows)

# The backfill owns a seo_context row when it filled it and no S1 / T2 write came after (the S1 upsert moves
# fetched_at, never paa_source / paa_filled_at). Used with the alias sc in CANDIDATES_SQL; WRITE_SQL has no alias.
OWNED_SQL = ("({p}paa_source IS NOT NULL AND {p}paa_filled_at IS NOT NULL "
             "AND {p}fetched_at <= {p}paa_filled_at)")
_EMPTY_SQL = "coalesce({p}people_also_ask, '[]'::jsonb) IN ('[]'::jsonb, 'null'::jsonb)"

# Published, live tours whose PAA the backfill may (re)fill. $1 = the SERP window (interval), $2 = optional tour ids.
CANDIDATES_SQL = """
    SELECT sc.tour_id::text AS tour_id, rt.src_name, rt.country, rt.activities, sc.paa_source,
           """ + OWNED_SQL.format(p="sc.") + """ AS owned,
           coalesce(jsonb_array_length(CASE WHEN jsonb_typeof(sc.people_also_ask) = 'array'
                                            THEN sc.people_also_ask END), 0) AS paa_count
      FROM gold_aa_internal.published_tours pt
      JOIN silver_aa_internal.seo_context sc ON sc.tour_id = pt.tour_id
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.master_status = 'active' AND pt.deleted_at IS NULL
       AND ($2::uuid[] IS NULL OR pt.tour_id = ANY($2::uuid[]))
       AND (
             (NOT """ + OWNED_SQL.format(p="sc.") + """ AND """ + _EMPTY_SQL.format(p="sc.") + """)
          OR (""" + OWNED_SQL.format(p="sc.") + """ AND sc.paa_filled_at < now() - $1::interval)
       )
     ORDER BY sc.tour_id
"""
COVERAGE_SQL = """
    SELECT count(*) AS published,
           count(*) FILTER (WHERE jsonb_typeof(sc.people_also_ask) = 'array'
                              AND jsonb_array_length(sc.people_also_ask) > 0) AS with_paa
      FROM gold_aa_internal.published_tours pt
      LEFT JOIN silver_aa_internal.seo_context sc ON sc.tour_id = pt.tour_id
     WHERE pt.master_status = 'active' AND pt.deleted_at IS NULL
"""
STORE_SQL = """
    SELECT keyword, market, search_volume, people_also_ask, (paa_retrieved_on IS NOT NULL
           AND paa_retrieved_on > now() - $3::interval AND related_searches IS NOT NULL) AS serp_fresh
      FROM acp_contract.search_demand
     WHERE market = ANY($1::text[]) AND keyword ILIKE ANY($2::text[])
"""
# Never overwrites a list S1 / T2 wrote (a row the backfill does not own, not empty), even one S1 wrote between the
# plan and this write; never touches fetched_at.
WRITE_SQL = """
    UPDATE silver_aa_internal.seo_context
       SET people_also_ask = $2::jsonb, paa_source = $3, paa_filled_at = now()
     WHERE tour_id = $1::uuid
       AND (""" + OWNED_SQL.format(p="") + """ OR """ + _EMPTY_SQL.format(p="") + """)
"""


@dataclass
class TourPlan:
    tour_id: str
    name: str
    store_paa: list[str] = field(default_factory=list)
    buy_keyword: str | None = None      # the place keyword one SERP would be bought for (None = nothing to buy)
    refresh: bool = False               # a row the backfill filled before, past the SERP window


def _json_list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, list) else []


def plan_tour(spec: dict, rows: list[dict], per_keyword: int, most: int) -> tuple[list[str], str | None]:
    """(store PAA, keyword to buy a SERP for) of one tour from the store rows naming its places. Pure.

    Store PAA: rows naming a strong place of the tour (s1_prefetch.strong_terms), best volume first (ties: keyword),
    via s1_prefetch.paa_from_rows. Keyword to buy: the US row naming a strong place with the highest volume above 0
    whose SERP is not fresh (a fresh SERP with no question says the box is not there: never bought again), None when
    there is none."""
    from services.seo_intelligence.s1_prefetch import paa_from_rows, strong_terms

    terms = strong_terms(spec.get("places") or [])
    if not terms:
        return [], None
    hits = [r for r in rows if any(t in str(r["keyword"]).lower() for t in terms)]
    hits.sort(key=lambda r: (-(r.get("search_volume") or 0), str(r["keyword"])))
    paa = paa_from_rows([{"people_also_ask": _json_list(r.get("people_also_ask"))} for r in hits], per_keyword, most)
    buy = next((r["keyword"] for r in hits if r.get("market") == SERP_MARKET and (r.get("search_volume") or 0) > 0
                and not r.get("serp_fresh")), None)
    return paa, buy


async def _specs(conn, rows: list[dict]) -> list[dict]:
    from services.seo_intelligence.s1_prefetch import atom_places, tour_spec

    out = []
    for r in rows:
        spec = tour_spec({"tour_id": r["tour_id"], "src_name": r["src_name"], "country": r["country"],
                          "activities": r["activities"]})
        spec["places"] = list(dict.fromkeys(spec["places"] + await atom_places(conn, r["tour_id"])))
        spec["paa_source"] = r["paa_source"]
        spec["owned"] = bool(r["owned"])
        spec["paa_count"] = r["paa_count"]
        out.append(spec)
    return out


async def build_plan(pool, tour_ids: list[str] | None = None) -> list[TourPlan]:
    """The tours to (re)fill with what the store gives each and the keyword a SERP would be bought for. $0."""
    from services.seo_intelligence.research_cache import fresh_windows
    from services.seo_intelligence.s1_prefetch import paa_caps, strong_terms

    window = (await fresh_windows()).serp
    per_keyword, most = paa_caps()
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(CANDIDATES_SQL, window, tour_ids or None)]
        specs = await _specs(conn, rows)
        terms = sorted({t for s in specs for t in strong_terms(s["places"])})
        store = [dict(r) for r in await conn.fetch(STORE_SQL, list(STORE_MARKETS), [f"%{t}%" for t in terms],
                                                   window)] if terms else []
    plans = []
    for spec in specs:
        paa, buy = plan_tour(spec, store, per_keyword, most)
        plans.append(TourPlan(spec["tour_id"], spec["name"], paa, buy, refresh=spec["owned"]))
    return plans


def serp_price() -> float:
    """The worst-case price of one live advanced SERP (cis.spend.dfs_call_estimate_usd, label serp_advanced)."""
    from shared.cost_guard import dfs_research_prices
    return dfs_research_prices("live")[1]


async def preview(pool, tour_ids: list[str] | None = None, *, paid: bool = True) -> dict:
    """What a run would do, $0: tours to fill, filled free from the store, still empty, SERPs it would buy within the
    caps (per run and the research month left), and the estimate."""
    from shared import research_budget, settings

    plans = await build_plan(pool, tour_ids)
    from_store = sum(1 for p in plans if p.store_paa)
    still = [p for p in plans if not p.store_paa]
    buyable = [p for p in still if p.buy_keyword]
    price = serp_price()
    run_cap = float(settings.get(rs.TOUR_PAA_MAX_USD_PER_RUN)) if paid else 0.0
    month = await research_budget.load_research_month_cap(pool)
    money = min(run_cap, month.remaining())
    distinct = len({p.buy_keyword for p in buyable})
    fits = min(distinct, math.floor(money / price + 1e-9)) if price > 0 else distinct
    async with pool.acquire() as conn:
        cov = await conn.fetchrow(COVERAGE_SQL)
    published, with_paa = int(cov["published"] or 0), int(cov["with_paa"] or 0)
    return {
        "tours_to_fill": len(plans), "refreshes": sum(1 for p in plans if p.refresh),
        "fill_from_store": from_store, "still_empty_after_store": len(still),
        "serp_keywords": distinct, "serp_fits_caps": fits, "serp_unit_usd": price,
        "estimate_usd": round(fits * price, 4), "estimate_all_usd": round(distinct * price, 4),
        "nothing_to_buy": len(still) - len(buyable),
        "run_cap_usd": run_cap, "month": month.summary(), "paid": paid,
        "coverage": {"published": published, "with_paa": with_paa,
                     "pct": round(100.0 * with_paa / published, 1) if published else None},
        "sample": [{"tour_id": p.tour_id, "name": p.name, "store_questions": len(p.store_paa),
                    "buy_keyword": p.buy_keyword} for p in plans[:20]],
    }


@dataclass
class RunResult:
    filled_store: int = 0
    filled_serp: int = 0
    marked_none: int = 0
    serp_calls: int = 0
    serp_cached: int = 0
    skipped_cap: int = 0
    failed: int = 0
    stop_reason: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def _write(pool, tour_id: str, paa: list[str], source: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(WRITE_SQL, tour_id, json.dumps(paa), source)


async def _fill_from_store(pool, plans: list[TourPlan], paid: bool, out: RunResult) -> dict[str, list[TourPlan]]:
    """Write the store's questions; return buy keyword -> its tours. A paid run marks a tour with nothing to buy
    'none'; a store-only run marks nothing (a paid run may still buy for those tours)."""
    by_kw: dict[str, list[TourPlan]] = {}
    for p in plans:
        if p.store_paa:
            await _write(pool, p.tour_id, p.store_paa, "store")
            out.filled_store += 1
        elif paid and p.buy_keyword:
            by_kw.setdefault(p.buy_keyword, []).append(p)
        elif paid:
            await _write(pool, p.tour_id, [], "none")
            out.marked_none += 1
    return by_kw


async def _write_answer(pool, tours: list[TourPlan], paa: list[str], most: int, out: RunResult) -> None:
    for p in tours:
        await _write(pool, p.tour_id, paa[:most], "serp" if paa else "none")
        if paa:
            out.filled_serp += 1
        else:
            out.marked_none += 1


async def run(pool, *, paid: bool, client=None, dfs_budget=None, tour_ids: list[str] | None = None) -> dict:
    """Fill the planned tours: store first; then, when ``paid``, ONE SERP per distinct buy keyword through the research
    cache, under ``dfs_budget`` (its per-run cap and the shared month cap are checked before each post by the client).
    A budget stop leaves the rest untouched (they stay empty, the next run tries again); a failed SERP leaves its tours
    untouched. Returns the counts."""
    from services.seo_intelligence.research_cache import ResearchCache, fresh_windows, parse_serp
    from services.seo_intelligence.s1_prefetch import paa_caps
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
    from shared.cost_guard import BudgetExceeded

    out = RunResult()
    by_kw = await _fill_from_store(pool, await build_plan(pool, tour_ids), paid, out)
    if not by_kw:
        return out.as_dict()
    if client is None:
        raise ValueError("a paid tour PAA run needs a DataForSEO client built with a run budget")
    cache = ResearchCache(pool, stage=STAGE, windows=await fresh_windows(), meta={"job": "tour_paa_backfill"})
    loc = DFS_LOCATION_MAP[SERP_MARKET][0]
    _per_keyword, most = paa_caps()
    stored = await cache.serps(list(by_kw), SERP_MARKET)

    async def _buy(kw: str, tours: list[TourPlan]) -> list[str] | None:
        """The PAA of one bought SERP (written back to the store), or None when nothing was bought."""
        try:
            data = await client._serp_advanced(kw, loc, "en")
        except BudgetExceeded as exc:
            out.stop_reason = str(exc)
            out.skipped_cap += len(tours)
            return None
        except Exception as exc:  # noqa: BLE001 — one failed SERP: its tours stay as they are
            logger.warning("tour_paa_serp_failed", keyword=kw, error=str(exc)[:200])
            out.failed += len(tours)
            return None
        out.serp_calls += 1
        await cache.put_serp_answer(client, kw, SERP_MARKET, data)
        return parse_serp(client, data)[0]

    for kw, tours in by_kw.items():
        if kw in stored:            # a concurrent research run stored it meanwhile: nothing to buy
            out.serp_cached += 1
            paa = list(stored[kw].paa)
        elif out.stop_reason is not None:
            out.skipped_cap += len(tours)
            continue
        elif (paa := await _buy(kw, tours)) is None:
            continue
        await _write_answer(pool, tours, paa, most, out)
    await cache.log_hits("serp_advanced", count=out.serp_cached, location_code=loc, meta={"phase": "tour_paa"})
    return out.as_dict()


async def coverage(pool) -> dict:
    """Published active tours and how many have at least one PAA question ($0)."""
    async with pool.acquire() as conn:
        cov = await conn.fetchrow(COVERAGE_SQL)
    published, with_paa = int(cov["published"] or 0), int(cov["with_paa"] or 0)
    return {"published": published, "with_paa": with_paa,
            "pct": round(100.0 * with_paa / published, 1) if published else None}


__all__ = ["build_plan", "plan_tour", "preview", "run", "coverage", "TourPlan", "STAGE"]
