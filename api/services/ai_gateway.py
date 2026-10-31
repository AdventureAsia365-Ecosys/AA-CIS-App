"""AA-804 (UI part 1, root ADR 0009) — the read side of the admin "AI providers" page (/admin/ai-gateway).

Two reads, both $0 (database only; no provider is called):

- ``read_providers(pool)`` — ``GET /admin/ai-gateway/providers``: one row per paid source.
    * every LLM connection (``shared.llm_provider_connection``): its circuit health (``shared.llm_provider_health``
      through ``api.services.llm_availability.read_connections``), its spend today and this month from
      ``shared.llm_call_log`` (rows whose ``account`` is the connection key, the same attribution as the monthly
      budget, ``shared.cost_guard.connection_budgets``) and its monthly budget state;
    * Jev (TypeSafe): the estimated credit left (``shared.jev_credit.estimate``: top-ups minus logged spend; TypeSafe
      has no balance API), its spend (``llm_call_log`` provider ``typesafe``) and the last 24 h of verdicts
      (``shared.decision_log``: calls, errors, mean latency of uncached calls);
    * DataForSEO: the latest stored balance (``shared.dfs_balance_snapshot``, written by the daily check; never a live
      call) and its spend (``shared.dfs_call_log``);
    * Geoapify: the credits used today (``geo.geocode_call``, the UTC day = Geoapify's credit window) against the
      daily credit limit the geocoder enforces (``GEOAPIFY_DAILY_CREDIT_LIMIT``) and its spend.
  Every row also carries its provider-wide daily cap (``shared.spend_budget``, scope ``global``) with today's spend
  against it. Each source is read on its own: a source that fails gives its rows ``status = unknown`` and an
  ``error`` text, the other rows still show.
- ``trace_job(pool, job_id)`` — ``GET /admin/ai-gateway/trace?job_id=``: every logged paid call of one job, in time
  order, from the four ledgers that carry a job id: ``shared.llm_call_log`` (index ``llm_call_log_job_idx``),
  ``shared.dfs_call_log`` (``idx_dfs_call_log_job``), ``geo.geocode_call`` and ``shared.decision_log``. The last two
  have no job index, so their reads are bounded by the job's run window (``started_at`` .. ``finished_at`` or now; the
  ``created_at`` / ``called_at`` indexes serve the range). ``started_at`` keeps the first attempt's start
  (shared/jobs/queue.py), so the window covers every attempt.

Limits of today's ledgers (ledger v2 lifts them): a call the provider refused writes no row (only billed calls are
logged), ``llm_call_log`` has no latency or outcome column, and nothing links a Jev verdict to its cost row.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import structlog

from api.core.error_ref import log_exception_ref, public_error

logger = structlog.get_logger()

# ── shared ──────────────────────────────────────────────────────────────────────────────────────────────────────────

# The LLM connection kinds in the order the page lists them (an unknown kind sorts last).
KIND_ORDER = ("bedrock", "anthropic", "openai", "azure_openai", "mock")
# shared.spend_budget provider of each connection kind (shared/cost_guard.py PROVIDERS); mock has no budget.
BUDGET_PROVIDER_OF_KIND = {"bedrock": "bedrock", "anthropic": "anthropic", "openai": "openai",
                           "azure_openai": "azure_openai"}
WARN_PCT = 80.0
DECISION_WINDOW_HOURS = 24


def day_and_month_start(now: datetime) -> tuple[datetime, datetime]:
    """The UTC midnight and the UTC first of the month of ``now`` (the windows cost_guard uses)."""
    now = now.astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


def budget_provider(provider: Optional[str]) -> Optional[str]:
    """The spend_budget provider a call-log ``provider`` value counts against (cost_guard._DAY_SPEND_SQL rules:
    ``bedrock%`` is bedrock; Jev is logged as ``typesafe``)."""
    if not provider:
        return None
    p = provider.lower()
    if p.startswith("bedrock"):
        return "bedrock"
    if p in ("typesafe", "jev"):
        return "jev"
    return p


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _f(v: Any) -> Optional[float]:
    return None if v is None else round(float(v), 6)


def _pct(used: Optional[float], limit: Optional[float]) -> Optional[float]:
    if used is None or not limit:
        return None
    return round(100.0 * float(used) / float(limit), 1)


async def _guard(source: str, fn: Callable[[], Awaitable[Any]]) -> tuple[Any, Optional[str]]:
    """(result, None), or (None, public error text) when the read raised: one source never breaks the page."""
    try:
        return await fn(), None
    except Exception:  # noqa: BLE001 — logged with a ref; the client gets the generic text
        ref = log_exception_ref("ai_gateway_source_failed", source=source)
        return None, public_error(f"{source} read", ref)


# ── providers ───────────────────────────────────────────────────────────────────────────────────────────────────────

# Month-to-date LLM ledger per (provider, account): today's and the month's spend, the calls and the newest call.
# The created_at index serves the month range ($2 .. $3); $1 is today's UTC midnight.
LEDGER_SQL = """
    SELECT provider, account,
           coalesce(sum(cost_usd) FILTER (WHERE created_at >= $1), 0)::float AS today_usd,
           coalesce(sum(cost_usd), 0)::float AS month_usd,
           count(*)::int AS month_calls,
           max(created_at) AS last_call_at
      FROM shared.llm_call_log
     WHERE created_at >= $2 AND created_at < $3
     GROUP BY provider, account
