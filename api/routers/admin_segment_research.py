"""AA-646 — admin-triggered search-demand research (the only path that buys DataForSEO demand data).

Before AA-646 `run_segment_research()` fired after every tenant T2 rewrite and, because AA-545 made
Segments platform-wide, swept every platform place for the tenant's markets (25/09/2026: 2,215
places x AU/US/UK, $49). Research is now an explicit admin action:

  - POST /admin/segment-research/preview — free: scope, stale count, selected count, and the spend
                                           ceiling from the budgets (AA-649). No LLM, no DFS.
  - POST /admin/segment-research/run     — enqueues one `segment_research` job (admin secret required).

Both take explicit markets. AA-1055: a run is bounded by `max_usd` (the selection's capped DFS estimate, default and
ceiling: what the DFS budget still allows, at most MAX_USD_PER_RUN; the job also makes it the run's hard DFS spend
limit) instead of the old 200-place cap; `max_places` is an optional extra limit.
`tiers` picks volume and / or the one SERP per place, `use_suggestions` is off by default, `dfs_queue` picks the live
endpoint or the Standard queue. `reatomized` (AA-1055 stale guard) limits the scope to the places atomized since their
last research: a re-atomize "queues" its new places as that batch, bought only when an admin runs it. Only one run at
a time. AA-650: a run is
a durable job (shared.job, services/jobs/segment_research_job.py), so a deploy mid-run re-queues it
instead of losing it; places that fail or are skipped by an abort stay stale and are picked up by the
next run (AA-647).

AA-649 — every run is bounded by shared.spend_budget (DFS + Bedrock, job kind `segment_research`):
the ceiling is shown before the run, each paid call is checked before it is sent, and the run is
refused up front when the DFS balance is below the selection's capped estimate (AA-1055; it used to be the whole
remaining budget, which refused small runs whenever the balance was low).

AA-1122 — the hard monthly research cap (setting cis.spend.research_max_usd_per_month, DFS + LLM + Jev of every research
job, manual and automatic) is refused up front by /run (409 "month cap reached") and shown by /preview; the job checks
it again before every paid post. GET /status is the Research tab of SEO Intelligence: the automatic research switch
and caps, the month spend against the cap (and every DataForSEO stage of the month, so the research cap is never read
as the whole DataForSEO bill), what waits (new places, stale per market, new markets), the last 10 runs with their
DFS + LLM USD, the next decision of the nightly tick, the volume history and the tour PAA coverage.
POST /tour-paa/preview (free) and /tour-paa/run enqueue the tour People Also Ask backfill (job tour_paa_backfill).
"""
from __future__ import annotations

from typing import Literal, Optional

import asyncio

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field, field_validator

from api.routers.admin import verify_admin_secret
from services.seo_intelligence.dfs_queue import research_queue_mode
from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
from services.jobs.segment_research_job import KIND as JOB_KIND, dfs_balance, load_budgets, max_usd_per_run
from shared.jobs import queue as job_queue
from shared.research_budget import with_caps
from shared.jobs.registry import enqueue

logger = structlog.get_logger()

# S241 (GĐ −1): every route of this router needs the admin secret. Router-level so a new GET can not
# ship open again (the reads here were public until S241). The inline verify_admin_secret() calls in
# the write routes are kept as-is (same check, run twice).
# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/segment-research", tags=["admin-segment-research"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

