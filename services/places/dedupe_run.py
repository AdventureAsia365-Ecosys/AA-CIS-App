"""AA-847 Problem 2 — one run of the automatic duplicate-place merge (the body of job kind place_dedupe), with the
model calls passed in (tests use stubs; no paid call in CI).

Per country, every run (no incremental watermark: shared.destinations has no updated_at; ~1.5k rows, milliseconds):
  1. rules: dedupe.plan_country over the usable rows, the rows already merged taking part in the clash check;
  2. embedding promotion: the max cosine of the two destinations' geo.tour_place embeddings (free) for the soft keeps
     within 1 km;
  3. model band (paid, countries on the allow-list only): the pairs with no cached verdict for their input hash and
     no pin, highest score first, ≤ MAX_PAIRS_PER_RUN per run and the bedrock/job:place_dedupe budget; each verdict
     is cached in geo.place_pair_verdict;
  4. re-check of the job's existing merges (rules only): undone only on a country / kind-class change or a move past
     2 × the radius, with a `recheck` pin so the pair does not flap; admin merges are never undone;
  5. the diff (merges + undos) and its hash. Writes only for a country in mode `apply` whose approval is for this rule
     version (else it drops back to dry_run): changes outside the approved diff above max(5, 1 %) → nothing applied,
     `held_volume`, an alert; otherwise merges in batches of 50 per transaction, each batch enqueueing the graph build
     in the same transaction (catalog._live_stop hides stops on a merged id until the next build).
After all countries (mode apply): the dangling-pointer repoint and the root assertion.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable, Optional

import structlog

from services.places import dedupe as dd
from services.places import merges

logger = structlog.get_logger()

BATCH = 50
LIST_CAP = 300          # list lengths kept in the job result
VACUUM_MIN_ROWS = 50    # repointed geo.tour_place rows after which the run asks for VACUUM (ANALYZE)

Gen = Callable[[str, str], Awaitable[str]]


class Models:
    """The two-model check: primary / check are async (system, user) -> text; `names` go into the input hash;
    `stopped` is set when the budget stopped the band."""

    def __init__(self, primary: Optional[Gen], check: Optional[Gen], names: list[str]):
        self.primary, self.check, self.names = primary, check, names
        self.stopped: Optional[str] = None
        self.calls = 0
        self.cost_usd = 0.0     # the job's model wrappers add each call's cost here

    @property
    def enabled(self) -> bool:
        return self.primary is not None and self.check is not None and self.stopped is None


def _country_groups(rows: list[dd.Row]) -> dict[str, list[dd.Row]]:
    out: dict[str, list[dd.Row]] = {}
    for r in rows:
        out.setdefault(merges.country_key(r.country), []).append(r)
    return out


async def _active_merges(conn, country: str) -> list[dict]:
    rows = await conn.fetch(
        """SELECT id::text AS id, merged_id::text AS merged_id, keeper_id::text AS keeper_id, decided_by
             FROM geo.place_merge WHERE undone_at IS NULL AND lower(btrim(country)) = $1""", country)
    return [dict(r) for r in rows]


async def _alert_held(pool, country: str, outside: int, limit: int, job_id: Optional[str]) -> None:
    """Admin bell + operator e-mail (shared.alert_recipient) for a held country. Best-effort."""
    try:
        from shared import ops_alerts
        message = (f"place_dedupe held {country}: {outside} changes outside the approved dry-run diff "
                   f"(limit {limit}). Nothing was applied; review the dry run on /admin/places (Merges).")
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id,
                                                     payload, target_roles)
                   VALUES ('00000000-0000-0000-0000-000000000001'::uuid, 'system', 'place_dedupe_held', 'places', $1,
                           $2::jsonb, ARRAY['admin'])""",
                country, json.dumps({"message": message, "outside": outside, "limit": limit, "job_id": job_id}))
            await ops_alerts.enqueue_place_dedupe_held_email(conn, country=country, outside=outside, limit=limit,
                                                             job_id=job_id, message=message)
    except Exception as e:  # noqa: BLE001 — the job result and the log still say held_volume
        logger.warning("place_dedupe_alert_failed", country=country, error=str(e)[:200])