"""

BUDGETS_SQL = """
    SELECT provider, per_day_usd, hard_stop, alert_pct
      FROM shared.spend_budget
     WHERE scope = 'global'
"""

DECISIONS_SQL = """
    SELECT count(*)::int AS calls,
           count(*) FILTER (WHERE zone = 'error' OR error IS NOT NULL)::int AS errors,
           count(*) FILTER (WHERE cached)::int AS cached,
           avg(latency_ms) FILTER (WHERE NOT cached AND latency_ms IS NOT NULL)::float AS mean_latency_ms,
           max(created_at) AS last_at
      FROM shared.decision_log
     WHERE created_at >= $1 AND created_at < $2
"""

DFS_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd) FILTER (WHERE created_at >= $1), 0)::float AS today_usd,
           coalesce(sum(cost_usd), 0)::float AS month_usd,
           count(*)::int AS month_calls,
           count(*) FILTER (WHERE fetched_live)::int AS month_live_calls,
           max(created_at) AS last_call_at
      FROM shared.dfs_call_log
     WHERE created_at >= $2 AND created_at < $3
"""

DFS_BALANCE_SQL = """
    SELECT balance_usd::float AS balance_usd, currency, fetched_at
      FROM shared.dfs_balance_snapshot
     ORDER BY fetched_at DESC
     LIMIT 1
"""

# Credits as the geocoder's daily guard counts them (services/places/store.geocoder_requests_today): rows older than
# migration 255 carry no credits and count one per request.
GEO_SQL = """
    SELECT coalesce(sum(coalesce(credits, requests)) FILTER (WHERE called_at >= $1), 0)::int AS credits_today,
           coalesce(sum(cost_usd) FILTER (WHERE called_at >= $1), 0)::float AS today_usd,
           coalesce(sum(cost_usd), 0)::float AS month_usd,
           coalesce(sum(requests), 0)::int AS month_requests,
           max(called_at) AS last_call_at
      FROM geo.geocode_call
     WHERE provider = $4 AND called_at >= $2 AND called_at < $3
"""


def _spend(today: float = 0.0, month: float = 0.0, calls: int = 0, last: Any = None) -> dict:
    return {"today_usd": _f(today) or 0.0, "month_usd": _f(month) or 0.0, "month_calls": int(calls or 0),
            "last_call_at": _iso(last)}


def daily_cap(provider: Optional[str], budgets: Optional[dict], spent_today: dict) -> Optional[dict]:
    """The provider-wide daily cap of ``provider`` (spend_budget scope global) with today's spend against it; None when
    the provider has no row or the row has no daily figure. Pure."""
    if not provider or not budgets or provider not in budgets:
        return None
    b = budgets[provider]
    cap = b.get("per_day_usd")
    if cap is None:
        return None
    spent = float(spent_today.get(provider, 0.0))
    pct = _pct(spent, cap)
    return {"budget_provider": provider, "per_day_usd": float(cap), "spent_today_usd": round(spent, 4),
            "used_pct": pct, "hard_stop": bool(b.get("hard_stop")), "alert_pct": b.get("alert_pct"),
            "state": "cap_reached" if pct is not None and pct >= 100 else
                     ("alert" if pct is not None and b.get("alert_pct") and pct >= float(b["alert_pct"]) else "ok")}