class ResearchScope(BaseModel):
    markets: list[str] = Field(..., min_length=1, description="Buyer market codes, e.g. ['US','UK']")
    max_usd: Optional[float] = Field(None, gt=0,
                                     description="Cap on the selection's capped DFS estimate (default: DFS budget); at "
                                                 "most the setting cis.spend.segment_research_max_usd_per_run")
    max_places: Optional[int] = Field(None, ge=1)
    places: Optional[list[str]] = None
    tour_ids: Optional[list[str]] = None
    country: Optional[str] = None
    # AA-648 — "batch": few large paid tasks. AA-1055: the per-place "loop" strategy is retired (its estimate did not
    # cover the SERPs and suggestions it bought).
    strategy: Literal["batch"] = "batch"
    # AA-1055 — keyword suggestions (keywords_for_keywords) are off by default.
    use_suggestions: bool = False
    tiers: list[Literal["volume", "serp"]] = Field(default_factory=lambda: ["volume", "serp"], min_length=1)
    # None = the env setting DFS_RESEARCH_QUEUE (live by default).
    dfs_queue: Optional[Literal["live", "standard"]] = None
    recompute: bool = True
    # AA-1055 (stale guard): only the places atomized since their last research (a new or re-atomized tour's places,
    # segment_research.reatomized_places). The batch a re-atomize queues: preview it (cost shown), then run it.
    reatomized: bool = Field(False, description="Only places atomized since their last research (re-atomize batch)")

    @field_validator("markets")
    @classmethod
    def _known_markets(cls, v: list[str]) -> list[str]:
        codes = [m.strip().upper() for m in v]
        unknown = [m for m in codes if m not in DFS_LOCATION_MAP]
        if unknown:
            raise ValueError(f"unknown market(s) {unknown}; supported: {sorted(DFS_LOCATION_MAP)}")
        return codes

    @field_validator("max_usd")
    @classmethod
    def _within_setting(cls, v: Optional[float]) -> Optional[float]:
        # S279 thresholds L1: the per-run ceiling is a setting (Platform › Settings), read per request
        if v is not None and v > max_usd_per_run():
            raise ValueError(f"max_usd must be at most {max_usd_per_run():g} (setting "
                             "cis.spend.segment_research_max_usd_per_run)")
        return v


def _kwargs(scope: ResearchScope) -> dict:
    return {
        "places": scope.places,
        "tour_ids": scope.tour_ids,
        "country": scope.country,
        "max_places": scope.max_places,
        "max_usd": scope.max_usd,
        "strategy": scope.strategy,
        "use_suggestions": scope.use_suggestions,
        "tiers": list(scope.tiers),
        # AA-1055: the mode is resolved here (payload, else DFS_RESEARCH_QUEUE of this task) and written into the job
        # payload, so the job buys through the endpoint the preview and the balance check were priced at.
        "dfs_queue": research_queue_mode(scope.dfs_queue),
        "recompute": scope.recompute,
        "reatomized": scope.reatomized,
    }


def _capped(kwargs: dict, dfs_budget) -> dict:
    """The kwargs with max_usd bounded by what the DFS budget still allows (the job applies the same rule)."""
    from services.jobs.segment_research_job import max_usd_for
    return {**kwargs, "max_usd": max_usd_for(kwargs, dfs_budget)}


async def _active_job(pool) -> Optional[dict]:
    return await job_queue.latest(pool, JOB_KIND, ("queued", "running"))


