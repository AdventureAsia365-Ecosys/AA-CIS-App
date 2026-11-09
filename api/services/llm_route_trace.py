"""AA-1042 (S268, AI gateway route control center: Route trace) — the reads of AI gateway > Trace, the route alert
policy and the route test.

Every attempt row the gateway writes for one request (refused / failed attempts, billed unusable replies) and the
caller's answered row share ``quality_signal.route_trace`` (``call_log.ROUTE_KEYS``); the answered row also carries
``route_position`` and every row its ``attempt_no`` (ledger v2). From those:

- ``read_requests``  the newest requests of a window (one line per route trace): attempts, the model and connection
                     that answered (or none), cost, latency; ``fallback_only`` keeps the requests that needed more than
                     one attempt or got no answer.
- ``read_trace``     one request: every attempt in order with its outcome, error class and text, latency and cost. A
                     trace is looked up within +-``TRACE_SLACK`` of a time the caller gives (the Calls row's time), or
                     the last ``TRACE_DEFAULT_WINDOW`` (the ``created_at`` index, never a full scan).
- ``read_rungs``     per stage over a window: the share of answered requests per rung (model @ connection, primary or
                     fallback), the fallback share, the requests with no answer, and the stage's route.
- ``read_fallback_content``  the newest A1 tour versions a fallback model wrote (``generated_content.fallback_used``).
- ``read_fallback_tenant_content``  the same for tenant content: T2 tour versions whose admin-only
                     ``judge_breakdown`` carries ``fallback_writer`` and T9 pieces whose ``flags`` carry it, with the
                     tenant name and the held / marked label. Tenant QA data: its route
                     (/admin/content-quality/fallback-writes) is gated by tenant_content.read, not spend.read (the
                     tenant never sees these marks).
- ``run_route_test`` the live check: 1-6 tiny real requests on one stage (16 output tokens), optionally with the
                     stage's first model refused for those requests only (``client.forced_primary_refusal``: a
                     ``refused`` / ``access_denied`` row, no provider call, no breaker or circuit change). Each answered
                     request is logged with ``**billed_fields(resp)`` (its route trace included).

$0 except ``run_route_test`` (about 0.0001 USD per request on today's routes).
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from shared.llm_client import route_watch
from shared.llm_client.expected_refusal import EXPECTED_SQL
from shared.llm_client.route_watch import FALLBACK_RUNG, LEDGER_FILTER, OK, POSITION_EXPR, PRIMARY_RUNG

logger = structlog.get_logger()

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
DEFAULT_WINDOW = "24h"
REQUESTS_LIMIT = 100
TRACE_SLACK = timedelta(minutes=15)
TRACE_DEFAULT_WINDOW = timedelta(hours=48)
TRACE_ROWS_MAX = 50
ERROR_CHARS = 300
CONTENT_LIMIT = 50
TEST_MAX_REQUESTS = 6
TEST_MAX_TOKENS = 16
TEST_SYSTEM_PROMPT = "You are a connectivity check for an AI gateway. Answer with one short word."
TEST_USER_PROMPT = "Reply with the single word: ok"
TEST_ROLES = ("writer", "judge", "validate")
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the platform sentinel tenant of platform audit rows
_STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def window_of(value: Optional[str]) -> tuple[str, timedelta]:
    key = value or DEFAULT_WINDOW
    if key not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    return key, WINDOWS[key]


def parse_stage(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    if not _STAGE_RE.match(value):
        raise ValueError("stage is not a stage name")
    return value


def parse_trace_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as e:
        raise ValueError("trace id must be a UUID") from e


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _f(v: Any) -> Optional[float]:
    return None if v is None else round(float(v), 6)


# ── requests ────────────────────────────────────────────────────────────────────────────────────────────────────────

def requests_sql(*, stage: Optional[str], fallback_only: bool) -> str:
    """The request list ($1 since, [$2 stage], last $n limit). Pure."""
    stage_clause = "AND stage = $2" if stage else ""
    limit_arg = "$3" if stage else "$2"
    keep = "WHERE NOT answered OR served_fallback OR failed_attempts > 0" if fallback_only else ""
    return f"""
    WITH t AS (
        SELECT quality_signal ->> 'route_trace' AS trace, min(stage) AS stage, min(role) AS role,
               min(created_at) AS first_at, max(created_at) AS last_at, count(*) AS rows,
               count(*) FILTER (WHERE status IN ('refused', 'error') AND NOT {EXPECTED_SQL}) AS failed_attempts,
               count(*) FILTER (WHERE {EXPECTED_SQL}) AS expected_refusals,
               bool_or({OK}) AS answered, max(attempt_no) AS attempts,
               coalesce(sum(cost_usd), 0)::float AS cost_usd, sum(latency_ms) AS latency_ms,
               (array_agg(model ORDER BY created_at DESC) FILTER (WHERE {OK}))[1] AS served_model,
               (array_agg(coalesce(connection_key, account) ORDER BY created_at DESC)
                    FILTER (WHERE {OK}))[1] AS served_connection,
               max(attempt_no) FILTER (WHERE {OK}) AS served_attempt,
               coalesce(bool_or({FALLBACK_RUNG}) FILTER (WHERE {OK}), false) AS served_fallback,
               max({POSITION_EXPR}) AS max_position,
               bool_or(coalesce(quality_signal ->> 'route_test', '') = 'true') AS route_test,
               coalesce(sum(CASE WHEN jsonb_typeof(quality_signal -> 'route_skips') = 'array'
                                 THEN jsonb_array_length(quality_signal -> 'route_skips') END), 0) AS skipped_attempts,
               (array_agg(job_id::text) FILTER (WHERE job_id IS NOT NULL))[1] AS job_id
          FROM shared.llm_call_log
         WHERE created_at >= $1 AND quality_signal ? 'route_trace' AND {LEDGER_FILTER} {stage_clause}
         GROUP BY 1
    )
    SELECT * FROM t {keep}
     ORDER BY last_at DESC
     LIMIT {limit_arg}
    """


def request_view(r: dict) -> dict:
    answered = bool(r.get("answered"))
    served_attempt = r.get("served_attempt")
    outcome = ("no_answer" if not answered else
               "fallback" if r.get("served_fallback") else "primary")
    return {"trace_id": r["trace"], "stage": r.get("stage"), "role": r.get("role"),
            "first_at": _iso(r.get("first_at")), "last_at": _iso(r.get("last_at")), "outcome": outcome,
            "attempts": r.get("attempts") or r.get("rows"), "failed_attempts": r.get("failed_attempts") or 0,
            # AA-900 (S271 l11): refused attempts of a model with no grant yet (not in failed_attempts)
            "expected_refusals": int(r.get("expected_refusals") or 0),
            "served_model": r.get("served_model"), "served_connection": r.get("served_connection"),
            "served_attempt": served_attempt, "max_position": r.get("max_position"),
            "cost_usd": _f(r.get("cost_usd")), "latency_ms": r.get("latency_ms"),
            "route_test": bool(r.get("route_test")), "job_id": r.get("job_id"),
            # AA-1042 (S270): route rungs deferred behind an open refusal circuit (no row of their own)
            "skipped_attempts": int(r.get("skipped_attempts") or 0)}


async def read_requests(pool, *, stage: Optional[str] = None, window: Optional[str] = None,
                        fallback_only: bool = False, limit: int = REQUESTS_LIMIT,
                        now: Optional[datetime] = None) -> dict:
    key, span = window_of(window)
    now = now or datetime.now(timezone.utc)
    args: list[Any] = [now - span] + ([stage] if stage else []) + [max(1, min(limit, REQUESTS_LIMIT))]
    rows = await pool.fetch(requests_sql(stage=stage, fallback_only=fallback_only), *args)
    return {"window": key, "stage": stage, "fallback_only": fallback_only,
            "requests": [request_view(dict(r)) for r in rows]}


# ── one trace ───────────────────────────────────────────────────────────────────────────────────────────────────────

TRACE_SQL = f"""
    SELECT created_at, stage, role, model, provider, account, connection_key, deployment_wire_id, status,
           error_class, attempt_no, latency_ms, cost_usd::float AS cost_usd, tokens_in, tokens_out, fallback_used,
           job_id::text AS job_id, tour_id::text AS tour_id, tenant_id::text AS tenant_id,
           {POSITION_EXPR} AS position, left(quality_signal ->> 'error', {ERROR_CHARS}) AS error,
           coalesce(quality_signal ->> 'route_test', '') = 'true' AS route_test,
           coalesce(quality_signal ->> 'billed_unusable', '') = 'true' AS billed_unusable,
           CASE WHEN jsonb_typeof(quality_signal -> 'route_skips') = 'array'
                THEN quality_signal -> 'route_skips' END AS route_skips
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at <= $2 AND quality_signal ->> 'route_trace' = $3
     ORDER BY coalesce({POSITION_EXPR}, 1000), coalesce(attempt_no, 1000), created_at
     LIMIT {TRACE_ROWS_MAX}