def ledger_by_account(rows: list[dict]) -> dict[str, dict]:
    """{account: spend} over the ledger rows, Jev rows left out (they are the Jev row's). Pure."""
    out: dict[str, dict] = {}
    for r in rows:
        if budget_provider(r.get("provider")) == "jev" or not r.get("account"):
            continue
        acc = out.setdefault(r["account"], {"today": 0.0, "month": 0.0, "calls": 0, "last": None})
        _add(acc, r)
    return out


def ledger_by_budget_provider(rows: list[dict]) -> dict[str, dict]:
    """{spend_budget provider: spend} over the ledger rows. Pure."""
    out: dict[str, dict] = {}
    for r in rows:
        p = budget_provider(r.get("provider")) or "unknown"
        _add(out.setdefault(p, {"today": 0.0, "month": 0.0, "calls": 0, "last": None}), r)
    return out


def _add(acc: dict, r: dict) -> None:
    acc["today"] += float(r.get("today_usd") or 0.0)
    acc["month"] += float(r.get("month_usd") or 0.0)
    acc["calls"] += int(r.get("month_calls") or 0)
    last = r.get("last_call_at")
    if last is not None and (acc["last"] is None or last > acc["last"]):
        acc["last"] = last


def llm_rows(connections: list[dict], health: dict[str, dict], by_account: dict[str, dict],
             budgets_month: dict[str, dict], budgets: Optional[dict], spent_today: dict) -> list[dict]:
    """One row per LLM connection. ``connections``: {connection_key, kind, label, enabled, data_scope, account_id};
    ``health``: llm_availability.build_connections entries by key; ``budgets_month``: ConnectionBudget summaries by
    key. Pure (unit-tested)."""
    rows = []
    for c in connections:
        key = c["connection_key"]
        h = health.get(key)
        spend = by_account.get(key, {})
        status = (h or {}).get("status") or ("disabled" if not c.get("enabled") else "unknown")
        monthly = budgets_month.get(key)
        reason = None
        if status == "open":
            reason = "circuit_open"
        elif monthly and monthly.get("state") == "stopped":
            status, reason = "cap_reached", "monthly_budget_reached"
        rows.append({
            "key": f"llm:{key}", "group": "llm", "provider": c.get("kind"), "label": c.get("label") or key,
            "connection_key": key, "account_id": c.get("account_id"), "enabled": bool(c.get("enabled")),
            "data_scope": c.get("data_scope"), "status": status, "status_reason": reason,
            "health": None if h is None else {
                "last_ok_at": h.get("last_ok_at"), "last_error_at": h.get("last_error_at"),
                "last_error_class": h.get("last_error_class"), "open_models": h.get("open_models", 0),
                "models": len(h.get("models") or []),
                "failing_models": sum(1 for m in (h.get("models") or []) if m.get("consecutive_failures"))},
            "spend": _spend(spend.get("today", 0.0), spend.get("month", 0.0), spend.get("calls", 0),
                            spend.get("last")),
            "monthly_budget": monthly,
            "daily_cap": daily_cap(BUDGET_PROVIDER_OF_KIND.get(c.get("kind") or ""), budgets, spent_today),
            "balance": None, "credits": None, "error": None,
        })
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    rows.sort(key=lambda r: (not r["enabled"], order.get(r["provider"] or "", len(order)),
                             -r["spend"]["month_usd"], r["connection_key"]))
    return rows


def jev_status(estimated_left: Optional[float], threshold: float) -> tuple[str, Optional[str]]:
    if estimated_left is None:
        return "unknown", "no_topup_recorded"
    if estimated_left <= 0:
        return "cap_reached", "credit_used_up"
    if estimated_left < threshold:
        return "low", "below_threshold"
    return "ok", None


