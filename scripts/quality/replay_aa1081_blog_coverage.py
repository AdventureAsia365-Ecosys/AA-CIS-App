"""AA-1081 (S277) — $0 replay of the T7 Blog topic coverage: per adopted tour, its Blog topics and no-topic reason.

No LLM, no Jev, no write. Per tenant it rebuilds today's candidate pools with the PRODUCTION slate code
(``slate._fetch_segment_candidates`` with the unseeded rows, the tenant's markets and enabled Channels, held places,
thin walks, ``_coverage_inputs``), judges Debate from what it STORED (``debate.assess_candidates(cache_only=True,
any_brand_version=True)``, as the AA-1079 replay), and runs ``slate._choose_slate`` in three arms:

- ``before_cut``  — Debate cut, no cover pass (the AA-741 phase 2 behaviour);
- ``before_rank`` — Debate rank only, no cover pass (main before AA-1081);
- ``after``       — Debate rank only + the cover pass (this change);
- ``after_min_seed`` (only with ``--min-seed N``) — ``after`` plus the Blog minimum seed of N words
  (``slate._fetch_short_blog_seeds``; DB reads only).

Per arm and tour: topics and the typed reason (``slate.blog_tour_coverage``). Per tour with ``low_search_interest``
in the after arm, the best demand / questions of its moments (the demand-join check of the audit, caf4bf16). Per
tenant: the candidates read and the seconds the reads took (the slate's read cost before an incremental slate).

    python -m scripts.quality.replay_aa1081_blog_coverage --slug-prefix audit- [--min-seed 600] [--out r.json]
    python -m scripts.quality.replay_aa1081_blog_coverage --tenant <uuid> [--tenant <uuid> ...]
    python -m scripts.quality.replay_aa1081_blog_coverage --all

``scripts/`` is not in the api image (the Dockerfile copies api/ shared/ services/ worker/ only). On Dev, run it with
the S3-mediated ECS exec: fetch the presigned file to /tmp, then ``cd /app && PYTHONPATH=/app python3 /tmp/replay.py
--slug-prefix audit-`` (it imports ``services.*`` only).

The DB login is ``DATABASE_URL`` (cis_app in the api container), ssl required unless ``REPLAY_DB_SSL=disable``; every
session is ``default_transaction_read_only``. Output: one JSON document (ids, catalog tour names, counts; no text) on
stdout between REPLAY-BEGIN / REPLAY-END, and at ``--out`` when given.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from typing import Optional
from uuid import UUID

SCHEMA = "aa1081-blog-coverage-replay/1"
CHANNEL = "blog"

_TENANTS_SQL = """
    SELECT t.tenant_id, t.slug FROM shared.tenants t
    WHERE t.is_active AND EXISTS (SELECT 1 FROM gold_aa_internal.tenant_tour_versions v WHERE v.tenant_id = t.tenant_id)
      AND ($1::text IS NULL OR t.slug LIKE $1 || '%')
    ORDER BY t.slug
"""
_TENANT_SQL = "SELECT slug FROM shared.tenants WHERE tenant_id = $1::uuid"


def arm_rows(tours: dict, segments: list, unseeded: list, dropped: list, chosen: dict, held, skip: frozenset,
             held_topics: dict) -> list[dict]:
    """One arm's per-tour rows (pure): `slate.blog_tour_coverage` on the Blog seats of `chosen`."""
    from services.acp_shared import slate

    return slate.blog_tour_coverage(CHANNEL, tours, segments, unseeded, dropped, chosen.get(CHANNEL, []), held,
                                    skip, held_topics)


def demand_check(tour_id: str, segments: list, unseeded: list, dropped: list) -> dict:
    """The best demand / questions over a tour's moments (pure): why no moment clears the Blog Bar."""
    rows = [c for c in segments + unseeded + dropped if tour_id in c.tour_ids]
    return {"moments": len(rows),
            "best_demand": max((c.demand or 0 for c in rows), default=0),
            "best_questions": max((c.questions for c in rows), default=0),
            "no_demand_data": sum(1 for c in rows if c.demand is None)}


def summarize(tenants: list[dict], arms: list[str]) -> dict:
    """Pairs (tenant x adopted tour) per arm: with a topic, without, the reasons, and the pairs that have a free
    candidate but no topic (`free_no_topic`: what the cover pass is for). Pure."""
    out: dict = {"pairs": 0}
    for arm in arms:
        rows = [t for r in tenants if "error" not in r for t in r["arms"].get(arm, [])]
        out["pairs"] = max(out["pairs"], len(rows))
        out[arm] = {"with_topic": sum(1 for t in rows if t["topics"]),
                    "no_topic": sum(1 for t in rows if not t["topics"]),
                    "reasons": dict(sorted(Counter(t["reason"] for t in rows if not t["topics"]).items())),
                    "free_no_topic": sum(1 for t in rows if not t["topics"] and (t.get("detail") or {}).get("free")),
                    "topics": sum(t["topics"] for t in rows)}
    return out


