"""AA-1042 (S268, AI gateway route control center: Alerts + Quality guard) — how the stage routes behaved, from the
ledger the gateway already writes, and the route alerts.

Tech lead decision S267: "every fallback is logged, traced and alerted". The gateway tags every attempt row of one
request with the same id (``quality_signal.route_trace``; the answered row also carries ``route_position``, the
route position of the model that answered, see ``call_log.ROUTE_KEYS``). This module reads those rows ($0, database
only) and decides the alerts; the job kind ``llm_route_watch`` (services/jobs/llm_route_watch_job.py) raises them
through the ops-check plumbing (``shared/ops_checks.raise_alert``: admin bell + at most one e-mail per check and UTC
day).

A request's **rung** (``FALLBACK_RUNG`` / ``PRIMARY_RUNG`` / ``UNKNOWN_RUNG``, one rule for every read here and in
api/services/llm_route_trace.py): an answered row is a FALLBACK answer when its route position is > 0 (a later model,
also when the earlier ones were skipped without an attempt: open circuit, disabled catalog row, budget, role not
served), its ``attempt_no`` is > 1 (a later deployment of the first model) or its ``fallback_used`` is set; it is the
PRIMARY answer when ``attempt_no`` is 1 and none of those holds. The attempt number alone is not enough: it counts only
the calls that reached a provider, so a fallback model answering after a skipped primary has ``attempt_no`` 1. Rows
written before ledger v2 with no attempt number and no fallback mark count as ``unknown``. Shadow, eval and Jev
(``typesafe``) rows are left out.

Alerts per stage (thresholds: ``shared.llm_route_policy``, api/migrations/*_llm_route_policy.sql; the ``*`` row and
then ``DEFAULTS`` fill a missing value):

- ``route_fb.<stage>`` — at least ``fallback_alert_pct`` % of the answered requests of the last ``WATCH_WINDOW`` came
  from a fallback rung, with at least ``min_requests`` answered. Bell at most hourly.
- ``route_last.<stage>`` — a request of the window reached the LAST model of the stage's route (route position >=
  the route length - 1; a one-model route never alerts). ``alert_last_rung``. Bell at most hourly.
- ``route_back.<stage>`` — the primary is back: the stage is degraded (its newest fallback / last-model bell of the
  last ``STATE_LOOKBACK`` is newer than its newest primary-back bell) and the primary rung answered after that bell
  and after the newest fallback answer. No state table: the bells are the state.
- ``route_cost.<stage>`` — over ``COST_WINDOW`` the fallback-answered requests cost at least ``cost_shift_usd`` more
  than the same number of primary answers would have (the stage's primary average over ``BASELINE_WINDOW``, from at
  least ``BASELINE_MIN`` rows). Bell at most daily.
- ``route_cut.<stage>`` — S271: more than ``CUT_ALERT_PCT`` % of the stage's replies of the last ``COST_WINDOW`` were
  cut at their token ceiling (status truncated, or billed unusable with a cut stop reason; the gateway's own cut rows
  and the provider's refused cuts included), with at least ``min_requests`` replies. Eval rows count under their real
  stage (unlike the other checks); shadow rows and the probe stages of ``token_floor.NO_RETRY_STAGES`` are left out
  (a cut is a probe's expected answer). Bell at most daily.
- ``route_digest`` — the daily digest (job mode ``digest``): per stage over the last 24 h, the answered requests, the
  fallback share, requests with no answer logged and the extra cost; raised only when one stage used a fallback or
  lost requests (the job result always keeps the digest). AA-900 (S271 l11): ``failed_attempts`` leaves out the
  expected refusals (``shared/llm_client/expected_refusal.py``: a model with no grant yet on an account that answers),
  counted apart as ``expected_refusals``; no alert reads them.

Quality guard: ``hold_fallback_writes(stage)`` (cached ``CACHE_TTL_SECONDS`` per process, never raises; off when
the table is missing or unreadable) tells ``services/quality/route_guard.py`` whether content written by a fallback
model is held for review or only marked.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from shared.llm_client.expected_refusal import EXPECTED_SQL
from shared.ops_checks import (EVENT_TYPE_PREFIX, ROUTE_BACK_PREFIX, ROUTE_COST_PREFIX, ROUTE_CUT_PREFIX,
                               ROUTE_DIGEST_CHECK, ROUTE_FALLBACK_PREFIX, ROUTE_LAST_PREFIX, Finding)

logger = structlog.get_logger()

WATCH_WINDOW = timedelta(minutes=60)
COST_WINDOW = timedelta(hours=24)
DIGEST_WINDOW = timedelta(hours=24)
BASELINE_WINDOW = timedelta(days=7)
BASELINE_MIN = 3
STATE_LOOKBACK = timedelta(days=7)
CACHE_TTL_SECONDS = 30.0
FAILURE_TTL_SECONDS = 60.0
DIGEST_MAX_STAGES = 25
POLICY_FIELDS = ("fallback_alert_pct", "min_requests", "alert_last_rung", "cost_shift_usd", "hold_fallback_writes")
DEFAULT_STAGE = "*"


@dataclass(frozen=True)
class Policy:
    """The effective route policy of one stage (every field set)."""
    stage: str
    fallback_alert_pct: int = 20
    min_requests: int = 5
    alert_last_rung: bool = True
    cost_shift_usd: float = 1.00
    hold_fallback_writes: bool = False


DEFAULTS = Policy(stage=DEFAULT_STAGE)

POLICY_SQL = """
    SELECT stage, fallback_alert_pct, min_requests, alert_last_rung, cost_shift_usd::float AS cost_shift_usd,
           hold_fallback_writes, notes, updated_at, updated_by
      FROM shared.llm_route_policy
     ORDER BY stage