def jev_row(estimate: dict, threshold: float, spend: dict, decisions: Optional[dict], budgets: Optional[dict],
            spent_today: dict) -> dict:
    """The Jev row. ``estimate``: shared.jev_credit.estimate(); ``spend``: the ledger 'jev' entry. Pure."""
    left = estimate.get("estimated_left_usd")
    status, reason = jev_status(left, threshold)
    health = None
    if decisions is not None:
        calls = int(decisions.get("calls") or 0)
        errors = int(decisions.get("errors") or 0)
        health = {"window_hours": DECISION_WINDOW_HOURS, "calls": calls, "errors": errors,
                  "error_pct": _pct(errors, calls), "cached": int(decisions.get("cached") or 0),
                  "mean_latency_ms": None if decisions.get("mean_latency_ms") is None
                  else round(float(decisions["mean_latency_ms"])),
                  "last_at": _iso(decisions.get("last_at"))}
        if status == "ok" and calls and errors * 2 > calls:
            status, reason = "degraded", "most_calls_failed"
    return {
        "key": "jev", "group": "decide", "provider": "typesafe", "label": "Jev (TypeSafe decisions)",
        "connection_key": None, "account_id": None, "enabled": True, "data_scope": "vendor_direct",
        "status": status, "status_reason": reason, "health": health,
        "spend": _spend(spend.get("today", 0.0), spend.get("month", 0.0), spend.get("calls", 0), spend.get("last")),
        "monthly_budget": None, "daily_cap": daily_cap("jev", budgets, spent_today),
        "balance": {"usd": left, "estimated": True, "threshold_usd": threshold,
                    "below_threshold": left is not None and left < threshold,
                    "as_of": None, "source": "topups_minus_logged_spend",
                    "topped_up_usd": estimate.get("total_topped_up_usd"),
                    "since": estimate.get("first_topup_on")},
        "credits": None, "error": None,
    }


def dfs_row(balance: Optional[dict], threshold: float, spend: dict, budgets: Optional[dict],
            spent_today: dict) -> dict:
    """The DataForSEO row. ``balance``: the newest snapshot row or None. Pure."""
    usd = None if not balance else balance.get("balance_usd")
    below = usd is not None and float(usd) < threshold
    status, reason = ("unknown", "no_balance_snapshot") if usd is None else (
        ("low", "below_threshold") if below else ("ok", None))
    return {
        "key": "dataforseo", "group": "data", "provider": "dataforseo", "label": "DataForSEO",
        "connection_key": None, "account_id": None, "enabled": True, "data_scope": "vendor_direct",
        "status": status, "status_reason": reason, "health": None,
        "spend": _spend(spend.get("today_usd", 0.0), spend.get("month_usd", 0.0), spend.get("month_calls", 0),
                        spend.get("last_call_at")),
        "monthly_budget": None, "daily_cap": daily_cap("dfs", budgets, spent_today),
        "balance": {"usd": None if usd is None else round(float(usd), 4), "estimated": False,
                    "threshold_usd": threshold, "below_threshold": below,
                    "as_of": _iso((balance or {}).get("fetched_at")), "source": "daily_balance_check",
                    "currency": (balance or {}).get("currency")},
        "credits": None, "error": None,
        "live_calls_month": int(spend.get("month_live_calls") or 0),
    }


def geo_row(configured: bool, geo: dict, daily_limit: int, budgets: Optional[dict], spent_today: dict) -> dict:
    """The Geoapify row. ``configured``: the place pipeline's geocoder is Geoapify (GEOCODER_PROVIDER). Pure."""
    used = int(geo.get("credits_today") or 0)
    pct = _pct(used, daily_limit)
    if not configured:
        status, reason = "disabled", "not_the_configured_geocoder"
    elif pct is not None and pct >= 100:
        status, reason = "cap_reached", "daily_credits_used"
    elif pct is not None and pct >= WARN_PCT:
        status, reason = "low", "daily_credits_low"
    else:
        status, reason = "ok", None
    return {
        "key": "geoapify", "group": "data", "provider": "geoapify", "label": "Geoapify (geocoder)",
        "connection_key": None, "account_id": None, "enabled": configured, "data_scope": "vendor_direct",
        "status": status, "status_reason": reason, "health": None,
        "spend": _spend(geo.get("today_usd", 0.0), geo.get("month_usd", 0.0), geo.get("month_requests", 0),
                        geo.get("last_call_at")),
        "monthly_budget": None, "daily_cap": daily_cap("geocoder", budgets, spent_today),
        "balance": None,
        "credits": {"used_today": used, "daily_limit": daily_limit, "used_pct": pct, "window": "UTC day"},
        "error": None,
    }


