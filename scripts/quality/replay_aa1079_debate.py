"""AA-1079 (S277) — $0 replay of the T7 slate Debate: what the Slate looks like with the AA-631 cut and with rank only.

No LLM, no Jev, no write. Per tenant it rebuilds today's candidate pools with the PRODUCTION slate code
(``slate._fetch_segment_candidates`` / ``_fetch_route_candidates`` / ``_fetch_segment_hub_map``, the tenant's markets
and enabled Channels, held places and thin walks), judges every candidate from what Debate STORED
(``acp_shared.atom_segment.contested`` and ``acp_shared.debate_brand_fit_cache``: the phase 2 scores, newest brand
version per candidate; ``debate.assess_candidates(cache_only=True, any_brand_version=True)``), then runs
``slate._choose_slate`` twice: the cut (``debate.apply_signals(..., "cut")``, the behaviour before AA-1079) and rank
only (``"rank"``, the new default). A cache miss is reported (``cache_miss``) and counts as not flagged, in both arms.

    python -m scripts.quality.replay_aa1079_debate --tenant <uuid> [--tenant <uuid> ...] [--out replay.json]
    python -m scripts.quality.replay_aa1079_debate --all-cached [--out replay.json]   # every tenant with cache rows

The DB login is ``DATABASE_URL`` (cis_app in the api container: it reads every table used here), ssl required unless
``REPLAY_DB_SSL=disable`` (local tests); every session is ``default_transaction_read_only``. Output: one JSON document
(ids and counts only, no text) on stdout between REPLAY-BEGIN / REPLAY-END, and at ``--out`` when given.

Known limit: the candidate pool is TODAY's (the tenants' tours, Bar data and Routes as they are now), judged with the
STORED Debate scores; a candidate Debate never judged is a cache miss. The phase 2 slates themselves (which Subjects
were proposed) are the ``acp_shared.subject`` rows, counted per Channel and state next to each arm as ``stored``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Optional
from uuid import UUID

SCHEMA = "aa1079-debate-replay/1"

_CACHED_TENANTS_SQL = "SELECT DISTINCT tenant_id FROM acp_shared.debate_brand_fit_cache ORDER BY tenant_id"
_TENANT_SQL = "SELECT slug FROM shared.tenants WHERE tenant_id = $1::uuid"
_STORED_SQL = """
    SELECT channel, state, count(*) AS n FROM acp_shared.subject
    WHERE tenant_id = $1::uuid GROUP BY channel, state
"""


def _key(c) -> str:
    return c.segment_id or c.route_id or f"{c.place}|{c.action}"


def pool_summary(candidates: list, signals: list) -> dict:
    """Counts of one candidate pool's Debate signals (pure)."""
    from services.acp_shared import debate

    by_flag = Counter(f for s in signals for f in s.flags)
    return {"candidates": len(candidates), "flagged": sum(1 for s in signals if s.flags),
            "flagged_by": dict(sorted(by_flag.items())), "would_cut": debate.would_cut(signals),
            "brand_fit_source": dict(sorted(Counter(s.brand_fit_source or "none" for s in signals).items()))}


def compare_arms(channels: list[str], segments: list, routes: list, seg_signals: list, route_signals: list,
                 hub_of: dict, held: Optional[dict] = None, thin: Optional[dict] = None,
                 stored: Optional[dict] = None) -> dict:
    """The two arms over one tenant's pools (pure): per Channel the proposals with the cut and rank only, the ids each
    arm proposes that the other does not, and how many rank-only proposals carry a flag."""
    from services.acp_shared import debate, slate

    arms = {}
    for mode in (debate.DEBATE_MODE_CUT, debate.DEBATE_MODE_RANK):
        segs = debate.apply_signals(segments, seg_signals, mode)
        rts = debate.apply_signals(routes, route_signals, mode)
        arms[mode] = slate._choose_slate(channels, segs, rts, hub_of, held=held, thin=thin)
    per_channel = {}
    for channel in channels:
        cut_ids = [_key(c) for c, _ in arms["cut"][channel]]
        rank = arms["rank"][channel]
        rank_ids = [_key(c) for c, _ in rank]
        per_channel[channel] = {
            "proposed_cut": len(cut_ids), "proposed_rank": len(rank_ids),
            "rank_flagged": sum(1 for c, _ in rank if c.debate_flags),
            "only_rank": sorted(set(rank_ids) - set(cut_ids)), "only_cut": sorted(set(cut_ids) - set(rank_ids)),
            "rank_order": rank_ids,
            "stored": (stored or {}).get(channel, {}),
        }
    return {"segments": pool_summary(segments, seg_signals), "routes": pool_summary(routes, route_signals),
            "channels": per_channel,
            "totals": {"proposed_cut": sum(v["proposed_cut"] for v in per_channel.values()),
                       "proposed_rank": sum(v["proposed_rank"] for v in per_channel.values()),
                       "dropped_by_cut": (pool_summary(segments, seg_signals)["would_cut"]
                                          + pool_summary(routes, route_signals)["would_cut"]),
                       "dropped_by_rank": 0}}