"""
UPSERT_SQL = """
    INSERT INTO shared.llm_route_policy (stage, fallback_alert_pct, min_requests, alert_last_rung, cost_shift_usd,
                                         hold_fallback_writes, notes, updated_at, updated_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8)
    ON CONFLICT (stage) DO UPDATE
       SET fallback_alert_pct = EXCLUDED.fallback_alert_pct, min_requests = EXCLUDED.min_requests,
           alert_last_rung = EXCLUDED.alert_last_rung, cost_shift_usd = EXCLUDED.cost_shift_usd,
           hold_fallback_writes = EXCLUDED.hold_fallback_writes, notes = EXCLUDED.notes, updated_at = now(),
           updated_by = EXCLUDED.updated_by
    RETURNING stage, fallback_alert_pct, min_requests, alert_last_rung, cost_shift_usd::float AS cost_shift_usd,
              hold_fallback_writes, notes, updated_at, updated_by
"""
DELETE_SQL = "DELETE FROM shared.llm_route_policy WHERE stage = $1 RETURNING stage"

ROUTES_SQL = ("SELECT stage, role, model_id, fallback_model_ids, is_active FROM shared.llm_role_config "
              "ORDER BY stage")

# The rows of the ledger that describe real traffic: no shadow, no eval run, no Jev decision row.
LEDGER_FILTER = """coalesce(is_shadow, false) = false AND coalesce(is_experiment, false) = false
       AND coalesce(provider, '') <> 'typesafe'"""
# The route position of a gateway-tagged row: route_position on an answered row, position on an attempt row.
POSITION_EXPR = """CASE WHEN quality_signal ? 'route_trace'
                 AND coalesce(quality_signal ->> 'route_position', quality_signal ->> 'position') ~ '^[0-9]{1,3}$'
                THEN coalesce(quality_signal ->> 'route_position', quality_signal ->> 'position')::int END"""
OK = "coalesce(status, 'ok') = 'ok'"
# The rung of an answered row (see the module doc). One SQL rule for every read; ``is_fallback`` is the same rule in
# Python (the Trace drawer).
FALLBACK_RUNG = f"""(coalesce(fallback_used, false) OR coalesce(attempt_no, 1) > 1
       OR coalesce({POSITION_EXPR}, 0) > 0)"""
PRIMARY_RUNG = f"(attempt_no = 1 AND NOT {FALLBACK_RUNG})"
UNKNOWN_RUNG = f"(attempt_no IS NULL AND NOT {FALLBACK_RUNG})"

# Per stage since $1: answered requests by rung, the fallback cost, the furthest route position reached and the
# requests (route traces) with no answered row.
STATS_SQL = f"""
    WITH rows AS (
        SELECT stage, status, attempt_no, fallback_used, quality_signal, cost_usd, created_at,
               quality_signal ->> 'route_trace' AS trace, {POSITION_EXPR} AS position,
               {EXPECTED_SQL} AS expected
          FROM shared.llm_call_log
         WHERE created_at >= $1 AND {LEDGER_FILTER}
    ), lost AS (
        SELECT stage, count(*) AS lost
          FROM (SELECT stage, trace, bool_or({OK}) AS answered FROM rows WHERE trace IS NOT NULL
                 GROUP BY stage, trace) t
         WHERE NOT answered
         GROUP BY stage
    )
    SELECT r.stage,
           count(*) FILTER (WHERE {OK}) AS answered,
           count(*) FILTER (WHERE {OK} AND {PRIMARY_RUNG}) AS primary_answered,
           count(*) FILTER (WHERE {OK} AND {FALLBACK_RUNG}) AS fallback_answered,
           count(*) FILTER (WHERE {OK} AND {UNKNOWN_RUNG}) AS unknown_answered,
           coalesce(sum(cost_usd) FILTER (WHERE {OK} AND {FALLBACK_RUNG}), 0)::float AS fallback_cost_usd,
           coalesce(sum(cost_usd) FILTER (WHERE {OK}), 0)::float AS cost_usd,
           count(*) FILTER (WHERE status IN ('refused', 'error') AND NOT expected) AS failed_attempts,
           count(*) FILTER (WHERE expected) AS expected_refusals,
           max(position) AS max_position,
           max(created_at) FILTER (WHERE {OK} AND {PRIMARY_RUNG}) AS last_primary_at,
           max(created_at) FILTER (WHERE {OK} AND {FALLBACK_RUNG}) AS last_fallback_at,
           coalesce(max(l.lost), 0) AS lost
      FROM rows r LEFT JOIN lost l ON l.stage = r.stage
     GROUP BY r.stage