def failed_row(key: str, group: str, provider: str, label: str, error: str) -> dict:
    return {"key": key, "group": group, "provider": provider, "label": label, "connection_key": None,
            "account_id": None, "enabled": True, "data_scope": None, "status": "unknown",
            "status_reason": "source_unreadable", "health": None, "spend": None, "monthly_budget": None,
            "daily_cap": None, "balance": None, "credits": None, "error": error}


async def _fetch(pool, sql: str, *args) -> list[dict]:
    return [dict(r) for r in await pool.fetch(sql, *args)]


async def _fetchrow(pool, sql: str, *args) -> Optional[dict]:
    r = await pool.fetchrow(sql, *args)
    return dict(r) if r is not None else None


async def _read_connections(pool, now: datetime) -> dict:
    """The connection registry, its health and its monthly budgets (one pooled connection)."""
    from api.services import llm_availability
    from shared import cost_guard
    from shared.llm_client import connections as conn_registry

    async with pool.acquire() as conn:
        registry = await conn_registry.fetch_connections(conn)
        _, health = await llm_availability.read_connections(conn, now=now)
        monthly = await cost_guard.connection_budgets(
            conn, {k: c.monthly_budget_usd for k, c in registry.items()})
    rows = [{"connection_key": c.connection_key, "kind": c.kind, "label": c.label, "enabled": c.enabled,
             "data_scope": c.data_scope,
             "account_id": c.resolved_account_id() if c.kind == "bedrock" else None}
            for c in registry.values()]
    return {"connections": rows, "health": {h["connection_key"]: h for h in health},
            "monthly": {k: b.summary() for k, b in monthly.items()}}


async def _read_budgets(pool) -> dict:
    return {r["provider"]: r for r in await _fetch(pool, BUDGETS_SQL)}


async def _read_jev(pool, now: datetime) -> dict:
    from shared import jev_credit

    estimate = await jev_credit.estimate(pool)
    decisions = await _fetchrow(pool, DECISIONS_SQL, now - timedelta(hours=DECISION_WINDOW_HOURS), now)
    return {"estimate": estimate, "decisions": decisions, "threshold": jev_credit.LOW_CREDIT_THRESHOLD_USD}


def dfs_threshold() -> float:
    from api.routers.admin_llm_ops import _DFS_BALANCE_ALERT_THRESHOLD_USD

    return float(_DFS_BALANCE_ALERT_THRESHOLD_USD)