"""


def attempt_view(r: dict) -> dict:
    status = r.get("status") or "ok"
    return {"at": _iso(r.get("created_at")), "attempt_no": r.get("attempt_no"), "position": r.get("position"),
            "model": r.get("model"), "provider": r.get("provider"),
            "connection": r.get("connection_key") or r.get("account"), "wire_id": r.get("deployment_wire_id"),
            "status": status, "error_class": r.get("error_class"), "error": r.get("error"),
            "latency_ms": r.get("latency_ms"), "cost_usd": _f(r.get("cost_usd")), "tokens_in": r.get("tokens_in"),
            "tokens_out": r.get("tokens_out"), "billed_unusable": bool(r.get("billed_unusable"))}


SKIPPED = "skipped"


def _route_skips_of(r: dict) -> list:
    """The route_skips entries of one row (jsonb: a list, or its JSON text from a pool without a jsonb codec)."""
    value = r.get("route_skips")
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def skipped_attempt_views(r: dict) -> list[dict]:
    """AA-1042 (S270): the route rungs a row says were deferred behind an open refusal circuit (its
    ``quality_signal.route_skips``), as attempts with status ``skipped``: no provider call, 0 USD, no latency, at the
    rung's own route position."""
    out = []
    for e in _route_skips_of(r):
        pos = e.get("position")
        reason = e.get("reason") if isinstance(e.get("reason"), str) else None
        cls = reason.split(":", 1)[1] if reason and reason.startswith("circuit_open:") else None
        out.append({"at": _iso(r.get("created_at")), "attempt_no": None,
                    "position": pos if isinstance(pos, int) and not isinstance(pos, bool) else None,
                    "model": e.get("model"), "provider": None, "connection": e.get("connection"), "wire_id": None,
                    "status": SKIPPED, "error_class": cls, "error": reason, "latency_ms": None,
                    "cost_usd": 0.0, "tokens_in": None, "tokens_out": None, "billed_unusable": False})
    return out


