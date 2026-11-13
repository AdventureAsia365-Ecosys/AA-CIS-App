#!/usr/bin/env python3
"""AA-1055 PR 1 — read-only before / after comparison of the Slate Bar per tenant.

For each tenant it builds the Slate's Segment candidates twice from the same live rows and prints, per Channel with a
Bar, how many clear it and how many miss it per reason (no_data / below_bar / few_questions / few_said):

  before  demand / questions of the Segment's best-RANKED market (the AA-741 S271 rule, copied below)
  after   the highest demand over the tenant's markets and the highest questions count (AA-1055 PR 1)

plus the Subjects the tenant holds today per Channel and state. Both rules live in this file, so the output is the same
on an image with or without AA-1055 (run it once after the deploy; a run before it prints the same comparison).

Read only: the session is READ ONLY (a write raises), no job is enqueued, no DataForSEO / LLM / Jev call. The tenant's
markets are resolved without `_tenant_market_codes` (that records unmapped-market requests). Debate is NOT applied
(its brand-fit cut is a paid call on a cache miss), so the counts are before Debate, the place rule and held places:
"how many candidates clear the Bar", not "how many Subjects get proposed".

Run in the CIS api container (the orchestrator, from the workspace root):
  tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1055/slate_bar_compare.py [TENANTS=<slug|uuid>,...] [CHANNELS=blog]
TENANTS empty = every active tenant that has rewritten a tour. CHANNELS = comma list (default: every Channel with a Bar).
Output: one JSON object per tenant, then a TOTAL line.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import UUID

sys.path.insert(0, os.getcwd())

REASONS = ("cleared", "no_data", "below_bar", "few_questions", "few_said")

# The AA-741 S271 rule (main before AA-1055): demand / questions of the best-ranked market, travelling together.
BEFORE_SQL = """
    WITH per_market AS (
        SELECT ar.segment_id, ar.market,
               MIN(ar.total_rank)    AS total_rank,
               MAX(ar.demand_volume) AS demand_volume,
               MAX(ar.questions)     AS questions,
               MIN(ar.said)          AS said
        FROM acp_contract.atom_ranking ar
        WHERE ar.tour_id = ANY($1::uuid[]) AND ar.market = ANY($2::text[])
          AND ar.excluded_reason IS NULL AND ar.superseded_at IS NULL
        GROUP BY ar.segment_id, ar.market
    ), best AS (
        SELECT DISTINCT ON (segment_id) segment_id, demand_volume, questions
        FROM per_market
        ORDER BY segment_id, total_rank ASC NULLS LAST, market
    )
    SELECT pm.segment_id, MIN(pm.total_rank) AS total_rank, MAX(b.demand_volume) AS demand_volume,
           MAX(b.questions) AS questions, MIN(pm.said) AS said,
           MAX(asg.canonical_place) AS canonical_place, MAX(asg.canonical_action) AS canonical_action
    FROM per_market pm
    JOIN best b ON b.segment_id = pm.segment_id
    JOIN acp_contract.atom_segment asg ON asg.segment_id = pm.segment_id
    GROUP BY pm.segment_id
"""

# The AA-1055 PR 1 rule (services/acp_shared/slate.py `_SEGMENT_CANDIDATES_SQL`, copied so an older image runs it too).
AFTER_SQL = """
    WITH per_market AS (
        SELECT ar.segment_id, ar.market,
               MIN(ar.total_rank)    AS total_rank,
               MAX(ar.demand_volume) AS demand_volume,
               MAX(ar.questions)     AS questions,
               MIN(ar.said)          AS said
        FROM acp_contract.atom_ranking ar
        WHERE ar.tour_id = ANY($1::uuid[]) AND ar.market = ANY($2::text[])
          AND ar.excluded_reason IS NULL AND ar.superseded_at IS NULL
        GROUP BY ar.segment_id, ar.market
    )
    SELECT pm.segment_id, MIN(pm.total_rank) AS total_rank, MAX(pm.demand_volume) AS demand_volume,
           MAX(pm.questions) AS questions, MIN(pm.said) AS said,
           MAX(asg.canonical_place) AS canonical_place, MAX(asg.canonical_action) AS canonical_action
    FROM per_market pm
    JOIN acp_contract.atom_segment asg ON asg.segment_id = pm.segment_id
    GROUP BY pm.segment_id
"""

TENANTS_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.slug
    FROM shared.tenants t
    WHERE t.is_active AND EXISTS (SELECT 1 FROM gold_aa_internal.tenant_tour_versions v WHERE v.tenant_id = t.tenant_id)
    ORDER BY t.slug
"""