async def _model_band(pool, rows_by_id: dict, plan: dd.Plan, verdicts: dict, cache: dict, pins: dict,
                      models: Models, budget_left: list, job_id: Optional[str], out: dict) -> None:
    """Fill `verdicts` for the llm pairs of a plan: a pin, a cached verdict of the same input hash, or (when the
    models are enabled and the per-run cap allows) a new two-model verdict, saved."""
    need = []
    for (lo, hi), dec in sorted(plan.decisions.items(), key=lambda kv: (-kv[1].score, kv[0])):
        if dec.decision != dd.LLM or (lo, hi) in verdicts:
            continue
        if lo not in rows_by_id or hi not in rows_by_id:
            continue            # a clash-check pair with an already merged row: only a pin matters there
        a, b = rows_by_id[lo], rows_by_id[hi]
        h = dd.input_hash(dd.pair_input(a, b, dec), models.names)
        v = cache.get((lo, hi, h))
        if v is not None:
            verdicts[(lo, hi)] = v
        else:
            need.append((lo, hi, dec, h))
    out["band_pairs"] = out.get("band_pairs", 0) + sum(
        1 for (lo, hi), d in plan.decisions.items() if d.decision == dd.LLM and lo in rows_by_id and hi in rows_by_id)
    out["band_cached"] = out.get("band_cached", 0) + sum(
        1 for (lo, hi), d in plan.decisions.items() if d.decision == dd.LLM and (lo, hi) in verdicts)
    if not need or not models.enabled:
        out["band_unasked"] = out.get("band_unasked", 0) + len(need)
        return
    async with pool.acquire() as conn:
        texts = await merges.excerpts(conn, {i for lo, hi, _, _ in need for i in (lo, hi)})
    asked = 0
    for lo, hi, dec, h in need:
        if budget_left[0] <= 0 or not models.enabled:
            break
        a, b = rows_by_id[lo], rows_by_id[hi]
        inp = dd.pair_input(a, b, dec, texts)
        spent_before = models.cost_usd
        try:
            res = await dd.adjudicate(inp, models.primary, models.check)
        except Exception as e:  # noqa: BLE001 — BudgetExceeded or a provider error: stop the band, keep the rules
            from shared.cost_guard import BudgetExceeded
            models.stopped = "budget" if isinstance(e, BudgetExceeded) else f"error:{type(e).__name__}"
            logger.warning("place_dedupe_model_band_stopped", reason=models.stopped, error=str(e)[:200])
            break
        budget_left[0] -= 1
        asked += 1
        verdicts[(lo, hi)] = res["verdict"]
        async with pool.acquire() as conn:
            await merges.save_verdict(conn, lo, hi, h, res, models=models.names,
                                      cost_usd=models.cost_usd - spent_before, job_id=job_id,
                                      created_by="place_dedupe")
    out["band_asked"] = out.get("band_asked", 0) + asked
    out["band_unasked"] = out.get("band_unasked", 0) + len(need) - asked


def _merge_row(m: dict) -> dict:
    return {**m, "rule_version": dd.RULE_VERSION}