def _attempt_order(a: dict) -> tuple:
    pos = a.get("position")
    return (pos if pos is not None else 1000, a.get("attempt_no") if a.get("attempt_no") is not None else 1000)


def trace_view(trace_id: str, rows: list[dict], route: Optional[dict]) -> Optional[dict]:
    """The Trace drawer of one request (None: no row). Pure. AA-1042 (S270): the rungs a row lists in route_skips
    (deferred behind an open refusal circuit, no row of their own) are attempts too, status ``skipped``, in route
    position order."""
    if not rows:
        return None
    attempts = logged = [attempt_view(r) for r in rows]
    answered = [(a, r) for a, r in zip(attempts, rows) if a["status"] == "ok"]
    served, served_row = answered[-1] if answered else (None, None)
    skipped = [s for r in rows for s in skipped_attempt_views(r)]
    if skipped:
        attempts = sorted(attempts + skipped, key=_attempt_order)     # stable: the SQL order within one position
    first = rows[0]
    return {"trace_id": trace_id, "stage": first.get("stage"), "role": first.get("role"),
            "job_id": next((r.get("job_id") for r in rows if r.get("job_id")), None),
            "tour_id": next((r.get("tour_id") for r in rows if r.get("tour_id")), None),
            "route_test": any(r.get("route_test") for r in rows),
            "outcome": ("no_answer" if served_row is None else
                        "fallback" if route_watch.is_fallback(served_row.get("attempt_no"), served_row.get("position"),
                                                              served_row.get("fallback_used")) else "primary"),
            "served": served, "attempts": attempts,
            "cost_usd": round(sum(a["cost_usd"] or 0.0 for a in attempts), 6),
            "latency_ms": sum(a["latency_ms"] or 0 for a in attempts),
            "route": route_watch.chain_of(route) if route else None,
            "first_at": logged[0]["at"], "last_at": logged[-1]["at"]}


async def read_trace(pool, trace_id: str, *, at: Optional[datetime] = None,
                     now: Optional[datetime] = None) -> Optional[dict]:
    now = now or datetime.now(timezone.utc)
    if at is not None:
        since, until = at - TRACE_SLACK, at + TRACE_SLACK
    else:
        since, until = now - TRACE_DEFAULT_WINDOW, now + TRACE_SLACK
    async with pool.acquire() as db:
        rows = [dict(r) for r in await db.fetch(TRACE_SQL, since, until, trace_id)]
        routes = await route_watch.read_routes(db) if rows else {}
    return trace_view(trace_id, rows, routes.get(rows[0]["stage"]) if rows else None)