HELD_SQL = """
    SELECT channel, state, count(*)::int AS n FROM acp_shared.subject
    WHERE tenant_id = $1::uuid GROUP BY channel, state ORDER BY channel, state
"""


def bar_status(spec: dict, demand, questions: int, said: int) -> str:
    """The first reason a candidate misses one Channel's Bar, or "cleared" (same tests and order as
    `services.acp_shared.slate.bar_status`; tests/unit/test_aa1055_slate_bar_reasons.py keeps the two equal)."""
    if (demand or 0) < spec["needs_demand"]:
        return "no_data" if demand is None else "below_bar"
    if (questions or 0) < spec["needs_questions"]:
        return "few_questions"
    if (said or 0) < spec["needs_said"]:
        return "few_said"
    return "cleared"


def count_statuses(rows, channels: list[str], bars: dict) -> dict[str, dict[str, int]]:
    """{channel: {reason: n}} over candidate rows (demand_volume / questions / said)."""
    out = {}
    for channel in channels:
        counts = dict.fromkeys(REASONS, 0)
        for r in rows:
            counts[bar_status(bars[channel], r["demand_volume"], r["questions"], r["said"])] += 1
        out[channel] = counts
    return out


def bar_channels(bars: dict, wanted: str) -> list[str]:
    names = [c for c, spec in bars.items() if not spec["on_demand"] and spec["grain"] == "segment"]
    picked = [c.strip() for c in wanted.split(",") if c.strip()] if wanted else names
    unknown = [c for c in picked if c not in names]
    if unknown:
        raise SystemExit(f"CHANNELS: unknown or Bar-less Channel(s) {unknown}; choose from {names}")
    return picked


async def tenant_markets(conn, tenant_id: str) -> list[str]:
    """The tenant's market codes as `atom_ranking.market` stores them (read only; see the module docstring)."""
    from services.seo_intelligence.seed_builder import LOCATION_CODE_TO_MARKET, resolve_buyer_markets
    from shared.services.tenant_config_service import TenantConfigService

    cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
    return [LOCATION_CODE_TO_MARKET[loc] for loc, _name, _lang in resolve_buyer_markets(cfg.target_market)]


async def compare_tenant(conn, tenant_id: str, slug: str, channels: list[str]) -> dict:
    from services.acp_shared import slate

    tour_ids = await slate._tenant_tour_ids(UUID(tenant_id), conn)
    markets = await tenant_markets(conn, tenant_id)
    out = {"tenant": slug, "markets": markets, "tours": len(tour_ids)}
    if not tour_ids:
        return {**out, "candidates": 0}
    result = {}
    for label, sql in (("before", BEFORE_SQL), ("after", AFTER_SQL)):
        rows = await conn.fetch(sql, tour_ids, markets)
        rows = await slate._segments_with_seed(UUID(tenant_id), conn, rows, {str(t) for t in tour_ids})
        result[label] = count_statuses(rows, channels, slate.bars())
        out["candidates"] = len(rows)
    out["bar"] = result
    out["held_subjects"] = {f"{r['channel']}/{r['state']}": r["n"] for r in await conn.fetch(HELD_SQL, tenant_id)}
    return out


async def main() -> int:
    import asyncpg

    from services.acp_shared import slate
    from shared.secrets import get_database_url

    channels = bar_channels(slate.CHANNEL_BARS, os.environ.get("CHANNELS", ""))
    wanted = [t.strip() for t in os.environ.get("TENANTS", "").split(",") if t.strip()]
    conn = await asyncpg.connect(get_database_url().strip(), ssl="require")
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        tenants = [dict(r) for r in await conn.fetch(TENANTS_SQL)]
        if wanted:
            tenants = [t for t in tenants if t["slug"] in wanted or t["tenant_id"] in wanted]
            missing = sorted(set(wanted) - {t["slug"] for t in tenants} - {t["tenant_id"] for t in tenants})
            if missing:
                print(json.dumps({"not_found_or_no_rewrites": missing}))
        total = {label: {c: dict.fromkeys(REASONS, 0) for c in channels} for label in ("before", "after")}
        for t in tenants:
            row = await compare_tenant(conn, t["tenant_id"], t["slug"], channels)
            print(json.dumps(row, default=str))
            for label, per_channel in (row.get("bar") or {}).items():
                for c, counts in per_channel.items():
                    for reason, n in counts.items():
                        total[label][c][reason] += n
        print("TOTAL " + json.dumps({"tenants": len(tenants), "debate_applied": False, **total}))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