async def _apply_country(pool, plan: dd.Plan, undos: list, *, job_id: Optional[str], out: dict) -> None:
    from services.places.enqueue import GRAPH_KIND, enqueue_debounced

    applied, skipped, tp_rows, undone = 0, 0, 0, 0
    for start in range(0, len(plan.merges), BATCH):
        batch = plan.merges[start:start + BATCH]
        async with pool.acquire() as conn:
            async with conn.transaction():
                for m in batch:
                    mid = await merges.apply_merge(conn, _merge_row(m), created_by="place_dedupe", job_id=job_id)
                    if mid is None:
                        skipped += 1
                        continue
                    applied += 1
                    tp_rows += int(await conn.fetchval(
                        "SELECT coalesce(jsonb_array_length(repointed -> 'geo.tour_place'), 0) "
                        "FROM geo.place_merge WHERE id = $1::uuid", mid))
                # the stops of a merged id vanish until the next build: the build is queued with the merge
                out["graph_job"], _ = await enqueue_debounced(conn, GRAPH_KIND, reason="place_dedupe",
                                                              created_by="place_dedupe")
    for u in undos:
        async with pool.acquire() as conn:
            async with conn.transaction():
                try:
                    await merges.undo_merge(conn, u["merge_id"], actor="place_dedupe",
                                            reason=f"recheck:{u['reason']}", pin_source="recheck")
                    undone += 1
                    out["graph_job"], _ = await enqueue_debounced(conn, GRAPH_KIND, reason="place_dedupe_undo",
                                                                  created_by="place_dedupe")
                except merges.MergeError as e:
                    out.setdefault("undo_refused", []).append({"merge_id": u["merge_id"], "detail": e.detail})
    out.update(applied=applied, apply_skipped=skipped, tour_place_repointed=tp_rows, undone=undone)