async def replay_tenant(pool, tenant_id: UUID, min_seed: int = 0) -> dict:
    from services.acp_shared import blog_seed, debate, slate

    async with pool.acquire() as conn:
        markets, unmatched = await slate._tenant_market_codes(tenant_id, conn)
        channels = await slate._tenant_enabled_channels(tenant_id, conn)
        slug = await conn.fetchval(_TENANT_SQL, str(tenant_id))
    if CHANNEL not in channels:
        return {"tenant_id": str(tenant_id), "tenant_slug": slug, "blog_enabled": False, "arms": {}}
    started = time.monotonic()
    unseeded: list = []
    segments = await slate._fetch_segment_candidates(tenant_id, pool, markets, unseeded_out=unseeded)
    hub_of = await slate._fetch_segment_hub_map(tenant_id, pool)
    read_seconds = round(time.monotonic() - started, 2)
    async with pool.acquire() as conn:
        signals = await debate.assess_candidates(tenant_id, segments, conn, cache_only=True, any_brand_version=True)
        held = await slate._fetch_held_subjects(
            tenant_id, conn, [c for c in channels if slate.CHANNEL_BARS[c]["one_piece_per_place"]])
        thin = await slate._fetch_thin_walk_segments(tenant_id, conn, segments, channels)
        tours, held_topics_all = await slate._coverage_inputs(tenant_id, conn, channels)
        short: dict = {}
        if min_seed > 0:
            # S279: the Blog minimum seed is a setting (no env override): evaluate min_seed through the floor argument
            short = await slate._fetch_short_blog_seeds(tenant_id, conn, segments, channels, held, thin,
                                                        floor=blog_seed.seed_floor(flat=min_seed))
    held_topics = held_topics_all[CHANNEL]
    cover = {CHANNEL: frozenset(t for t in tours if not held_topics.get(t))}
    thin_blog = (thin or {}).get(CHANNEL, frozenset())

    arms: dict[str, list[dict]] = {}
    specs = [("before_cut", debate.DEBATE_MODE_CUT, False, frozenset()),
             ("before_rank", debate.DEBATE_MODE_RANK, False, frozenset()),
             ("after", debate.DEBATE_MODE_RANK, True, frozenset())]
    if min_seed > 0:
        specs.append(("after_min_seed", debate.DEBATE_MODE_RANK, True, short.get(CHANNEL, frozenset())))
    for arm, mode, use_cover, extra in specs:
        segs = debate.apply_signals(segments, signals, mode)
        kept = {c.segment_id for c in segs}
        dropped = [c for c in segments if c.segment_id not in kept]
        skip = frozenset(thin_blog | extra)
        chosen = slate._choose_slate(channels, segs, [], hub_of, held=held, thin={**(thin or {}), CHANNEL: skip},
                                     cover=cover if use_cover else None)
        arms[arm] = arm_rows(tours, segs, unseeded, dropped, chosen, held.get(CHANNEL), skip, held_topics)
    low = [t["tour_id"] for t in arms["after"] if t["reason"] == slate.NO_TOPIC_LOW_INTEREST]
    return {"tenant_id": str(tenant_id), "tenant_slug": slug, "blog_enabled": True, "markets": markets,
            "unmatched_markets": unmatched, "tours": len(tours), "candidates_read": len(segments) + len(unseeded),
            "read_seconds": read_seconds, "short_seed": len(short.get(CHANNEL, ())), "min_seed_words": min_seed,
            "arms": arms, "demand_check": {t: demand_check(t, segments, unseeded, []) for t in low}}


async def _pool():
    import asyncpg

    ssl = None if os.environ.get("REPLAY_DB_SSL") == "disable" else "require"
    return await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl=ssl, min_size=1, max_size=2,
                                     server_settings={"default_transaction_read_only": "on"})


async def run(args) -> dict:
    pool = await _pool()
    try:
        tenants = [UUID(t) for t in args.tenant]
        if args.all or args.slug_prefix:
            async with pool.acquire() as conn:
                tenants += [r["tenant_id"] for r in await conn.fetch(_TENANTS_SQL, args.slug_prefix)]
        tenants = list(dict.fromkeys(tenants))
        rows = []
        for t in tenants:
            try:
                rows.append(await replay_tenant(pool, t, args.min_seed))
            except Exception as exc:          # one tenant's failure is a row, not a stop
                rows.append({"tenant_id": str(t), "error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        await pool.close()
    arms = ["before_cut", "before_rank", "after"] + (["after_min_seed"] if args.min_seed > 0 else [])
    return {"schema": SCHEMA, "cost_usd": 0, "tenants": rows,
            "summary": {"tenants": len(rows), "errors": sum(1 for r in rows if "error" in r), **summarize(rows, arms)}}


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tenant", action="append", default=[], help="tenant uuid (repeatable)")
    p.add_argument("--slug-prefix", help="every active tenant with adopted tours whose slug starts with this")
    p.add_argument("--all", action="store_true", help="every active tenant with adopted tours")
    p.add_argument("--min-seed", type=int, default=0, help="also run the after arm with this Blog minimum seed")
    p.add_argument("--out", help="also write the JSON here")
    args = p.parse_args(argv)
    if not args.tenant and not args.all and not args.slug_prefix:
        p.error("name at least one --tenant, or --slug-prefix, or --all")
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
