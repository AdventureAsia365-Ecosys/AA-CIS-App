"""services/acp_contract/seo_context_copy.py — AA-1055: copy what S1 already bought (silver_aa_internal.seo_context)
into the shared demand store (acp_contract.search_demand), free.

S1 bought keyword ideas (with volume), People Also Ask and related searches per tour, US only, and kept them in its own
per-tour table: no other step reads them (design docs/audits/2026-11-08-S273-session/dfs-cost-audit/design.vi.md
section 3, "Không ghi đè dữ liệu tháng 10"). This copies them into the store every step reads:

  - each keyword idea with a measured volume -> a volume row (keyword, market), dated the seo_context row's fetched_at;
  - the seed's PAA and related searches -> the seed's row: people_also_ask, related_searches, paa_retrieved_on =
    fetched_at, serp_location = market.

Merge, never overwrite: a volume is written only where the store has none or an older one, a SERP only where the row
has no SERP date or an older one. A seed row created only for its SERP has no volume (search_volume NULL, retrieved_on
at the epoch, so research still buys its volume). The market comes from the row's cache_key location code (S1 bought
in the AA tenant's market, US). No DataForSEO or LLM call. Run by scripts/aa1055_seo_context_to_store.py (dry run by
default).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from services.seo_intelligence.seed_builder import LOCATION_CODE_TO_MARKET

AA_TENANT = "00000000-0000-0000-0000-000000000001"
DEFAULT_MARKET = "US"

READ_SQL = """
    SELECT keyword_search, keyword_ideas, people_also_ask, related_keywords, cache_key, fetched_at
    FROM silver_aa_internal.seo_context
    WHERE tenant_id = $1::uuid
"""

UPSERT_VOLUME_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, retrieved_on)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (keyword, market) DO UPDATE SET
        search_volume = excluded.search_volume, retrieved_on = excluded.retrieved_on
    WHERE acp_contract.search_demand.retrieved_on < excluded.retrieved_on
"""

UPSERT_SERP_SQL = """
    INSERT INTO acp_contract.search_demand (keyword, market, search_volume, people_also_ask, retrieved_on,
                                            related_searches, paa_retrieved_on, serp_location)
    VALUES ($1, $2, NULL, $3::jsonb, 'epoch'::timestamptz, $4::jsonb, $5, $2)
    ON CONFLICT (keyword, market) DO UPDATE SET
        people_also_ask = CASE WHEN excluded.people_also_ask <> '[]'::jsonb THEN excluded.people_also_ask
                               ELSE acp_contract.search_demand.people_also_ask END,
        related_searches = excluded.related_searches,
        paa_retrieved_on = excluded.paa_retrieved_on,
        serp_location = excluded.serp_location
    WHERE acp_contract.search_demand.paa_retrieved_on IS NULL
       OR acp_contract.search_demand.paa_retrieved_on < excluded.paa_retrieved_on
"""

EXISTING_SQL = """
    SELECT keyword, market, retrieved_on, paa_retrieved_on
    FROM acp_contract.search_demand WHERE keyword = ANY($1::text[])
"""


@dataclass
class CopyPlan:
    volumes: dict[tuple[str, str], tuple[int, datetime]] = field(default_factory=dict)
    serps: dict[tuple[str, str], tuple[list[str], list[str], datetime]] = field(default_factory=dict)
    rows_read: int = 0


def _json_list(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else []
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def market_of(cache_key: str | None) -> str:
    """The market of the location code inside a cache_key (``seo:<seed>:<location_code>:<format>``), else US."""
    for token in re.findall(r"\d{4}", cache_key or ""):
        market = LOCATION_CODE_TO_MARKET.get(int(token))
        if market:
            return market
    return DEFAULT_MARKET


def plan_copy(rows) -> CopyPlan:
    """The newest volume and SERP per (keyword, market) across the seo_context rows (pure, unit-tested)."""
    from services.acp_contract.segment_research_batch import clean_keyword

    plan = CopyPlan()
    for r in rows:
        plan.rows_read += 1
        fetched = r["fetched_at"]
        market = market_of(r["cache_key"])
        for idea in _json_list(r["keyword_ideas"]):
            if not isinstance(idea, dict):
                continue
            kw = clean_keyword(str(idea.get("keyword") or ""))
            vol = idea.get("search_volume")
            if not kw or not isinstance(vol, int) or isinstance(vol, bool):
                continue
            old = plan.volumes.get((kw, market))
            if old is None or old[1] < fetched:
                plan.volumes[(kw, market)] = (vol, fetched)
        seed = clean_keyword(str(r["keyword_search"] or ""))
        paa = [str(q) for q in _json_list(r["people_also_ask"]) if q][:10]
        related = [str(q) for q in _json_list(r["related_keywords"]) if q][:10]
        if seed and (paa or related):
            old = plan.serps.get((seed, market))
            if old is None or old[2] < fetched:
                plan.serps[(seed, market)] = (paa, related, fetched)
    return plan


async def run_copy(pool, *, apply: bool = False) -> dict:
    """Dry run (default): counts of what would be written. `apply=True` writes, one transaction."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(READ_SQL, AA_TENANT)
        plan = plan_copy(rows)
        keys = sorted({k for k, _m in plan.volumes} | {k for k, _m in plan.serps})
        existing = {(r["keyword"], r["market"]): r for r in await conn.fetch(EXISTING_SQL, keys)} if keys else {}
        vol_new = sum(1 for k in plan.volumes if k not in existing)
        vol_newer = sum(1 for k, (_v, at) in plan.volumes.items()
                        if k in existing and existing[k]["retrieved_on"] < at)
        serp_new = sum(1 for k in plan.serps if k not in existing and k not in plan.volumes)
        serp_newer = sum(1 for k, (_p, _r, at) in plan.serps.items()
                         if (k in existing or k in plan.volumes)
                         and (k not in existing or existing[k]["paa_retrieved_on"] is None
                              or existing[k]["paa_retrieved_on"] < at))
        summary = {
            "apply": apply, "seo_context_rows": plan.rows_read,
            "volume_candidates": len(plan.volumes), "volume_rows_new": vol_new, "volume_rows_newer": vol_newer,
            "serp_candidates": len(plan.serps), "serp_rows_new": serp_new, "serp_rows_newer": serp_newer,
        }
        if not apply:
            return summary
        async with conn.transaction():
            await conn.executemany(UPSERT_VOLUME_SQL, [(kw, m, vol, at) for (kw, m), (vol, at)
                                                       in sorted(plan.volumes.items())])
            await conn.executemany(UPSERT_SERP_SQL, [(kw, m, json.dumps(paa), json.dumps(rel), at)
                                                     for (kw, m), (paa, rel, at) in sorted(plan.serps.items())])
        # AA-630 — the Segments that could land a copied PAA re-land it (questions_count NULL: the landing sweep
        # and the next recompute pick them up). Best effort, after the commit.
        invalidated = 0
        from services.acp_contract.atom_ranking import invalidate_questions_cache_for_keyword
        for kw in sorted({kw for (kw, _m), (paa, _r, _at) in plan.serps.items() if paa}):
            try:
                invalidated += await invalidate_questions_cache_for_keyword(conn, kw) or 0
            except Exception:  # noqa: BLE001 — a missed invalidation only delays the landing
                pass
        return {**summary, "written": True, "segments_invalidated": invalidated}
