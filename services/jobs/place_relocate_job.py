"""AA-997 — `place_relocate` job kind (platform, background, cap 1): re-locate destinations whose point came from the
model through the geocoder-only path (services/places/relocate.py; S254 geocode audit, fix plan step 1).

Payload: {"mode": "dry_run" | "apply" | "hide" | "unhide" (default dry_run), "located_by"?: ["llm"] (| "mapbox"),
"country"?, "limit"? (apply, ≤ 400 a run), "hide_unconfirmed"? (default false), "reason"?,
AA-997 v2: "recheck"? (outcomes of an earlier rule version to take again: llm_point / off_route / confirmed; default
none), "strategy"? ("v1" default | "v2": query variants, hint bias, compact names — geocoder.STRATEGIES),
"accept_rule"? ("v1" default | "aa997.2" | "aa997.3": the strict acceptance; recheck confirmed / geoapify need it —
geoapify is dry-run only), "max_requests"? (a cap on Geoapify requests over the whole apply chain, "chain_id" set by
the endpoint: the spent count is read from geo.geocode_call of the chain's jobs, so a retried or crashed run is
counted too), "rule_version" (set by the endpoint; an apply whose rule version is not the deployed one is refused, so
a chain queued before a rule change stops instead of running the new rule with an old dry run), "reasons"? /
"confirm"? (hide), "outcomes"? / "stamp_versions"? / "stamp_accept"? / "confirm"? / "dry_run_job"? / "max_rows"?
(hide_flagged: rows a relocate run stamped llm_point / off_route (| weak_confirm) and left on the model's point,
located_by as given; report unless confirm, a confirm hides only the unchanged rows of the report dry_run_job),
"by"? / "destination_ids"? (unhide: the policy stamps to revert, only these rows)}.
Queued by POST /admin/places/relocate.

dry_run: no paid call and no write — the counts and the Geoapify credit estimate in the job result.
apply: ≤ `limit` rows a run under the Geoapify lock shared with place_geocode / place_shape (services/places/geolock);
re-enqueues itself while rows remain. Geoapify credits: one search request (1 credit) per query of a row's name
(geomatch.normalise: 1, or 2 for "X to Y"); this job stops RESERVE_CREDITS below the daily limit
(GEOAPIFY_DAILY_CREDIT_LIMIT, default 2,500 of the Free plan's 3,000/day), so new names of the day are still geocoded,
and resumes after the UTC credit reset. Free plan $0; on a paid plan GEOCODER_COST_PER_REQUEST_USD per request (the
geocoder/job budget rows apply as in place_geocode). No model call. Each run logs its requests in geo.geocode_call.
A run that confirmed or hid rows queues place_dedupe and tour_graph_build.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from services.jobs.place_geocode_job import BudgetedGeocoder
from services.places import relocate, store
from services.places.enqueue import (GRAPH_KIND, RELOCATE_KIND, enqueue_debounced, enqueue_dedupe,
                                     next_credit_reset)
from services.places.geocoder import DEFAULT_DAILY_CREDITS, _env_float, get_geocoder
from services.places.geolock import geoapify_lock
from shared.cost_guard import BudgetExceeded, load_run_budget, maybe_alert
from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = RELOCATE_KIND
# apply payloads queued before AA-997 v2 carry no rule_version: they were dry-run against this rule
PRE_V2_RULE = "aa997.1"
LOCKED_DELAY_S = 120
CONTINUE_DELAY_S = 15


MODES = ("dry_run", "apply", "hide", "unhide", "hide_flagged")
NO_CALL_MODES = ("hide", "unhide", "hide_flagged")
MAX_UNHIDE_IDS = 500


def _int_or_none(v) -> Optional[int]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _payload(p: dict) -> dict:
    from services.places.geocoder import STRATEGIES, STRATEGY_V1
    from services.places.geomatch import ACCEPT_RULE_V1, ACCEPT_RULES
    mode = p.get("mode") if p.get("mode") in MODES else "dry_run"
    limit = max(1, min(int(p.get("limit") or relocate.MAX_ROWS_PER_RUN), relocate.MAX_ROWS_PER_RUN))
    out = {"mode": mode, "located_by": relocate.located_by_list(p.get("located_by")),
           "country": (p.get("country") or None), "limit": limit,
           "hide_unconfirmed": bool(p.get("hide_unconfirmed"))}
    # AA-997 v2 keys, present only when used (an old payload reads exactly as before)
    accept_rule = p.get("accept_rule") if p.get("accept_rule") in ACCEPT_RULES else ACCEPT_RULE_V1
    if accept_rule != ACCEPT_RULE_V1:
        out["accept_rule"] = accept_rule
    if p.get("recheck"):
        out["recheck"] = relocate.recheck_list(p.get("recheck"))
    if p.get("strategy") in STRATEGIES and p.get("strategy") != STRATEGY_V1:
        out["strategy"] = p["strategy"]
    if _int_or_none(p.get("max_requests")):
        out["max_requests"] = _int_or_none(p.get("max_requests"))
        out["spent"] = max(0, int(p.get("spent") or 0))
        if p.get("chain_id"):
            out["chain_id"] = str(p["chain_id"])
    if p.get("rule_version"):
        out["rule_version"] = str(p["rule_version"])
    if mode == "hide":
        out["reasons"] = relocate.hide_reasons(p.get("reasons"))
        out["confirm"] = bool(p.get("confirm"))
    if mode == "hide_flagged":
        out["outcomes"] = relocate.hide_flagged_outcomes(p.get("outcomes"))
        out["stamp_versions"] = sorted({str(v) for v in (p.get("stamp_versions") or []) if v})
        out["stamp_accept"] = str(p["stamp_accept"]) if p.get("stamp_accept") else None
        out["confirm"] = bool(p.get("confirm"))
        if p.get("max_rows") is not None:
            try:
                out["max_rows"] = max(0, int(p["max_rows"]))
            except (TypeError, ValueError):
                out["max_rows"] = 0         # unreadable cap: a confirm refuses rather than hides an unknown set
        if p.get("dry_run_job"):
            out["dry_run_job"] = str(p["dry_run_job"])
    if mode == "unhide":
        out["by"] = relocate.policy_stamps(p.get("by"))
        ids = [str(i) for i in (p.get("destination_ids") or []) if i][:MAX_UNHIDE_IDS]
        if ids:
            out["destination_ids"] = ids
    return out


def daily_credits() -> int:
    """This job's share of the day: the place_geocode credit limit minus RESERVE_CREDITS (never below 0)."""
    limit = int(_env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", DEFAULT_DAILY_CREDITS))
    return max(0, limit - relocate.RESERVE_CREDITS)


async def _chain_spent(ctx: JobContext, p: dict) -> int:
    """Requests the apply chain already sent: geo.geocode_call rows of the chain's jobs (logged in each run's
    `finally`, so a retried attempt or a run stopped by an exception counts), never below the payload's carried
    `spent`. A chain with no chain_id (queued before) uses the carried count only."""
    carried = int(p.get("spent") or 0)
    if not p.get("chain_id"):
        return carried
    try:
        async with ctx.pool.acquire() as conn:
            return max(carried, await store.chain_requests(conn, KIND, p["chain_id"]))
    except Exception as e:  # noqa: BLE001 — the carried count is the fallback
        logger.warning("place_relocate_chain_spent_failed", error=str(e)[:200])
        return carried


async def _requeue(ctx: JobContext, p: dict, *, run_after: datetime, key: Optional[str]) -> str:
    job_id, _ = await enqueue(ctx.pool, KIND, {**p, "reason": f"{KIND}:continue"}, created_by=KIND,
                              run_after=run_after, idempotency_key=key)
    return job_id


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    p = _payload(ctx.payload or {})
    if p["mode"] in NO_CALL_MODES:     # no call: the readers' hide-unconfirmed policy (report only unless confirm)
        actor = (getattr(ctx.job, "created_by", None) or KIND)[:64]
        if p["mode"] == "hide":
            out = await relocate.hide(ctx.pool, reasons=p["reasons"], country=p["country"], confirm=p["confirm"],
                                      actor=actor, job_id=ctx.job_id)
        elif p["mode"] == "hide_flagged":
            out = await relocate.hide_flagged(ctx.pool, outcomes=p["outcomes"], stamp_versions=p["stamp_versions"],
                                              stamp_accept=p["stamp_accept"], located_by=p["located_by"],
                                              country=p["country"], confirm=p["confirm"],
                                              dry_run_job=p.get("dry_run_job"), max_rows=p.get("max_rows"),
                                              actor=actor, job_id=ctx.job_id)
        else:
            out = await relocate.unhide(ctx.pool, country=p["country"], by=p["by"],
                                        destination_ids=p.get("destination_ids"), actor=actor, job_id=ctx.job_id)
        # the Tour Graph drops / takes back suspect rows on rebuild; queued after every write run, not only when this
        # attempt changed rows: a retry after a crash between batches finds the committed rows already done (0)
        if p["mode"] == "unhide" or p.get("confirm"):
            out["graph_job"], _ = await enqueue_debounced(ctx.pool, GRAPH_KIND, reason=KIND, created_by=KIND)
        return out
    inner = get_geocoder()
    if p["mode"] == "dry_run":
        async with ctx.pool.acquire() as conn:
            return await relocate.dry_run(conn, located_by=p["located_by"], country=p["country"],
                                          cost_per_request=inner.cost_per_request, daily_credits=daily_credits(),
                                          recheck=p.get("recheck"), strategy=p.get("strategy", "v1"),
                                          max_requests=p.get("max_requests"),
                                          accept_rule=p.get("accept_rule", "v1"))
    if daily_credits() <= 0:
        return {"status": "refused_no_credit_share", "detail": (
            f"GEOAPIFY_DAILY_CREDIT_LIMIT leaves no share after the {relocate.RESERVE_CREDITS} credits kept for "
            "place_geocode: raise the limit; re-queueing would wait for a credit reset that never helps")}
    if not inner.calls_external:
        return {"status": "refused_no_geocoder", "geocoder": inner.name,
                "detail": "apply needs GEOCODER_PROVIDER=geoapify: re-locating with model_only would change nothing"}
    if p.get("rule_version", PRE_V2_RULE) != relocate.RULE_VERSION:
        return {"status": "refused_rule_version_changed", "payload_rule_version": p.get("rule_version", PRE_V2_RULE),
                "rule_version": relocate.RULE_VERSION,
                "detail": "the rule changed since this chain's dry run: run a new dry run, then apply"}
    held_back = sorted(set(p.get("recheck") or ()) & set(relocate.RECHECK_DRY_RUN_ONLY))
    if held_back:
        return {"status": "refused_recheck_dry_run_only", "recheck": held_back,
                "detail": "this recheck is dry-run only until a labelled sample shows the re-judge keeps correct rows"}
    if p.get("max_requests"):
        p = {**p, "spent": await _chain_spent(ctx, p)}
        if p["spent"] >= p["max_requests"]:
            return {"status": "stopped_request_cap", "spent": p["spent"], "max_requests": p["max_requests"]}
    async with geoapify_lock(ctx.pool) as held:
        if not held:
            job = await _requeue(ctx, p, run_after=datetime.now(timezone.utc) + timedelta(seconds=LOCKED_DELAY_S),
                                 key=None)
            return {"status": "waiting_for_geoapify_lock", "next_job": job}
        return await _apply_locked(ctx, p, inner)


async def _apply_locked(ctx: JobContext, p: dict, inner) -> dict:
    started = datetime.now(timezone.utc)
    if hasattr(inner, "credits_used_today"):
        async with ctx.pool.acquire() as conn:
            inner.credits_used_today = await store.geocoder_requests_today(conn, inner.name)
        inner.daily_credit_limit = daily_credits()
    geo_budget = await load_run_budget(ctx.pool, "geocoder", KIND)
    geocoder = BudgetedGeocoder(inner, geo_budget)
    result: dict = {}
    try:
        cap = (p["max_requests"] - p.get("spent", 0)) if p.get("max_requests") else None
        result = await relocate.apply(ctx.pool, geocoder, located_by=p["located_by"], country=p["country"],
                                      limit=p["limit"], hide_unconfirmed=p["hide_unconfirmed"],
                                      progress=ctx.progress, recheck=p.get("recheck"),
                                      strategy=p.get("strategy", "v1"), max_requests=cap,
                                      accept_rule=p.get("accept_rule", "v1"))
    except BudgetExceeded as e:
        stopped = {"status": "stopped_budget", **result}
        if p.get("max_requests"):       # the next run reads the chain's geo.geocode_call rows; shown here too
            stopped["spent"] = p.get("spent", 0) + inner.requests
        ctx.set_result(stopped)
        await maybe_alert(ctx.pool, geo_budget, reason=str(e))
        raise
    finally:
        cost = round(inner.requests * inner.cost_per_request, 6)
        ctx.add_cost(cost)
        try:
            async with ctx.pool.acquire() as conn:
                await store.log_geocode_calls(conn, inner.name, inner.requests, cost, ctx.job_id)
        except Exception as e:  # noqa: BLE001 — the spend is also on the job row
            logger.warning("geocode_call_log_failed", error=str(e)[:200])
        await inner.aclose()
    result["geocoder_requests"] = inner.requests
    result["geocoder_cost_usd"] = round(inner.requests * inner.cost_per_request, 6)
    if p.get("max_requests"):
        p = {**p, "spent": p.get("spent", 0) + inner.requests}     # carried to the next run of the chain
        result["spent"], result["max_requests"] = p["spent"], p["max_requests"]
    try:
        if (result.get("confirmed") or result.get("reconfirmed")
                or (p["hide_unconfirmed"] and result.get("done"))):
            result["dedupe_job"], _ = await enqueue_dedupe(ctx.pool, reason=KIND, created_by=KIND)
            result["graph_job"], _ = await enqueue_debounced(ctx.pool, GRAPH_KIND, reason=KIND, created_by=KIND)
        if result.get("stopped_geocoder_unavailable"):
            result["status"] = "stopped_geocoder_unavailable"
        elif result.get("stopped_request_cap") or (p.get("max_requests") and p["spent"] >= p["max_requests"]):
            result["status"] = "stopped_request_cap"
        elif result.get("stopped_credit_limit"):
            result["status"] = "stopped_credit_limit"
            at = next_credit_reset(started)
            result["next_job"] = await _requeue(ctx, p, run_after=at, key=f"{KIND}:credit_reset:{at:%Y-%m-%d}")
            result["next_run_after"] = at.isoformat()
        elif result.get("remaining") and result.get("done"):
            result["next_job"] = await _requeue(
                ctx, p, run_after=datetime.now(timezone.utc) + timedelta(seconds=CONTINUE_DELAY_S), key=None)
            result["status"] = "continuing"
        else:
            result["status"] = "ok"
    except Exception as e:  # noqa: BLE001 — follow-ups are best-effort
        result["follow_up_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logger.info("place_relocate_done", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result
