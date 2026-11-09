"""AA-1050 W4 — the claim-instrument shadow on the replay corpus: recent stored T9 pieces and T2 tenant versions.

Run in the api container (services/ is in the image, scripts/ is not), with the container's DATABASE_URL (cis_app):

    python -m services.quality.claim_shadow_replay --surfaces t9_blog,t9_social,t2 --days 30 --limit 10
    python -m services.quality.claim_shadow_replay ... --live --confirm-spend --cap 0.10

DRY RUN is the default: it reads the objects (read-only), counts the cache hits and the calls the rest would make,
and prints the expected and worst-case cost; no model call, no row. ``--live`` (with ``--confirm-spend``) runs the
shadow on each object exactly as the live hooks do (cache first, the pinned instrument, the same spend guard over
the stage's ledger window, plus this run's own ``--cap`` <= the window cap) and writes one verdict row per object
(run_kind replay). It stops at the first object the guard refuses; the summary says so. The output is counts and
ids only (no text).

What it reads (never writes): ``acp_shared.content_piece`` (approved / held pieces with a stored writer input: the
seed and the Facts the F9 judge read, the CTA) and ``gold_aa_internal.tenant_tour_versions`` (AI-written versions,
not a policy hold), with each version's source rebuilt the way the T2 job builds it (``_PT_SQL`` +
``source_tour_dict``) and its brand pack (``_prepare``: CTA and brand name are the tenant's spans). T2 Facts entries
(``T2_FACTS``) are not added back: the replay source is the published tour text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Optional

from services.ground_truth import r1
from services.quality import claim_shadow as cs

T9_SQL = """
SELECT piece_id, tenant_id, job_id, channel, status, content_text, seo_title, meta_description, gate_ledger,
       writer_input
FROM acp_shared.content_piece
WHERE status IN ('approved', 'held') AND writer_input IS NOT NULL AND writer_input ? 'seed'
  AND created_at >= now() - make_interval(days => $1)
  AND (CASE WHEN channel = 'blog' THEN 't9_blog' ELSE 't9_social' END) = ANY($2::text[])
ORDER BY created_at DESC
LIMIT $3
"""
T2_SQL = """
SELECT id, tenant_id, job_id, status, rewritten_content, judge_breakdown, published_tour_id
FROM gold_aa_internal.tenant_tour_versions
WHERE edit_source = 'ai_generated' AND status IN ('ai_generated', 'needs_review')
  AND NOT (coalesce(judge_breakdown, '{}'::jsonb) ? 'policy_hold')
  AND created_at >= now() - make_interval(days => $1)