"""
# S271: per stage since $1, the replies (a billed row: answered, cut or billed unusable; or a refused cut: the provider
# refused to return a reply cut at its ceiling, status truncated, nothing billed) and those cut at their token
# ceiling. $2 = the cut stop reasons, $3 = the stages left out (probes).
# Review S271: its own filter, not LEDGER_FILTER. Eval rows (is_experiment, an R2 / targeted-shadow run) are kept: they
# drive the real stage paths with the real ceilings (the R2 judge cuts that motivated S271 were all eval rows), and
# count under their real stage (quality_signal.real_stage, call_log._relabel). Shadow and Jev rows stay out.
CUT_ALERT_PCT = 5.0
CUT_STAGE_EXPR = "coalesce(nullif(quality_signal ->> 'real_stage', ''), stage)"
CUT_FILTER = "coalesce(is_shadow, false) = false AND coalesce(provider, '') <> 'typesafe'"
CUT_ROW = "(status = 'truncated' OR (status = 'billed_unusable' AND stop_reason = ANY($2::text[])))"
CUT_STATS_SQL = f"""
    SELECT {CUT_STAGE_EXPR} AS stage,
           count(*) AS replies,
           count(*) FILTER (WHERE {CUT_ROW}) AS cut,
           count(*) FILTER (WHERE status = 'truncated' AND coalesce(tokens_out, 0) = 0
                              AND coalesce(cost_usd, 0) = 0) AS refused_cut,
           count(*) FILTER (WHERE coalesce(is_experiment, false)) AS eval_rows,
           coalesce(sum(cost_usd) FILTER (WHERE {CUT_ROW}), 0)::float AS cut_cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND {CUT_FILTER} AND NOT ({CUT_STAGE_EXPR} = ANY($3::text[]))
       AND (coalesce(tokens_out, 0) > 0 OR coalesce(cost_usd, 0) > 0 OR status = 'truncated')
     GROUP BY 1
"""
# The primary rung's average cost per answered request, per stage since $1.
BASELINE_SQL = f"""
    SELECT stage, count(*) AS n, avg(cost_usd)::float AS avg_cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND {LEDGER_FILTER} AND {OK} AND {PRIMARY_RUNG} AND cost_usd IS NOT NULL
     GROUP BY stage