# ── rungs ───────────────────────────────────────────────────────────────────────────────────────────────────────────

RUNGS_SQL = f"""
    SELECT stage, model, coalesce(connection_key, account) AS connection,
           CASE WHEN {FALLBACK_RUNG} THEN 'fallback' WHEN {PRIMARY_RUNG} THEN 'primary' ELSE 'unknown' END AS rung,
           min({POSITION_EXPR}) AS position, count(*) AS answered, coalesce(sum(cost_usd), 0)::float AS cost_usd,
           avg(latency_ms)::float AS avg_latency_ms
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND {LEDGER_FILTER} AND {OK}
     GROUP BY 1, 2, 3, 4
"""
_RUNG_ORDER = {"primary": 0, "fallback": 1, "unknown": 2}


def rungs_view(rung_rows: list[dict], stats: dict[str, dict], routes: dict[str, dict],
               stage: Optional[str] = None) -> list[dict]:
    """Per stage: totals and the share of each rung. Pure."""
    by_stage: dict[str, list[dict]] = {}
    for r in rung_rows:
        by_stage.setdefault(r["stage"], []).append(r)
    out = []
    for name in sorted(set(by_stage) | set(stats)):
        if stage and name != stage:
            continue
        st = stats.get(name) or {}
        rows = by_stage.get(name, [])
        total = sum(int(r["answered"]) for r in rows)
        route = routes.get(name)
        rungs = [{"model": r["model"], "connection": r.get("connection"), "rung": r["rung"],
                  "position": r.get("position"), "answered": int(r["answered"]),
                  "share_pct": round(100.0 * int(r["answered"]) / total, 1) if total else None,
                  "cost_usd": _f(r.get("cost_usd")),
                  "avg_latency_ms": None if r.get("avg_latency_ms") is None else int(r["avg_latency_ms"])}
                 for r in rows]
        rungs.sort(key=lambda x: (_RUNG_ORDER.get(x["rung"], 9), x["position"] if x["position"] is not None else 99,
                                  -x["answered"], x["model"] or ""))
        out.append({"stage": name, "role": route["role"] if route else None,
                    "route": route_watch.chain_of(route) if route else [],
                    "active": bool(route["is_active"]) if route else None,
                    "answered": total, "fallback_answered": int(st.get("fallback_answered") or 0),
                    "fallback_pct": route_watch.fallback_rate(st), "lost_requests": int(st.get("lost") or 0),
                    "failed_attempts": int(st.get("failed_attempts") or 0),
                    "expected_refusals": int(st.get("expected_refusals") or 0), "rungs": rungs})
    out.sort(key=lambda s: (-(s["fallback_pct"] or 0), -s["lost_requests"], s["stage"]))
    return out


async def read_rungs(pool, *, window: Optional[str] = None, stage: Optional[str] = None,
                     now: Optional[datetime] = None) -> dict:
    key, span = window_of(window)
    now = now or datetime.now(timezone.utc)
    since = now - span
    async with pool.acquire() as db:
        rung_rows = [dict(r) for r in await db.fetch(RUNGS_SQL, since)]
        stats = await route_watch.read_stats(db, since)
        routes = await route_watch.read_routes(db)
    return {"window": key, "stages": rungs_view(rung_rows, stats, routes, stage)}


# ── content a fallback model wrote ──────────────────────────────────────────────────────────────────────────────────

FALLBACK_TOURS_SQL = """
    SELECT gc.id::text AS id, gc.tour_id::text AS tour_id, gc.version_num, gc.created_at, gc.status::text AS status,
           rt.src_name AS tour_name, gc.metadata -> 'degraded' AS degraded
      FROM silver_aa_internal.generated_content gc
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
     WHERE gc.fallback_used AND gc.created_at >= $1
     ORDER BY gc.created_at DESC
     LIMIT $2
"""
FALLBACK_VERSIONS_SQL = """
    SELECT ttv.id::text AS id, ttv.version_number, ttv.status, ttv.created_at, t.name AS tenant_name,
           pt.aa_name AS tour_name,
           coalesce(ttv.judge_breakdown -> 'fallback_writer' ->> 'held', '') = 'true' AS held
      FROM gold_aa_internal.tenant_tour_versions ttv
      LEFT JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
     WHERE ttv.created_at >= $1 AND ttv.judge_breakdown ? 'fallback_writer'
     ORDER BY ttv.created_at DESC
     LIMIT $2
"""
FALLBACK_PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.channel, cp.status, cp.held_reason, cp.created_at, t.name AS tenant_name
      FROM acp_shared.content_piece cp
      LEFT JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
     WHERE cp.created_at >= $1 AND cp.flags::text LIKE '%fallback_writer%'
     ORDER BY cp.created_at DESC
     LIMIT $2