ORDER BY created_at DESC
LIMIT $2
"""


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


async def t9_objects(conn, surfaces: list[str], days: int, limit: int) -> list[cs.ShadowObject]:
    out = []
    for r in await conn.fetch(T9_SQL, days, surfaces, limit):
        wi = _json(r["writer_input"]) or {}
        source = "\n".join(t for t in (wi.get("seed"), wi.get("facts")) if t)
        out.append(cs.t9_object(
            piece_id=r["piece_id"], tenant_id=r["tenant_id"], job_id=r["job_id"], channel=r["channel"],
            content_text=r["content_text"], seo_title=r["seo_title"], meta_description=r["meta_description"],
            source_text=source, cta=wi.get("cta"), gate_ledger=_json(r["gate_ledger"]) or [], status=r["status"]))
    return out


async def t2_objects(pool, days: int, limit: int) -> list[cs.ShadowObject]:
    from services.jobs import t2_rewrite_job as t2

    out = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(T2_SQL, days, limit)
    for r in rows:
        async with pool.acquire() as conn:
            pt = await conn.fetchrow(t2._PT_SQL, r["published_tour_id"])
        if pt is None:
            continue
        tour_dict, brand_rules = await t2._prepare(pool, dict(pt), str(r["tenant_id"]), "EN-US")
        brand_rules.pop("_brand_loaded", None)
        out.append(cs.t2_object(
            version_id=r["id"], tenant_id=r["tenant_id"], job_id=r["job_id"],
            rewritten=_json(r["rewritten_content"]) or {}, status=r["status"],
            judge_breakdown=_json(r["judge_breakdown"]), tour_dict=tour_dict, brand_rules=brand_rules))
    return out


def plan_object(obj: cs.ShadowObject, inst: r1.Instrument, attempts: int = 1) -> dict:
    prompts = cs.user_prompts(obj, inst)
    calls = len(prompts) * cs.VOTES
    expected = sum(r1.call_cost(inst, u, r1.expected_out(inst)) for _f, _t, u in prompts) * cs.VOTES
    worst = sum(r1.worst_call_cost(inst, u, attempts) for _f, _t, u in prompts) * cs.VOTES
    return {"calls": calls, "expected_usd": expected, "worst_usd": worst, "key": cs.cache_key(inst, prompts)}


async def replay(pool, objects: list[cs.ShadowObject], *, run_id: str, cap: float, live: bool,
                 caller=None, attempts: Optional[int] = None) -> dict:
    """DRY RUN (live False): the plan. Live: one guarded shadow per object, cache first; stops at the first refusal
    of the guard. Counts and ids only."""
    inst = cs.instrument()
    window_cap = cs.cap_usd()
    if not 0 < cap <= window_cap:
        raise ValueError(f"--cap must be in (0, {window_cap}] USD (the window cap), got {cap}")
    out = {"run_id": run_id, "instrument": inst.id, "votes": cs.VOTES, "objects": len(objects), "dry_run": not live,
           "cap_usd": cap, "window_cap_usd": window_cap, "window_days": cs.window_days()}
    async with pool.acquire() as conn:
        out["window_spend_usd"] = round(await cs.ledger_spend(conn), 6)
        hits = 0
        plan = {"calls": 0, "expected_usd": 0.0, "worst_usd": 0.0}
        for o in objects:
            p = plan_object(o, inst)
            if await cs.cached_verdict(conn, p["key"]) is not None:
                hits += 1
                continue
            for k in plan:
                plan[k] += p[k]
    out.update({"cache_hits": hits, "planned_calls": plan["calls"],
                "expected_usd": round(plan["expected_usd"], 4), "worst_case_usd": round(plan["worst_usd"], 4)})
    if not live:
        return out
    guard = cs.SpendGuard(window_cap, run_cap=cap)
    results = []
    for o in objects:
        if not o.fields:
            continue
        s = await cs.shadow_object(pool, o, run_kind="replay", run_id=run_id, guard=guard, caller=caller,
                                   inst=inst, attempts=attempts)
        if s is None:
            continue
        results.append({k: s[k] for k in ("verdict_id", "surface", "verdict", "reason", "baseline", "agreement",
                                          "cached", "calls", "cost_usd")} | {"object_id": o.object_id})
        if s.get("stopped") == "budget":
            out["stopped"] = f"spend guard after {len(results)} objects"
            break
    out.update({"results": results, "spent_usd": round(guard.charged, 6), "calls": guard.calls,
                "agreement": {a: sum(1 for r in results if r["agreement"] == a)
                              for a in ("agree", "disagree", "unverified", "no_baseline")}})
    return out


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--surfaces", default=",".join(cs.SURFACES))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=10, help="objects per source (T9, T2)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm-spend", action="store_true")
    ap.add_argument("--cap", type=float, default=0.10, help="this run's own cap (USD, <= the window cap)")
    ap.add_argument("--run-id", default=None)
    return ap


async def _main(argv: list[str]) -> int:
    import asyncpg

    args = _parser().parse_args(argv)
    surfaces = [s for s in args.surfaces.split(",") if s in cs.SURFACES]
    if args.live and not args.confirm_spend:
        print("refused: --live needs --confirm-spend", file=sys.stderr)
        return 2
    if not 1 <= args.limit <= 200 or not 1 <= args.days <= 366:
        print("refused: --limit in 1..200, --days in 1..366", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3, ssl="require")
    try:
        objs: list[cs.ShadowObject] = []
        t9 = [s for s in surfaces if s != "t2"]
        if t9:
            async with pool.acquire() as conn:
                objs += await t9_objects(conn, t9, args.days, args.limit)
        if "t2" in surfaces:
            objs += await t2_objects(pool, args.days, args.limit)
        run_id = args.run_id or f"replay-{uuid.uuid4().hex[:12]}"
        result = await replay(pool, objs, run_id=run_id, cap=args.cap, live=args.live)
    finally:
        await pool.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