"""
# The newest route bell of each check name since $1 (the alert state: degraded / primary back).
STATE_SQL = """
    SELECT entity_id AS check_name, max(created_at) AS last
      FROM shared.notifications
     WHERE event_type LIKE $2 AND created_at >= $1
     GROUP BY entity_id
"""


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def row_of(r: Any) -> dict:
    d = dict(r)
    if d.get("updated_at") is not None and hasattr(d["updated_at"], "isoformat"):
        d["updated_at"] = d["updated_at"].isoformat()
    return d


def effective(rows: dict[str, dict], stage: str) -> Policy:
    """The policy of ``stage``: its own row, then the ``*`` row, then DEFAULTS, field by field (NULL falls through).
    Pure."""
    values = asdict(DEFAULTS)
    for key in (DEFAULT_STAGE, stage):
        row = rows.get(key) if key else None
        for f in POLICY_FIELDS:
            v = (row or {}).get(f)
            if v is not None:
                values[f] = v
    values["stage"] = stage
    values["fallback_alert_pct"] = int(values["fallback_alert_pct"])
    values["min_requests"] = int(values["min_requests"])
    values["cost_shift_usd"] = float(values["cost_shift_usd"])
    values["alert_last_rung"] = bool(values["alert_last_rung"])
    values["hold_fallback_writes"] = bool(values["hold_fallback_writes"])
    return Policy(**values)


async def read_policies(db) -> tuple[bool, dict[str, dict]]:
    """(ready, {stage: row}) through ``db`` (a pool or a connection). ready False before the llm_route_policy
    migration."""
    import asyncpg
    try:
        rows = await db.fetch(POLICY_SQL)
    except asyncpg.exceptions.UndefinedTableError:
        return False, {}
    return True, {r["stage"]: row_of(r) for r in rows}


async def _load() -> dict[str, dict]:
    from . import connections
    conn = await connections._connect()
    try:
        return (await read_policies(conn))[1]
    finally:
        await conn.close()


def loader() -> dict[str, dict]:
    """The sync loader of the cache (tests replace it; the unit conftest: no database)."""
    from . import connections
    return connections.run_sync(_load)


_rows: dict[str, dict] = {}
_loaded_at = 0.0
_failed_at = 0.0
_lock = threading.Lock()


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < FAILURE_TTL_SECONDS))


def reset() -> None:
    """Drop the cache (a policy write, tests)."""
    global _rows, _loaded_at, _failed_at
    _rows, _loaded_at, _failed_at = {}, 0.0, 0.0


def policies() -> dict[str, dict]:
    """The cached policy rows ({} = none, or unreadable). Never raises."""
    global _rows, _loaded_at, _failed_at
    if _fresh(time.monotonic()):
        return _rows
    with _lock:
        if _fresh(time.monotonic()):
            return _rows
        try:
            _rows, _loaded_at, _failed_at = dict(loader()), time.monotonic(), 0.0
        except Exception as e:  # noqa: BLE001 — the hold switch fails open (off): content is still marked
            _rows, _loaded_at, _failed_at = {}, 0.0, time.monotonic()
            logger.warning("llm_route_policy_read_failed", error=str(e)[:300])
    return _rows


def hold_fallback_writes(stage: Optional[str]) -> bool:
    """True when content written by a fallback model on ``stage`` is held for review. Never raises."""
    try:
        return effective(policies(), stage or "").hold_fallback_writes
    except Exception:  # noqa: BLE001
        return False


def validate_policy(fields: dict) -> dict:
    """The stored values of one policy row (None = inherit). Raises ValueError on a bad value. Pure."""
    out: dict = {}
    pct = fields.get("fallback_alert_pct")
    if pct is not None and not (isinstance(pct, int) and not isinstance(pct, bool) and 1 <= pct <= 100):
        raise ValueError("fallback_alert_pct must be a whole number between 1 and 100")
    mn = fields.get("min_requests")
    if mn is not None and not (isinstance(mn, int) and not isinstance(mn, bool) and 1 <= mn <= 10000):
        raise ValueError("min_requests must be a whole number between 1 and 10000")
    cost = fields.get("cost_shift_usd")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not (0.01 <= float(cost) <= 10000):
            raise ValueError("cost_shift_usd must be between 0.01 and 10000")
        cost = round(float(cost), 2)
    for key in ("alert_last_rung", "hold_fallback_writes"):
        v = fields.get(key)
        if v is not None and not isinstance(v, bool):
            raise ValueError(f"{key} must be true, false or null")
        out[key] = v
    notes = fields.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 500):
        raise ValueError("notes must be text of at most 500 characters")
    out.update(fallback_alert_pct=pct, min_requests=mn, cost_shift_usd=cost, notes=notes or None)
    return out


# ── stats ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def is_fallback(attempt_no: Any, position: Any, fallback_used: Any) -> bool:
    """``FALLBACK_RUNG`` in Python: an answer from a later model or deployment, or one the gateway marked. Pure."""
    return bool(fallback_used) or (attempt_no or 1) > 1 or (position or 0) > 0


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def chain_of(route: dict) -> list[str]:
    return [route["model_id"], *list(route.get("fallback_model_ids") or [])]


async def read_routes(db) -> dict[str, dict]:
    return {r["stage"]: {"stage": r["stage"], "role": r["role"], "model_id": r["model_id"],
                         "fallback_model_ids": list(r["fallback_model_ids"] or []), "is_active": r["is_active"]}
            for r in await db.fetch(ROUTES_SQL)}


async def read_stats(db, since: datetime) -> dict[str, dict]:
    return {r["stage"]: dict(r) for r in await db.fetch(STATS_SQL, since)}


async def read_baseline(db, since: datetime) -> dict[str, dict]:
    return {r["stage"]: dict(r) for r in await db.fetch(BASELINE_SQL, since)}


async def read_cut_stats(db, since: datetime) -> dict[str, dict]:
    """S271: per stage since ``since`` (an eval row under its real stage), the replies and those cut at their token
    ceiling (``CUT_STATS_SQL``)."""
    from .errors import TRUNCATED_STOP_REASONS
    from .token_floor import NO_RETRY_STAGES
    rows = await db.fetch(CUT_STATS_SQL, since, sorted(TRUNCATED_STOP_REASONS), sorted(NO_RETRY_STAGES))
    return {r["stage"]: dict(r) for r in rows}


def cut_findings(cut_stats: dict[str, dict], rows: dict[str, dict]) -> list[Finding]:
    """S271: ``route_cut.<stage>`` when more than ``CUT_ALERT_PCT`` % of a stage's replies over ``COST_WINDOW``
    (billed ones and refused cuts) were cut at their token ceiling, with at least the policy's ``min_requests``
    replies. Pure."""
    out: list[Finding] = []
    for stage in sorted(cut_stats):
        st = cut_stats[stage] or {}
        replies, cut = int(st.get("replies") or 0), int(st.get("cut") or 0)
        pol = effective(rows, stage)
        if replies <= 0 or replies < pol.min_requests:
            continue
        pct = round(100.0 * cut / replies, 1)
        if pct <= CUT_ALERT_PCT:
            continue
        refused, evals = int(st.get("refused_cut") or 0), int(st.get("eval_rows") or 0)
        notes = ([f"{refused} refused by the provider"] if refused else []) + \
            ([f"{evals} of the replies from eval runs"] if evals else [])
        out.append(Finding(ROUTE_CUT_PREFIX + stage,
                           f"stage {stage}: {cut} of {replies} replies in the last 24 h were cut at their token "
                           f"ceiling ({pct:g} %, threshold {CUT_ALERT_PCT:g} %"
                           + "".join(f"; {n}" for n in notes) + ")",
                           {"stage": stage, "replies": replies, "cut": cut, "cut_pct": pct,
                            "refused_cut": refused, "eval_rows": evals,
                            "cut_cost_usd": round(float(st.get("cut_cost_usd") or 0.0), 6),
                            "threshold_pct": CUT_ALERT_PCT,
                            "window_hours": int(COST_WINDOW.total_seconds() // 3600)}))
    return out


async def read_state(db, now: datetime) -> dict[str, datetime]:
    rows = await db.fetch(STATE_SQL, now - STATE_LOOKBACK, EVENT_TYPE_PREFIX + "route_%")
    return {r["check_name"]: r["last"] for r in rows}


def fallback_rate(st: dict) -> Optional[float]:
    answered = int(st.get("answered") or 0)
    if answered <= 0:
        return None
    return round(100.0 * int(st.get("fallback_answered") or 0) / answered, 1)


def extra_cost(st: dict, base: Optional[dict]) -> Optional[float]:
    """What the fallback answers cost above the primary's average (None without a baseline). Pure."""
    if not base or int(base.get("n") or 0) < BASELINE_MIN or base.get("avg_cost_usd") is None:
        return None
    n = int(st.get("fallback_answered") or 0)
    return round(float(st.get("fallback_cost_usd") or 0.0) - n * float(base["avg_cost_usd"]), 6)