def geo_settings() -> tuple[bool, int]:
    """(Geoapify is the configured geocoder, its daily credit limit) — the values the geocoder itself enforces."""
    from services.places import geocoder

    limit = int(geocoder._env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", geocoder.DEFAULT_DAILY_CREDITS))
    return geocoder.provider_name() == "geoapify", limit


async def read_providers(pool, *, now: Optional[datetime] = None) -> dict:
    """``GET /admin/ai-gateway/providers`` (see the module docstring)."""
    now = now or datetime.now(timezone.utc)
    day, month = day_and_month_start(now)
    results = await asyncio.gather(
        _guard("LLM connections", lambda: _read_connections(pool, now)),
        _guard("LLM ledger", lambda: _fetch(pool, LEDGER_SQL, day, month, now)),
        _guard("Spend budgets", lambda: _read_budgets(pool)),
        _guard("Jev credit", lambda: _read_jev(pool, now)),
        _guard("DataForSEO", lambda: asyncio.gather(_fetchrow(pool, DFS_BALANCE_SQL),
                                                    _fetchrow(pool, DFS_SPEND_SQL, day, month, now))),
        _guard("Geoapify", lambda: _fetchrow(pool, GEO_SQL, day, month, now, "geoapify")),
    )
    (conns, conns_err), (ledger, ledger_err), (budgets, budgets_err), (jev, jev_err), (dfs, dfs_err), \
        (geo, geo_err) = results
    errors = [{"source": s, "error": e} for s, e in (
        ("llm_connections", conns_err), ("llm_ledger", ledger_err), ("spend_budgets", budgets_err),
        ("jev", jev_err), ("dataforseo", dfs_err), ("geoapify", geo_err)) if e]

    ledger = ledger or []
    by_account = ledger_by_account(ledger)
    by_provider = ledger_by_budget_provider(ledger)
    dfs_balance, dfs_spend = dfs if dfs else (None, None)
    spent_today = {p: v["today"] for p, v in by_provider.items()}
    if dfs_spend:
        spent_today["dfs"] = float(dfs_spend.get("today_usd") or 0.0)
    if geo:
        spent_today["geocoder"] = float(geo.get("today_usd") or 0.0)

    providers: list[dict] = []
    if conns is not None:
        providers += llm_rows(conns["connections"], conns["health"], by_account if ledger_err is None else {},
                              conns["monthly"], budgets, spent_today)
        if ledger_err:
            for r in providers:
                r["spend"] = None
                r["error"] = ledger_err
    else:
        providers.append(failed_row("llm", "llm", "llm", "LLM connections", conns_err))
    if jev is not None:
        row = jev_row(jev["estimate"], jev["threshold"], by_provider.get("jev", {}), jev["decisions"], budgets,
                      spent_today)
        if ledger_err:
            row["spend"], row["error"] = None, ledger_err
        providers.append(row)
    else:
        providers.append(failed_row("jev", "decide", "typesafe", "Jev (TypeSafe decisions)", jev_err))
    if dfs is not None:
        providers.append(dfs_row(dfs_balance, dfs_threshold(), dfs_spend or {}, budgets, spent_today))
    else:
        providers.append(failed_row("dataforseo", "data", "dataforseo", "DataForSEO", dfs_err))
    if geo is not None:
        configured, limit = geo_settings()
        providers.append(geo_row(configured, geo, limit, budgets, spent_today))
    else:
        providers.append(failed_row("geoapify", "data", "geoapify", "Geoapify (geocoder)", geo_err))

    # LLM spend whose account names no connection (older rows from before the S6 connection keys, an unknown key):
    # shown apart so the page's totals match the ledger.
    known = {c["connection_key"] for c in (conns or {}).get("connections", [])}
    other = {"today": 0.0, "month": 0.0, "calls": 0, "last": None}
    for r in ledger:
        if budget_provider(r.get("provider")) != "jev" and r.get("account") not in known:
            _add(other, r)
    unattributed = None if conns is None or ledger_err else _spend(other["today"], other["month"], other["calls"],
                                                                   other["last"])
    spends = [p["spend"] for p in providers if p.get("spend")] + ([unattributed] if unattributed else [])
    return {
        "generated_at": now.isoformat(), "day_start": day.isoformat(), "month_start": month.isoformat(),
        "providers": providers,
        "unattributed_llm": unattributed,
        "totals": {"today_usd": round(sum(s["today_usd"] for s in spends), 4),
                   "month_usd": round(sum(s["month_usd"] for s in spends), 4)},
        "errors": errors,
    }


# ── trace ───────────────────────────────────────────────────────────────────────────────────────────────────────────

TRACE_LIMIT = 500
TRACE_WINDOW_SLACK = timedelta(minutes=1)
ERROR_CHARS = 300
SOURCES = ("llm", "jev", "dfs", "geocoder")

TRACE_JOB_SQL = """
    SELECT id::text AS id, kind, status, created_at, started_at, finished_at, cost_usd::float AS cost_usd,
           payload ->> 'tour_id' AS tour_id, parent_job_id::text AS parent_job_id
      FROM shared.job
     WHERE id = $1::uuid
"""

# count(*) / sum() OVER () are taken before LIMIT, so the totals cover every row of the job.
TRACE_LLM_SQL = """
    SELECT created_at AS at, stage, role, model, provider, account, tokens_in, tokens_out, cache_read_tokens,
           cache_write_tokens, cost_usd::float AS cost_usd, stop_reason, fallback_used, pricing_mode,
           coalesce(quality_signal ->> 'shadow', '') = 'true' AS shadow,
           count(*) OVER () AS total_n, coalesce(sum(cost_usd) OVER (), 0)::float AS total_cost
      FROM shared.llm_call_log
     WHERE job_id = $1::uuid
     ORDER BY created_at, id
     LIMIT $2
"""

TRACE_DFS_SQL = """
    SELECT created_at AS at, endpoint, keyword, location_code, keyword_count, cost_usd::float AS cost_usd, cache_hit,
           fetched_live, tour_id::text AS tour_id,
           count(*) OVER () AS total_n, coalesce(sum(cost_usd) OVER (), 0)::float AS total_cost
      FROM shared.dfs_call_log
     WHERE job_id = $1::uuid
     ORDER BY created_at, id
     LIMIT $2
"""

# No job_id index on these two: the run window bounds the scan (geocode_call_called_at_idx /
# decision_log_created_id_idx).
TRACE_GEO_SQL = """
    SELECT called_at AS at, provider, endpoint, requests, credits, cost_usd::float AS cost_usd,
           count(*) OVER () AS total_n, coalesce(sum(cost_usd) OVER (), 0)::float AS total_cost
      FROM geo.geocode_call
     WHERE called_at >= $2 AND called_at <= $3 AND job_id = $1::uuid
     ORDER BY called_at, id
     LIMIT $4
"""

TRACE_DECISION_SQL = """
    SELECT created_at AS at, stage, question_key, subject_key, mode, zone, model, latency_ms,
           cost_usd::float AS cost_usd, error, outcome, cached,
           count(*) OVER () AS total_n, coalesce(sum(cost_usd) OVER (), 0)::float AS total_cost
      FROM shared.decision_log
     WHERE created_at >= $2 AND created_at <= $3 AND job_id = $1::uuid
     ORDER BY created_at, id
     LIMIT $4
"""


def parse_job_id(value: str) -> str:
    """The canonical UUID text, or ValueError."""
    return str(uuid.UUID(str(value).strip()))


def trace_window(job: dict, now: datetime) -> Optional[tuple[datetime, datetime]]:
    """The run window that bounds the reads without a job index: started_at .. (finished_at or now) + slack; None
    for a job that never started (it made no call). Pure."""
    start = job.get("started_at")
    if start is None:
        return None
    end = job.get("finished_at") or now
    return start, end + TRACE_WINDOW_SLACK


def llm_event(r: dict) -> dict:
    outcome = "shadow" if r.get("shadow") else ("fallback" if r.get("fallback_used") else "ok")
    return {"at": _iso(r["at"]), "source": "llm", "stage": r.get("stage"), "what": r.get("role"),
            "model": r.get("model"), "provider": r.get("provider"), "account": r.get("account"),
            "outcome": outcome, "cost_usd": _f(r.get("cost_usd")), "latency_ms": None,
            "tokens_in": r.get("tokens_in"), "tokens_out": r.get("tokens_out"),
            "detail": {"stop_reason": r.get("stop_reason"), "pricing_mode": r.get("pricing_mode"),
                       "cache_read_tokens": r.get("cache_read_tokens"),
                       "cache_write_tokens": r.get("cache_write_tokens")}}


def jev_event(r: dict) -> dict:
    error = r.get("error")
    return {"at": _iso(r["at"]), "source": "jev", "stage": r.get("stage"), "what": r.get("question_key"),
            "model": r.get("model"), "provider": "typesafe", "account": None,
            "outcome": "error" if error else r.get("zone"), "cost_usd": _f(r.get("cost_usd")),
            "latency_ms": r.get("latency_ms"), "tokens_in": None, "tokens_out": None,
            "detail": {"mode": r.get("mode"), "subject_key": r.get("subject_key"), "cached": r.get("cached"),
                       "result": r.get("outcome"), "error": (error or "")[:ERROR_CHARS] or None}}


def dfs_event(r: dict) -> dict:
    return {"at": _iso(r["at"]), "source": "dfs", "stage": None, "what": r.get("endpoint"), "model": None,
            "provider": "dataforseo", "account": None, "outcome": "cache" if r.get("cache_hit") else "live",
            "cost_usd": _f(r.get("cost_usd")), "latency_ms": None, "tokens_in": None, "tokens_out": None,
            "detail": {"keyword": r.get("keyword"), "location_code": r.get("location_code"),
                       "keyword_count": r.get("keyword_count"), "tour_id": r.get("tour_id")}}


def geo_event(r: dict) -> dict:
    return {"at": _iso(r["at"]), "source": "geocoder", "stage": None, "what": r.get("endpoint"), "model": None,
            "provider": r.get("provider"), "account": None, "outcome": "ok", "cost_usd": _f(r.get("cost_usd")),
            "latency_ms": None, "tokens_in": None, "tokens_out": None,
            "detail": {"requests": r.get("requests"), "credits": r.get("credits")}}


EVENT_OF = {"llm": llm_event, "jev": jev_event, "dfs": dfs_event, "geocoder": geo_event}


def source_summary(rows: Optional[list[dict]], error: Optional[str], *, read: bool = True) -> dict:
    """{events (all matching), shown, cost_usd, truncated, read, error} of one ledger. Pure."""
    rows = rows or []
    total = int(rows[0]["total_n"]) if rows else 0
    return {"events": total, "shown": len(rows), "cost_usd": _f(rows[0]["total_cost"]) if rows else 0.0,
            "truncated": total > len(rows), "read": read and error is None, "error": error}


def build_trace(job: dict, window: Optional[tuple[datetime, datetime]], by_source: dict[str, tuple],
                limit: int) -> dict:
    """The trace answer from the job row and each source's (rows, error). Pure (unit-tested)."""
    events: list[dict] = []
    sources = {}
    for s in SOURCES:
        rows, error = by_source.get(s, (None, None))
        read = not (s in ("jev", "geocoder") and window is None)
        sources[s] = source_summary(rows, error, read=read)
        events += [EVENT_OF[s](r) for r in rows or []]
    events.sort(key=lambda e: (e["at"] or "", e["source"]))
    notes = ["refused_calls_not_logged"]
    if window is None:
        notes.append("job_not_started")
    if any(v["truncated"] for v in sources.values()):
        notes.append("truncated")
    return {
        "job": {k: _iso(job.get(k)) for k in ("id", "kind", "status", "created_at", "started_at", "finished_at",
                                              "cost_usd", "tour_id", "parent_job_id")},
        "window": None if window is None else {"from": window[0].isoformat(), "to": window[1].isoformat()},
        "events": events,
        "sources": sources,
        "totals": {"events": sum(v["events"] for v in sources.values()),
                   "cost_usd": round(sum(v["cost_usd"] or 0.0 for v in sources.values()), 6)},
        "limit_per_source": limit,
        "notes": notes,
    }


async def trace_job(pool, job_id: str, *, limit: int = TRACE_LIMIT, now: Optional[datetime] = None) -> Optional[dict]:
    """``GET /admin/ai-gateway/trace?job_id=`` (see the module docstring); None when the job does not exist."""
    now = now or datetime.now(timezone.utc)
    job = await _fetchrow(pool, TRACE_JOB_SQL, job_id)
    if job is None:
        return None
    window = trace_window(job, now)

    async def none() -> list[dict]:
        return []

    reads = {
        "llm": lambda: _fetch(pool, TRACE_LLM_SQL, job_id, limit),
        "dfs": lambda: _fetch(pool, TRACE_DFS_SQL, job_id, limit),
        "geocoder": (lambda: _fetch(pool, TRACE_GEO_SQL, job_id, window[0], window[1], limit)) if window else none,
        "jev": (lambda: _fetch(pool, TRACE_DECISION_SQL, job_id, window[0], window[1], limit)) if window else none,
    }
    labels = {"llm": "LLM call log", "dfs": "DataForSEO call log", "geocoder": "Geocoder call log",
              "jev": "Jev decision log"}
    results = await asyncio.gather(*(_guard(labels[s], reads[s]) for s in SOURCES))
    return build_trace(job, window, dict(zip(SOURCES, results)), limit)