async def replay_tenant(pool, tenant_id: UUID) -> dict:
    from services.acp_shared import debate, slate

    async with pool.acquire() as conn:
        markets, unmatched = await slate._tenant_market_codes(tenant_id, conn)
        channels = await slate._tenant_enabled_channels(tenant_id, conn)
        slug = await conn.fetchval(_TENANT_SQL, str(tenant_id))
        stored: dict = {}
        for r in await conn.fetch(_STORED_SQL, str(tenant_id)):
            stored.setdefault(r["channel"], {})[r["state"]] = int(r["n"])
    grains = {slate.CHANNEL_BARS[c]["grain"] for c in channels}
    segments = await slate._fetch_segment_candidates(tenant_id, pool, markets) if "segment" in grains else []
    routes = await slate._fetch_route_candidates(tenant_id, pool, markets) if "route" in grains else []
    hub_of = await slate._fetch_segment_hub_map(tenant_id, pool) if "segment" in grains else {}
    async with pool.acquire() as conn:
        seg_signals = await debate.assess_candidates(tenant_id, segments, conn, cache_only=True,
                                                     any_brand_version=True)
        route_signals = await debate.assess_candidates(tenant_id, routes, conn, cache_only=True,
                                                       any_brand_version=True)
        held = await slate._fetch_held_subjects(
            tenant_id, conn, [c for c in channels if slate.CHANNEL_BARS[c]["one_piece_per_place"]])
        thin = await slate._fetch_thin_walk_segments(tenant_id, conn, segments, channels)
    out = compare_arms(channels, segments, routes, seg_signals, route_signals, hub_of, held=held, thin=thin,
                       stored=stored)
    return {"tenant_id": str(tenant_id), "tenant_slug": slug, "markets": markets, "unmatched_markets": unmatched,
            "enabled_channels": channels, **out}


async def _pool():
    import asyncpg

    ssl = None if os.environ.get("REPLAY_DB_SSL") == "disable" else "require"
    return await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl=ssl, min_size=1, max_size=2,
                                     server_settings={"default_transaction_read_only": "on"})


async def run(args) -> dict:
    pool = await _pool()
    try:
        tenants = [UUID(t) for t in args.tenant]
        if args.all_cached:
            async with pool.acquire() as conn:
                tenants += [r["tenant_id"] for r in await conn.fetch(_CACHED_TENANTS_SQL)]
        tenants = list(dict.fromkeys(tenants))
        rows = []
        for t in tenants:
            try:
                rows.append(await replay_tenant(pool, t))
            except Exception as exc:          # one tenant's failure is a row, not a stop
                rows.append({"tenant_id": str(t), "error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        await pool.close()
    ok = [r for r in rows if "error" not in r]
    return {"schema": SCHEMA, "cost_usd": 0, "tenants": rows,
            "summary": {"tenants": len(rows), "errors": len(rows) - len(ok),
                        "candidates": sum(r["segments"]["candidates"] + r["routes"]["candidates"] for r in ok),
                        "flagged": sum(r["segments"]["flagged"] + r["routes"]["flagged"] for r in ok),
                        "dropped_by_cut": sum(r["totals"]["dropped_by_cut"] for r in ok),
                        "dropped_by_rank": 0,
                        "proposed_cut": sum(r["totals"]["proposed_cut"] for r in ok),
                        "proposed_rank": sum(r["totals"]["proposed_rank"] for r in ok)}}


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tenant", action="append", default=[], help="tenant uuid (repeatable)")
    p.add_argument("--all-cached", action="store_true", help="every tenant with debate_brand_fit_cache rows")
    p.add_argument("--out", help="also write the JSON here")
    args = p.parse_args(argv)
    if not args.tenant and not args.all_cached:
        p.error("name at least one --tenant, or --all-cached")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print("REPLAY-BEGIN")
    print(text)
    print("REPLAY-END")
    return 1 if result["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