def degraded_since(state: dict[str, datetime], stage: str) -> Optional[datetime]:
    """The newest fallback / last-model bell of ``stage`` when it is newer than its newest primary-back bell. Pure."""
    alerts = [state.get(ROUTE_FALLBACK_PREFIX + stage), state.get(ROUTE_LAST_PREFIX + stage)]
    alerts = [a for a in alerts if a is not None]
    if not alerts:
        return None
    newest = max(alerts)
    back = state.get(ROUTE_BACK_PREFIX + stage)
    return newest if back is None or newest > back else None


def watch_findings(stats: dict[str, dict], cost_stats: dict[str, dict], baseline: dict[str, dict],
                   routes: dict[str, dict], rows: dict[str, dict], state: dict[str, datetime]) -> list[Finding]:
    """The route alerts of one watch run. Pure (unit-tested)."""
    out: list[Finding] = []
    for stage in sorted(set(stats) | set(cost_stats)):
        pol = effective(rows, stage)
        st = stats.get(stage) or {}
        route = routes.get(stage)
        chain = chain_of(route) if route else []
        answered = int(st.get("answered") or 0)
        rate = fallback_rate(st)
        base = {"stage": stage, "window_minutes": int(WATCH_WINDOW.total_seconds() // 60), "answered": answered,
                "fallback_answered": int(st.get("fallback_answered") or 0), "fallback_pct": rate,
                "lost_requests": int(st.get("lost") or 0), "route": chain}
        if rate is not None and answered >= pol.min_requests and rate >= pol.fallback_alert_pct:
            out.append(Finding(ROUTE_FALLBACK_PREFIX + stage,
                               f"stage {stage}: {rate:g} % of {answered} requests in the last "
                               f"{base['window_minutes']} min were answered by a fallback model (threshold "
                               f"{pol.fallback_alert_pct} %)", {**base, "threshold_pct": pol.fallback_alert_pct}))
        max_pos = st.get("max_position")
        if (pol.alert_last_rung and len(chain) >= 2 and max_pos is not None and int(max_pos) >= len(chain) - 1):
            out.append(Finding(ROUTE_LAST_PREFIX + stage,
                               f"stage {stage}: a request reached the last model of its route "
                               f"({chain[-1]}, position {len(chain)})", {**base, "last_model": chain[-1]}))
        since = degraded_since(state, stage)
        last_primary, last_fallback = st.get("last_primary_at"), st.get("last_fallback_at")
        if (since is not None and last_primary is not None and last_primary > since
                and (last_fallback is None or last_primary > last_fallback)):
            out.append(Finding(ROUTE_BACK_PREFIX + stage,
                               f"stage {stage}: the primary model answers again ({chain[0] if chain else 'primary'})",
                               {**base, "degraded_since": _iso(since), "primary_at": _iso(last_primary)}))
        cst = cost_stats.get(stage) or {}
        extra = extra_cost(cst, baseline.get(stage))
        if extra is not None and extra >= pol.cost_shift_usd:
            out.append(Finding(ROUTE_COST_PREFIX + stage,
                               f"stage {stage}: fallback answers cost {extra:.2f} USD more than the primary would "
                               f"have in the last 24 h (threshold {pol.cost_shift_usd:.2f} USD)",
                               {"stage": stage, "extra_usd": extra,
                                "fallback_answered": int(cst.get("fallback_answered") or 0),
                                "fallback_cost_usd": round(float(cst.get("fallback_cost_usd") or 0.0), 6),
                                "primary_avg_usd": round(float(baseline[stage]["avg_cost_usd"]), 6),
                                "threshold_usd": pol.cost_shift_usd}))
    return out


def digest(stats: dict[str, dict], baseline: dict[str, dict], routes: dict[str, dict]) -> dict:
    """The daily digest over ``DIGEST_WINDOW`` (job result + the digest finding's details). Pure."""
    stages = []
    for stage, st in sorted(stats.items()):
        answered = int(st.get("answered") or 0)
        fb = int(st.get("fallback_answered") or 0)
        lost = int(st.get("lost") or 0)
        route = routes.get(stage)
        stages.append({"stage": stage, "answered": answered, "fallback_answered": fb,
                       "fallback_pct": fallback_rate(st), "lost_requests": lost,
                       "failed_attempts": int(st.get("failed_attempts") or 0),
                       "expected_refusals": int(st.get("expected_refusals") or 0),
                       "cost_usd": round(float(st.get("cost_usd") or 0.0), 6),
                       "extra_usd": extra_cost(st, baseline.get(stage)),
                       "primary": route["model_id"] if route else None})
    attention = [s for s in stages if s["fallback_answered"] or s["lost_requests"]]
    attention.sort(key=lambda s: (-(s["fallback_pct"] or 0), -s["lost_requests"], s["stage"]))
    return {"window_hours": int(DIGEST_WINDOW.total_seconds() // 3600), "stages": len(stages),
            "answered": sum(s["answered"] for s in stages),
            "fallback_answered": sum(s["fallback_answered"] for s in stages),
            "lost_requests": sum(s["lost_requests"] for s in stages),
            "attention": attention[:DIGEST_MAX_STAGES], "attention_total": len(attention)}


def digest_finding(d: dict) -> Optional[Finding]:
    """The digest bell / e-mail, only when a stage used a fallback model or lost requests. Pure."""
    if not d.get("attention_total"):
        return None
    lines = [f"{s['stage']}: {s['fallback_answered']}/{s['answered']} by fallback"
             + (f", {s['lost_requests']} with no answer" if s["lost_requests"] else "")
             + (f", +{s['extra_usd']:.2f} USD" if s.get("extra_usd") else "")
             for s in d["attention"][:8]]
    return Finding(ROUTE_DIGEST_CHECK,
                   f"{d['attention_total']} stage(s) used a fallback model or lost requests in the last "
                   f"{d['window_hours']} h ({d['fallback_answered']} of {d['answered']} requests by fallback): "
                   + "; ".join(lines), {"digest": d})


async def evaluate(pool, *, mode: str = "watch", now: Optional[datetime] = None) -> dict:
    """One run of the watch (or the digest): reads only. {"findings": [Finding], "ready": bool, ...}."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as db:
        ready, rows = await read_policies(db)
        routes = await read_routes(db)
        if mode == "digest":
            stats = await read_stats(db, now - DIGEST_WINDOW)
            baseline = await read_baseline(db, now - BASELINE_WINDOW)
            d = digest(stats, baseline, routes)
            f = digest_finding(d)
            return {"mode": mode, "policy_ready": ready, "findings": [f] if f else [], "digest": d}
        stats = await read_stats(db, now - WATCH_WINDOW)
        cost_stats = await read_stats(db, now - COST_WINDOW)
        baseline = await read_baseline(db, now - BASELINE_WINDOW)
        state = await read_state(db, now)
        cut_stats = await read_cut_stats(db, now - COST_WINDOW)
    findings = watch_findings(stats, cost_stats, baseline, routes, rows, state) + cut_findings(cut_stats, rows)
    return {"mode": mode, "policy_ready": ready, "findings": findings, "stages": len(stats),
            "degraded": sorted(s for s in routes if degraded_since(state, s) is not None)}


def policy_view(rows: dict[str, dict], stages: list[str]) -> list[dict]:
    """Per stage: its own row (or None) and the effective values. Pure."""
    return [{"stage": s, "row": rows.get(s), "effective": asdict(effective(rows, s))} for s in stages]