async def _month_cap(pool):
    """The research month cap with the ledger's month spend, or 503 when the ledger cannot be read (fail closed)."""
    from shared.research_budget import load_research_month_cap

    try:
        return await load_research_month_cap(pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("research_month_spend_unreadable", error=str(exc)[:200])
        raise HTTPException(status_code=503, detail="research spend ledger unreadable: no run is started") from exc


def _refuse_spent_month(month) -> None:
    if month.remaining() <= 0:
        raise HTTPException(status_code=409,
                            detail=f"month cap reached: spent ${month.spent:.2f} of ${month.cap_usd:.2f} "
                                   "(setting cis.spend.research_max_usd_per_month)")


@router.post("/preview", summary="Scope + stale count + spend ceiling (no LLM/DFS spend)")
async def preview_research(scope: ResearchScope, request: Request):
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    dfs_budget, llm_budget = await load_budgets(pool)
    month = await _month_cap(pool)
    dfs_budget = with_caps(dfs_budget, month)
    result = await run_segment_research({"countries": scope.markets}, pool, dry_run=True,
                                        **_capped(_kwargs(scope), dfs_budget))
    active = await _active_job(pool)
    return {
        **result,
        "month": month.summary(),
        "running": active is not None,
        "active_job_id": active["id"] if active else None,
        # AA-649 — the most this run can spend; the run stops before crossing either figure.
        "max_spend_usd": {"dfs": dfs_budget.remaining_usd(), "bedrock": llm_budget.remaining_usd()},
        "budgets": [dfs_budget.summary(), llm_budget.summary()],
    }


@router.post("/run", status_code=202, summary="Enqueue one admin-triggered research run")
async def run_research(scope: ResearchScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.acp_contract.segment_research import run_segment_research

    pool = request.app.state.pool
    active = await _active_job(pool)
    if active is not None:
        raise HTTPException(status_code=409,
                            detail=f"A segment research run is already {active['status']} (job {active['id']})")

    dfs_budget, llm_budget = await load_budgets(pool)
    month = await _month_cap(pool)
    _refuse_spent_month(month)                     # AA-1122: refused up front, before any read of the scope
    dfs_budget = with_caps(dfs_budget, month)
    kwargs = _capped(_kwargs(scope), dfs_budget)
    preview = await run_segment_research({"countries": scope.markets}, pool, dry_run=True, **kwargs)
    if preview["places_selected"] == 0:
        return {**preview, "started": False, "reason": "nothing stale in scope (or nothing fits max_usd)"}

    for budget in (dfs_budget, llm_budget):
        remaining = budget.remaining_usd()
        if remaining is not None and remaining <= 0:
            raise HTTPException(status_code=409, detail=f"{budget.provider} budget exhausted: {budget.summary()}")

    # AA-649 / AA-1055 — refuse up front when the DFS balance cannot cover the selection's capped estimate, instead of
    # draining the account mid-run (the 25/09 failure mode). An unreadable balance also refuses.
    # The job re-checks both when it starts (a retry can start much later).
    balance = await dfs_balance()
    needed = preview["estimate"]["dfs_usd_cap"]
    if balance is None or balance < needed:
        raise HTTPException(
            status_code=409,
            detail=(f"DataForSEO balance {balance} is below this run's capped estimate {needed}; "
                    "top up or lower max_usd"),
        )

    job_id, _ = await enqueue(pool, JOB_KIND, {"markets": scope.markets, **_kwargs(scope)},
                              created_by=f"admin:{x_admin_user_id or 'unknown'}")
    logger.info("admin_segment_research_enqueued", admin_user=x_admin_user_id, markets=scope.markets,
                places_selected=preview["places_selected"], dfs_balance=balance, job_id=job_id)
    return {**preview, "started": True, "job_id": job_id, "dfs_balance_usd": balance,
            "max_spend_usd": {"dfs": kwargs["max_usd"], "bedrock": llm_budget.remaining_usd()}}


RUNS_SQL = """
    SELECT j.id::text AS id, j.kind, j.status, j.created_by, j.payload->>'scope' AS scope,
           j.payload->'markets' AS markets, j.created_at, j.finished_at, left(j.error, 300) AS error,
           j.result->>'abort_reason' AS stop_reason, j.result->>'places_researched' AS places_researched,
           j.result->>'places_selected' AS places_selected, j.result->>'filled_store' AS filled_store,
           j.result->>'filled_serp' AS filled_serp,
           (SELECT coalesce(sum(d.cost_usd), 0) FROM shared.dfs_call_log d WHERE d.job_id = j.id)::float8 AS dfs_usd,
           (SELECT coalesce(sum(l.cost_usd), 0) FROM shared.llm_call_log l WHERE l.job_id = j.id)::float8 AS llm_usd
      FROM shared.job j WHERE j.kind = ANY($1::text[]) ORDER BY j.created_at DESC LIMIT 10
"""
# C13 of the S282 design: every DataForSEO stage of the month, so the research cap is one line of the whole bill.
DFS_STAGES_SQL = """
    SELECT coalesce(stage, 'unattributed') AS stage, coalesce(sum(cost_usd), 0)::float8 AS usd, count(*) AS calls
      FROM shared.dfs_call_log WHERE created_at >= $1 GROUP BY 1 ORDER BY 2 DESC
"""
STATUS_PENDING_TIMEOUT_S = 25.0   # technical-constant: bound of the panel's pending read (it then shows "not measured")


def _num(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _panel(pool) -> dict:
    """The Research tab's data (module docstring). Every part is bounded; a failed part is null with its error."""
    import json as _json

    from services.acp_contract import research_auto as ra
    from services.acp_contract.research_coverage import ReadOnlyPool
    from services.seo_intelligence import search_history, tour_paa
    from shared.research_budget import RESEARCH_JOB_KINDS, load_research_month_cap, month_start

    out: dict = {"errors": {}}
    cfg = ra.config()
    out["auto"] = {k: cfg[k] for k in ("enabled", "window_start", "window_end", "new_min_places", "new_max_wait_days",
                                       "refresh_every_days", "auto_max_usd_per_run", "min_run_usd", "dfs_queue",
                                       "tenant_statuses", "max_failures")}
    try:
        month = await load_research_month_cap(pool)
        out["month"] = {**month.summary(), "spent_usd": round(month.spent, 4), "warn_pct": cfg["month_warn_pct"]}
    except Exception as exc:  # noqa: BLE001
        month, out["month"] = None, None
        out["errors"]["month"] = str(exc)[:200]
    async with pool.acquire() as conn:
        out["runs"] = [{**dict(r), "markets": _json.loads(r["markets"]) if isinstance(r["markets"], str)
                        else r["markets"], "places_researched": _num(r["places_researched"]),
                        "places_selected": _num(r["places_selected"]), "filled_store": _num(r["filled_store"]),
                        "filled_serp": _num(r["filled_serp"])}
                       for r in await conn.fetch(RUNS_SQL, list(RESEARCH_JOB_KINDS))]
        out["dfs_month_by_stage"] = [dict(r) for r in await conn.fetch(DFS_STAGES_SQL, month_start())]
    ro = ReadOnlyPool(pool, statement_timeout="15s")
    try:
        markets = await ra.auto_markets(ro, cfg["tenant_statuses"])
        waiting = await asyncio.wait_for(ra.pending(ro, markets, cfg["activation_stale_pct"]),
                                         timeout=STATUS_PENDING_TIMEOUT_S)
        out["pending"] = waiting.as_dict()
        if month is not None:
            from datetime import datetime, timezone
            jobs = await ra.recent_jobs(pool)
            d = ra.decide(datetime.now(timezone.utc), cfg, waiting, jobs, month)
            out["next"] = {"action": d.action, "kind": d.kind, "max_usd": d.max_usd, "reason": d.reason,
                           "streak": ra.failure_streak(jobs)}
    except Exception as exc:  # noqa: BLE001 — a slow or failed measure: the panel still shows the rest
        out["pending"] = None
        out["errors"]["pending"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    for name, call in (("history", search_history.counts(pool)), ("tour_paa", tour_paa.coverage(pool))):
        try:
            out[name] = await call
        except Exception as exc:  # noqa: BLE001 — before CIS 450 / 451 are applied
            out[name] = None
            out["errors"][name] = str(exc)[:200]
    return out


@router.get("/status", summary="The active run, the last run and the Research tab (auto, month cap, pending, runs)")
async def research_status(request: Request):
    pool = request.app.state.pool
    active = await _active_job(pool)
    last = await job_queue.latest(pool, JOB_KIND, ("succeeded", "failed", "stopped_budget", "cancelled"))
    return {
        "running": active is not None,
        "active_job": active,
        "last_result": last["result"] if last else None,
        "last_job": last,
        **(await _panel(pool)),
    }


class TourPaaScope(BaseModel):
    paid: bool = Field(True, description="Buy SERPs for the tours the store cannot fill (within the caps)")
    tour_ids: Optional[list[str]] = Field(None, max_length=1000)


@router.post("/tour-paa/preview", summary="Tour People Also Ask backfill: what the store fills and the SERP price ($0)")
async def tour_paa_preview(scope: TourPaaScope, request: Request):
    from services.seo_intelligence import tour_paa

    return await tour_paa.preview(request.app.state.pool, scope.tour_ids, paid=scope.paid)


@router.post("/tour-paa/run", status_code=202, summary="Enqueue the tour People Also Ask backfill")
async def tour_paa_run(scope: TourPaaScope, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.jobs.tour_paa_job import KIND as PAA_KIND

    pool = request.app.state.pool
    if scope.paid:
        _refuse_spent_month(await _month_cap(pool))
    active = await job_queue.latest(pool, PAA_KIND, ("queued", "running"))
    if active is not None:
        raise HTTPException(status_code=409, detail=f"a tour PAA backfill is already {active['status']} "
                                                    f"(job {active['id']})")
    payload = {"paid": scope.paid, **({"tour_ids": scope.tour_ids} if scope.tour_ids else {})}
    job_id, _ = await enqueue(pool, PAA_KIND, payload, created_by=f"admin:{x_admin_user_id or 'unknown'}")
    logger.info("admin_tour_paa_enqueued", admin_user=x_admin_user_id, paid=scope.paid, job_id=job_id)
    return {"started": True, "job_id": job_id, "paid": scope.paid}