"""


def _held(degraded_list: Any) -> bool:
    if isinstance(degraded_list, str):
        import json
        try:
            degraded_list = json.loads(degraded_list)
        except ValueError:
            return False
    return any(isinstance(e, dict) and e.get("name") == "fallback_writer" and e.get("severity") == "hard"
               for e in (degraded_list or []))


async def _content_rows(db, sql: str, since: datetime) -> list[dict]:
    """The rows of one content list; [] when its table or a column is missing (a database without the content
    pipeline's tables, e.g. a test replay)."""
    import asyncpg
    try:
        return [dict(r) for r in await db.fetch(sql, since, CONTENT_LIMIT)]
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError) as e:
        logger.warning("llm_route_fallback_content_unreadable", error=str(e)[:200])
        return []


async def read_fallback_content(pool, *, window: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """A1 (catalog) tour versions a fallback model wrote. No tenant data (spend.read)."""
    key, span = window_of(window or "7d")
    now = now or datetime.now(timezone.utc)
    since = now - span
    async with pool.acquire() as db:
        tours = await _content_rows(db, FALLBACK_TOURS_SQL, since)
    return {"window": key,
            "tours": [{"id": t["id"], "tour_id": t["tour_id"], "tour_name": t.get("tour_name"),
                       "version": t.get("version_num"), "status": t.get("status"), "at": _iso(t.get("created_at")),
                       "held": _held(t.get("degraded"))} for t in tours]}


async def read_fallback_tenant_content(pool, *, window: Optional[str] = None,
                                       now: Optional[datetime] = None) -> dict:
    """T2 tour versions and T9 pieces a fallback model wrote, every tenant (Tenant QA data: tenant_content.read)."""
    key, span = window_of(window or "7d")
    now = now or datetime.now(timezone.utc)
    since = now - span
    async with pool.acquire() as db:
        versions = await _content_rows(db, FALLBACK_VERSIONS_SQL, since)
        pieces = await _content_rows(db, FALLBACK_PIECES_SQL, since)
    return {"window": key,
            "versions": [{"id": v["id"], "tenant_name": v.get("tenant_name"), "tour_name": v.get("tour_name"),
                          "version": v.get("version_number"), "status": v.get("status"),
                          "held": bool(v.get("held")), "at": _iso(v.get("created_at"))} for v in versions],
            "pieces": [{"piece_id": p["piece_id"], "tenant_name": p.get("tenant_name"), "channel": p.get("channel"),
                        "status": p.get("status"), "held": p.get("status") == "held"
                        and "fallback_writer" in (p.get("held_reason") or ""), "at": _iso(p.get("created_at"))}
                       for p in pieces]}


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def read_policy(pool) -> dict:
    async with pool.acquire() as db:
        ready, rows = await route_watch.read_policies(db)
        routes = await route_watch.read_routes(db)
    stages = sorted(routes)
    return {"ready": ready, "defaults": asdict(route_watch.DEFAULTS),
            "default_row": rows.get(route_watch.DEFAULT_STAGE),
            "stages": route_watch.policy_view(rows, stages)}


# ── route test (the live check) ─────────────────────────────────────────────────────────────────────────────────────

class RouteTestRefused(ValueError):
    """The stage cannot be tested (unknown, switched off, not a chat role, a one-model route with a refusal)."""


def _test_one(stage: str, refuse_primary: bool):
    from shared.llm_client.client import LLMClient, forced_primary_refusal
    from shared.llm_client.models import LLMRequest

    request = LLMRequest(system_prompt=TEST_SYSTEM_PROMPT, user_prompt=TEST_USER_PROMPT, max_tokens=TEST_MAX_TOKENS,
                         temperature=0.0, stage=stage)
    client = LLMClient()
    if refuse_primary:
        with forced_primary_refusal(stage):
            return client.generate(request)
    return client.generate(request)


async def run_route_test(pool, *, stage: str, refuse_primary: bool, count: int, actor: str) -> dict:
    """``count`` tiny requests on ``stage`` (sequential). Each answered one is logged (``**billed_fields``, its
    route trace included, ``quality_signal.route_test``); a failed one is in the ledger through the gateway's own
    attempt rows. Returns one line per request."""
    from shared.llm_client.call_log import billed_fields, record_call_with_pool

    count = max(1, min(int(count), TEST_MAX_REQUESTS))
    async with pool.acquire() as db:
        routes = await route_watch.read_routes(db)
    route = routes.get(stage)
    if route is None:
        raise RouteTestRefused(f"no route for stage {stage!r}")
    if not route["is_active"]:
        raise RouteTestRefused(f"stage {stage!r} is switched off")
    if route["role"] not in TEST_ROLES:
        raise RouteTestRefused(f"stage {stage!r} has role {route['role']!r}: only chat stages can be tested")
    if refuse_primary and len(route_watch.chain_of(route)) < 2:
        raise RouteTestRefused(f"stage {stage!r} has no fallback model: a refused primary would fail the request")
    out = []
    for i in range(count):
        try:
            resp = await asyncio.to_thread(_test_one, stage, refuse_primary)
        except Exception as e:  # noqa: BLE001 — the gateway already wrote its attempt rows
            out.append({"n": i + 1, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        await record_call_with_pool(
            pool, stage=stage, role=route["role"], model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
            quality_signal={"route_test": True, "forced_primary_refusal": refuse_primary, "requested_by": actor[:64]},
            stop_reason=resp.stop_reason, account=resp.satellite_account, fallback_used=resp.fallback_used,
            provider=resp.provider, **billed_fields(resp))
        out.append({"n": i + 1, "ok": True, "trace_id": resp.route_trace, "model": resp.model_used,
                    "connection": resp.connection_key or resp.satellite_account, "attempt_no": resp.attempt_no,
                    "route_position": resp.route_position, "fallback_used": bool(resp.fallback_used),
                    "cost_usd": _f(resp.cost_usd)})
    logger.info("llm_route_test_done", stage=stage, refuse_primary=refuse_primary, count=count, actor=actor,
                ok=sum(1 for r in out if r["ok"]))
    return {"stage": stage, "refuse_primary": refuse_primary, "route": route_watch.chain_of(route),
            "requests": out, "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in out), 6)}


class PolicyUnavailable(RuntimeError):
    """shared.llm_route_policy does not exist yet (api/migrations/*_llm_route_policy.sql not applied)."""


POLICY_MISSING = "the route alert policy needs the llm_route_policy migration"


async def write_policy(pool, stage: str, fields: dict, *, actor: str) -> dict:
    """Create or replace one policy row (``*`` = the defaults), audited ``llm_route_policy_set``. ValueError on a bad
    value; PolicyUnavailable before the llm_route_policy migration."""
    import asyncpg

    from services.acp_shared.audit_log import write_audit_log

    if stage != route_watch.DEFAULT_STAGE:
        parse_stage(stage)
    values = route_watch.validate_policy(fields)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(route_watch.UPSERT_SQL, stage, values["fallback_alert_pct"],
                                          values["min_requests"], values["alert_last_rung"], values["cost_shift_usd"],
                                          values["hold_fallback_writes"], values["notes"], actor[:128])
                await write_audit_log(conn, tenant_id=PLATFORM_TENANT_ID, actor=actor,
                                      action="llm_route_policy_set", resource_type="llm_route_policy",
                                      resource_id=stage,
                                      details={k: v for k, v in values.items() if k != "notes"})
    except asyncpg.exceptions.UndefinedTableError as e:
        raise PolicyUnavailable(POLICY_MISSING) from e
    route_watch.reset()
    return route_watch.row_of(row)


async def clear_policy(pool, stage: str, *, actor: str) -> dict:
    """Remove one policy row (back to the ``*`` row / the defaults), audited ``llm_route_policy_cleared``.
    LookupError when there is none."""
    import asyncpg

    from services.acp_shared.audit_log import write_audit_log

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                gone = await conn.fetchval(route_watch.DELETE_SQL, stage)
                if gone is None:
                    raise LookupError(stage)
                await write_audit_log(conn, tenant_id=PLATFORM_TENANT_ID, actor=actor,
                                      action="llm_route_policy_cleared", resource_type="llm_route_policy",
                                      resource_id=stage, details={})
    except asyncpg.exceptions.UndefinedTableError as e:
        raise PolicyUnavailable(POLICY_MISSING) from e
    route_watch.reset()
    return {"stage": stage, "cleared": True}