async def run(pool, *, mode: str = "apply", country: Optional[str] = None, models: Optional[Models] = None,
              job_id: Optional[str] = None, max_pairs: int = dd.MAX_PAIRS_PER_RUN, progress=None) -> dict:
    """One run. mode 'apply' writes for countries in mode apply (allow-list); 'dry_run' never writes."""
    if mode not in ("apply", "dry_run"):
        raise ValueError("mode must be apply or dry_run")
    models = models or Models(None, None, [])
    async with pool.acquire() as conn:
        allow = await merges.allow_list(conn)
        usable, merged = await merges.load_rows(conn, country)
    merged_rows = {r.id: r for rs in merged.values() for r in rs}
    keeper_of_merged = {r.id: k for k, rs in merged.items() for r in rs}
    result: dict = {"rule_version": dd.RULE_VERSION, "mode": mode, "countries": {}, "usable": len(usable),
                    "merged_rows": len(merged_rows)}
    budget_left = [max(0, int(max_pairs))]
    groups = _country_groups(usable)
    for n, (ckey, rows) in enumerate(sorted(groups.items())):
        listed = allow.get(ckey)
        out: dict = {"listed": listed["mode"] if listed else None, "usable": len(rows)}
        rows_by_id = {r.id: r for r in rows}
        locs = dd.localities_of(rows)
        ctx = dd.Ctx(localities=locs.get(ckey, frozenset()))
        country_merged = {k: v for k, v in merged.items() if k in rows_by_id}
        async with pool.acquire() as conn:
            cache, pins = await merges.verdicts(conn, list(rows_by_id) + [r.id for v in country_merged.values()
                                                                         for r in v])
        verdicts = dict(pins)
        # pass 1: rules + pins → the soft keeps the embedding may promote
        plan = dd.plan_country(rows, verdicts, ctx, merged=country_merged)
        promote = [k for k, d in plan.decisions.items() if d.decision == dd.KEEP and d.embed_candidate]
        if promote:
            async with pool.acquire() as conn:
                ctx.embedding_cos = await merges.embedding_cosines(conn, promote)
            out["embedding_checked"] = len(promote)
            out["embedding_promoted"] = sum(1 for k in promote if ctx.embedding_cos.get(k, 0.0) >= dd.EMBED_COS)
            plan = dd.plan_country(rows, verdicts, ctx, merged=country_merged)
        # model band: cached verdicts for everyone, new calls for listed countries only
        band_models = models if listed else Models(None, None, models.names)
        await _model_band(pool, rows_by_id, plan, verdicts, cache, pins, band_models, budget_left, job_id, out)
        plan = dd.plan_country(rows, verdicts, ctx, merged=country_merged)
        # re-check of the job's existing merges
        undos, merge_conflicts = [], []
        async with pool.acquire() as conn:
            active = await _active_merges(conn, ckey)
        for m in active:
            k, mr = rows_by_id.get(m["keeper_id"]), merged_rows.get(m["merged_id"])
            if k is None or mr is None or keeper_of_merged.get(mr.id) != k.id:
                continue
            verdict = dd.recheck(k, mr, m["decided_by"], ctx)
            if verdict and verdict[0] == "undo":
                undos.append({"merge_id": m["id"], "merged_id": mr.id, "keeper_id": k.id, "reason": verdict[1]})
            elif verdict:
                merge_conflicts.append({"merge_id": m["id"], "reason": verdict[1]})
        changes = plan.diff() + [["undo", u["merged_id"]] for u in undos]
        # the whole diff (an approval stores it; ~80 bytes per change), the other lists capped
        out.update(plan.summary(), diff=changes, diff_size=len(changes),
                   diff_hash=merges.diff_hash(changes), undos=undos[:LIST_CAP], merge_conflicts=merge_conflicts,
                   conflicts=plan.conflicts[:LIST_CAP], defects=plan.defects[:LIST_CAP],
                   possible=[list(p) for p in plan.possible[:LIST_CAP]],
                   merges_planned=[{k: m[k] for k in ("merged_id", "keeper_id", "decided_by", "score", "reason")}
                                   for m in plan.merges[:LIST_CAP]])
        status = "dry_run"
        if mode == "apply" and listed and listed["mode"] == "apply":
            if listed["rule_version"] != dd.RULE_VERSION:
                async with pool.acquire() as conn:
                    await merges.drop_to_dry_run(conn, ckey, reason="rule_version_changed")
                status = "rule_version_changed"
            else:
                outside = dd.outside_diff(changes, listed["approved_diff"])
                limit = dd.breaker_limit(len(rows))
                out.update(outside=len(outside), breaker_limit=limit)
                if len(outside) > limit:
                    status = "held_volume"
                    logger.error("place_dedupe_held_volume", country=ckey, outside=len(outside), limit=limit)
                    await _alert_held(pool, ckey, len(outside), limit, job_id)
                else:
                    await _apply_country(pool, plan, undos, job_id=job_id, out=out)
                    status = "applied"
        out["status"] = status
        result["countries"][ckey] = out
        if progress is not None:
            await progress(step="countries", done=n + 1, total=len(groups))
    result["model_calls"] = models.calls
    result["model_cost_usd"] = round(models.cost_usd, 6)
    result["model_stopped"] = models.stopped
    if mode == "apply":
        async with pool.acquire() as conn:
            async with conn.transaction():
                await merges.lock_destinations(conn)
                result["dangling_repointed"] = await merges.repoint_dangling(conn)
            result["non_root_merges"] = await merges.assert_roots(conn)
        if result["non_root_merges"]:
            logger.error("place_dedupe_non_root_merges", n=result["non_root_merges"])
        tp = sum(c.get("tour_place_repointed", 0) for c in result["countries"].values())
        if tp >= VACUUM_MIN_ROWS:
            result["vacuum"] = await _vacuum(pool)
    return result


async def _vacuum(pool) -> str:
    """VACUUM (ANALYZE) geo.tour_place after many repoints (non-HOT updates, new HNSW entries). Needs the table owner:
    as cis_app Postgres skips it with a WARNING — recorded, autovacuum then does it."""
    try:
        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT pg_has_role(current_user, relowner, 'USAGE') FROM pg_class "
                                        "WHERE oid = 'geo.tour_place'::regclass")
            if not owner:
                return "skipped_not_owner"
            await conn.execute("VACUUM (ANALYZE) geo.tour_place")
            return "done"
    except Exception as e:  # noqa: BLE001
        return f"failed:{type(e).__name__}"
