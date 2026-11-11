#!/usr/bin/env python3
"""AA-1080 — $0 replay of the T2 keyword pick per tenant: the market T2 takes its SEO keywords from, before and after
AA-1080, and the keywords each rule picks for the tenant's rewritten tours.

  before  the lowest MARKET_RANK among the tenant's supported markets (US first; the rule up to AA-1080, copied below)
  after   the first supported market the agency declared (seed_builder.first_supported_market)

The keywords come from `s1_prefetch.market_keyword_seo`, the same cache-only pick T2 runs (acp_contract.search_demand,
no DataForSEO, no LLM, no Jev: $0). Read only: the session is READ ONLY (a write raises), nothing is enqueued or
recorded (no unmapped-market request).

Run in the CIS api container through the orchestrator's S3-mediated ECS exec helper, from the workspace root:
  apps/AA-CIS-App/scripts/aa1080/t2_keyword_market_replay.py TENANTS=<slug|uuid>,... [TOURS=<n per tenant, default 3>]
On an image built before AA-1080, `market_keyword_seo` is missing and the script stops with a clear message.
Output: one JSON object per tenant, then a SUMMARY line {"tenants", "market_changed"}.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

# The rule up to AA-1080 (seed_builder.resolve_buyer_market before), copied so the comparison is stable.
_RANK_BEFORE = {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}

TENANTS_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.slug
    FROM shared.tenants t
    WHERE t.slug = ANY($1::text[]) OR t.tenant_id::text = ANY($1::text[])
    ORDER BY t.slug
"""
TOURS_SQL = """
    SELECT DISTINCT pt.tour_id::text AS tour_id
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    WHERE ttv.tenant_id = $1::uuid
    ORDER BY 1
    LIMIT $2
"""


def market_before(target_market: dict) -> str:
    """The lowest-rank supported market (US when none): the T2 pick up to AA-1080."""
    present = [c for c in (target_market or {}).get("countries") or [] if c in _RANK_BEFORE]
    return min(present, key=lambda c: _RANK_BEFORE[c]) if present else "US"


def market_after(target_market: dict) -> str:
    from services.seo_intelligence.seed_builder import first_supported_market
    return first_supported_market(target_market) or "US"


def parse_args(argv: list[str]) -> dict:
    out = {"TENANTS": os.environ.get("TENANTS", ""), "TOURS": os.environ.get("TOURS", "3")}
    for a in argv:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k.strip().upper()] = v.strip()
    tenants = [t.strip() for t in out["TENANTS"].split(",") if t.strip()]
    if not tenants:
        raise SystemExit("TENANTS=<slug|uuid>,... is required")
    return {"tenants": tenants, "tours": max(1, min(int(out["TOURS"]), 20))}


async def replay_tenant(conn, tenant_id: str, slug: str, tours: int) -> dict:
    from services.seo_intelligence import s1_prefetch
    from shared.services.tenant_config_service import TenantConfigService

    if not hasattr(s1_prefetch, "market_keyword_seo"):
        raise SystemExit("this image predates AA-1080 (no s1_prefetch.market_keyword_seo): run it after the deploy")
    cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
    tm = cfg.target_market or {}
    before, after = market_before(tm), market_after(tm)
    out = {"tenant": slug, "declared": tm.get("declared") or [], "countries": tm.get("countries") or [],
           "market_before": before, "market_after": after, "changed": before != after, "tours": []}
    for r in await conn.fetch(TOURS_SQL, tenant_id, tours):
        row = {"tour_id": r["tour_id"]}
        for label, market in (("before", before), ("after", after)):
            seo = await s1_prefetch.market_keyword_seo(conn, market, r["tour_id"])
            row[label] = {"market": market, "keywords": (seo or {}).get("top_keywords", [])[:5]}
        out["tours"].append(row)
    return out


async def main(argv: list[str]) -> int:
    import asyncpg

    from shared.secrets import get_database_url

    args = parse_args(argv)
    conn = await asyncpg.connect(get_database_url().strip(), ssl="require")
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        rows = [dict(r) for r in await conn.fetch(TENANTS_SQL, args["tenants"])]
        found = {r["slug"] for r in rows} | {r["tenant_id"] for r in rows}
        missing = [t for t in args["tenants"] if t not in found]
        if missing:
            print(json.dumps({"not_found": missing}))
        changed = 0
        for r in rows:
            res = await replay_tenant(conn, r["tenant_id"], r["slug"], args["tours"])
            changed += res["changed"]
            print(json.dumps(res, default=str))
        print("SUMMARY " + json.dumps({"tenants": len(rows), "market_changed": changed, "cost_usd": 0}))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
