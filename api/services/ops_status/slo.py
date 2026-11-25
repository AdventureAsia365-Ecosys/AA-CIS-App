"""AA-1142 (S292, P2) — the availability objective (SLO) of each app, its error budget and the burn rate alerts of the
Ecosystem status page (SRE Workbook, multiwindow burn rate; S285 review idea 9).

Per app, over three windows that end now:

- the budget window (``cis.ops.status_slo_window_days``, 30): availability = good / all, the error budget = 100 % minus
  the objective (``cis.ops.status_slo_target_pct``, 99.5), budget left = 1 - (bad share / budget share);
- the fast window (``cis.ops.status_burn_fast_window_min``, 60) and the slow window
  (``cis.ops.status_burn_slow_window_hours``, 6): burn rate = bad share of the window / budget share. Above
  ``cis.ops.status_burn_fast_factor`` (14.4) the fast burn is a SEV2 Needs-attention item, above
  ``cis.ops.status_burn_slow_factor`` (6) the slow burn a SEV3 one. A window with fewer events than
  ``cis.ops.status_min_requests`` (CIS 475) is shown but not judged ("few requests"), as for the traffic tiles.

Events. CIS and Booking: API Gateway Count and 5XXError; TripPlanner: its two Lambdas' Invocations and Errors (the
same metrics as the golden signals, :mod:`.cw_series`). The partner CRM's requests are not measured from CIS (no
load balancer grant), so its events are the 5-minute health samples of the sampler (``acp_shared.ops_sample`` kind
``app_health``: ok / warn good, degraded / down bad, unknown not counted); its basis is ``health samples``.

Cost. Two GetMetricData calls of 8 metrics (requests and errors of the four sources): the short windows on a 5-minute
period, cached 5 minutes in Redis, the budget window on an hourly period, cached one hour; never read without Redis.
At most 288 + 24 calls a day (about 2,500 metrics, 0.025 USD a day if a page stays open all day; nothing when nobody
looks). No new AWS service, no new IAM (the existing ``cloudwatch:GetMetricData`` grant).

The burn alert is proved on Dev by a synthetic error burst: ``GET /admin/overview/status/slo/drill`` answers 503 on
purpose (ops.read, staff only; ``scripts/ops/slo_error_burst.py`` calls it N times).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services.ops_overview import cache, cloudwatch
from shared import ops_status_thresholds as th

logger = structlog.get_logger()

KEY_PREFIX = "admin:overview:status"
SHORT_TTL_S = 300  # technical-constant: cache refresh of a read (Redis TTL)
WINDOW_TTL_S = 3600  # technical-constant: cache refresh of the budget window read (Redis TTL)
SHORT_PERIOD_S = 300  # technical-constant: CloudWatch period of the burn windows (API Gateway's 5-minute grid)
WINDOW_PERIOD_S = 3600  # technical-constant: CloudWatch period of the budget window
SAMPLE_MINUTES = 5
GOOD_STATES, BAD_STATES = ("ok", "warn"), ("degraded", "down")

HEALTH_SQL = """
    SELECT count(*) FILTER (WHERE state IN ('ok', 'warn') AND sampled_at >= $2) AS good_window,
           count(*) FILTER (WHERE state IN ('degraded', 'down') AND sampled_at >= $2) AS bad_window,
           count(*) FILTER (WHERE state IN ('ok', 'warn') AND sampled_at >= $3) AS good_slow,
           count(*) FILTER (WHERE state IN ('degraded', 'down') AND sampled_at >= $3) AS bad_slow,
           count(*) FILTER (WHERE state IN ('ok', 'warn') AND sampled_at >= $4) AS good_fast,
           count(*) FILTER (WHERE state IN ('degraded', 'down') AND sampled_at >= $4) AS bad_fast
      FROM acp_shared.ops_sample
     WHERE kind = 'app_health' AND subject = $1 AND sampled_at >= $2
"""


def _floor(t: datetime, step_s: int) -> datetime:
    e = int(t.timestamp())
    return datetime.fromtimestamp(e - e % step_s, tz=timezone.utc)


def _ceil(t: datetime, step_s: int) -> datetime:
    f = _floor(t, step_s)
    return f if f == t else f + timedelta(seconds=step_s)


def queries(env: str, period_s: int) -> list[dict]:
    """GetMetricData queries of requests and errors of every measured source (ids ``<app>_<n>_<key>``). Pure."""
    out = []
    for src in cloudwatch.app_sources(env):
        for n, name in enumerate(src.names):
            if src.kind == "apigw":
                ns, dims = "AWS/ApiGateway", [{"Name": "ApiName", "Value": name}, {"Name": "Stage", "Value": env}]
                sums = (("requests", "Count"), ("errors", "5XXError"))
            elif src.kind == "lambda":
                ns, dims = "AWS/Lambda", [{"Name": "FunctionName", "Value": name}]
                sums = (("requests", "Invocations"), ("errors", "Errors"))
            else:
                continue
            for key, metric in sums:
                out.append({"Id": f"{src.app}_{n}_{key}", "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": ns, "MetricName": metric, "Dimensions": dims},
                    "Period": period_s, "Stat": "Sum"}})
    return out


def _ts(v: Any) -> datetime:
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v.astimezone(timezone.utc)


def points(results: list[dict], env: str) -> dict[str, dict[str, list[tuple[str, float]]]]:
    """{app: {requests: [(iso time, value)], errors: [...]}} summed over the app's sources per timestamp. Pure."""
    by_id = {r["Id"]: r for r in results}
    out: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for src in cloudwatch.app_sources(env):
        if src.kind not in ("apigw", "lambda"):
            continue
        app: dict[str, dict[str, float]] = {"requests": {}, "errors": {}}
        for n, _name in enumerate(src.names):
            for key in ("requests", "errors"):
                r = by_id.get(f"{src.app}_{n}_{key}") or {}
                for ts, v in zip(r.get("Timestamps", []), r.get("Values", [])):
                    k = _ts(ts).isoformat()
                    app[key][k] = app[key].get(k, 0.0) + float(v)
        out[src.app] = {k: sorted(v.items()) for k, v in app.items()}
    return out


def sum_since(series: list[tuple[str, float]], since: datetime) -> float:
    """The sum of the points at or after ``since``. Pure."""
    return float(sum(v for t, v in series if datetime.fromisoformat(t) >= since))


def _read(env: str, start: datetime, end: datetime, period_s: int, client: Any = None) -> dict:
    """Sync (run it in a thread): {status, as_of, apps: points()} or ``unavailable``. Never raises."""
    client = client or cloudwatch.aws_client("cloudwatch")
    qs = queries(env, period_s)
    try:
        results, calls = cloudwatch.get_metric_data(client, qs, start, end)
    except Exception as e:  # noqa: BLE001 — AWS / network: the section degrades
        logger.warning("ops_status_slo_read_failed", error=repr(e)[:300])
        reason = "not_permitted" if cloudwatch.is_denied(e) else "aws_error"
        return {"status": "unavailable", "reason": reason,
                "message": "AWS CloudWatch did not answer. Try again in a few minutes."}
    logger.info("ops_status_slo_read", metrics=len(qs), calls=calls, period_s=period_s)
    return {"status": "ok", "as_of": datetime.now(timezone.utc).isoformat(), "apps": points(results, env)}


def burn(bad: float, total: float, budget_share: float) -> Optional[float]:
    """Burn rate: the window's bad share over the budget share (1.0 = spending the budget exactly over its window).
    None without events. Pure."""
    if total <= 0 or budget_share <= 0:
        return None
    return round((bad / total) / budget_share, 2)


def judge(burn_rate: Optional[float], events: float, factor: float, min_events: int) -> str:
    """ok | alert | few (fewer events than the minimum: shown, not judged) | none (no events). Pure."""
    if burn_rate is None:
        return "none"
    if events < min_events:
        return "few"
    return "alert" if burn_rate > factor else "ok"


def app_row(app: str, label: str, basis: str, counts: dict, rules: dict) -> dict:
    """One app's row from its event counts {window: (good+bad, bad), slow: (...), fast: (...)} and the rules
    (:func:`shared.ops_status_thresholds.slo`). Pure."""
    budget_share = (100.0 - rules["target_pct"]) / 100.0
    total, bad = counts["window"]
    avail = round(100.0 * (total - bad) / total, 3) if total > 0 else None
    left = round(100.0 * (1.0 - (bad / total) / budget_share), 1) if total > 0 and budget_share > 0 else None
    out = {"app": app, "label": label, "basis": basis, "measured": True, "availability_pct": avail,
           "target_pct": rules["target_pct"], "budget_left_pct": left, "events": int(total), "bad": int(bad),
           "below_target": avail is not None and avail < rules["target_pct"]}
    for name, factor in (("fast", rules["fast"]["factor"]), ("slow", rules["slow"]["factor"])):
        n, b = counts[name]
        rate = burn(b, n, budget_share)
        out[name] = {"burn": rate, "events": int(n), "bad": int(b), "factor": factor,
                     "state": judge(rate, n, factor, rules["min_requests"])}
    return out


def not_measured_row(app: str, label: str, reason: str) -> dict:
    return {"app": app, "label": label, "measured": False, "reason": reason}


def windows(now: datetime, rules: dict) -> dict[str, datetime]:
    """Start of each window. Pure."""
    return {"window": now - timedelta(days=rules["window_days"]),
            "slow": now - timedelta(hours=rules["slow"]["window_hours"]),
            "fast": now - timedelta(minutes=rules["fast"]["window_min"])}


def metric_rows(short: dict, long_: dict, env: str, starts: dict[str, datetime], rules: dict) -> list[dict]:
    """The rows of the CloudWatch-measured apps from the two reads. Pure."""
    rows = []
    for src in cloudwatch.app_sources(env):
        if src.kind not in ("apigw", "lambda"):
            continue
        if short.get("status") != "ok" or long_.get("status") != "ok":
            rows.append(not_measured_row(src.app, src.label, "cloudwatch_unavailable"))
            continue
        s = (short.get("apps") or {}).get(src.app) or {"requests": [], "errors": []}
        w = (long_.get("apps") or {}).get(src.app) or {"requests": [], "errors": []}
        counts = {"window": (sum_since(w["requests"], starts["window"]), sum_since(w["errors"], starts["window"]))}
        for name in ("slow", "fast"):
            counts[name] = (sum_since(s["requests"], starts[name]), sum_since(s["errors"], starts[name]))
        rows.append(app_row(src.app, src.label, "requests", counts, rules))
    return rows


async def _health_row(pool: Any, app: str, label: str, starts: dict[str, datetime], rules: dict) -> dict:
    r = await pool.fetchrow(HEALTH_SQL, app, starts["window"], starts["slow"], starts["fast"])
    if r is None or not (r["good_window"] + r["bad_window"]):
        return not_measured_row(app, label, "no_health_samples")
    counts = {k: (r[f"good_{k}"] + r[f"bad_{k}"], r[f"bad_{k}"]) for k in ("window", "slow", "fast")}
    return app_row(app, label, "health samples", counts, rules)


async def collect(pool: Any, redis: Any, now: datetime, env: Optional[str] = None) -> dict:
    """The SLO section: {rules, rows[]} with each app's availability, budget left and burn rates."""
    from api.services.ops_status import sections

    env = env or cloudwatch.environment()
    rules = th.slo()
    starts = windows(now, rules)
    short_end = _ceil(now, SHORT_PERIOD_S)
    short_start = short_end - timedelta(hours=rules["slow"]["window_hours"]) - timedelta(seconds=SHORT_PERIOD_S)
    long_end = _ceil(now, WINDOW_PERIOD_S)
    long_start = _floor(starts["window"], WINDOW_PERIOD_S)

    async def short_read() -> dict:
        return await asyncio.to_thread(_read, env, short_start, short_end, SHORT_PERIOD_S)

    async def long_read() -> dict:
        return await asyncio.to_thread(_read, env, long_start, long_end, WINDOW_PERIOD_S)

    short, long_ = await asyncio.gather(
        cache.cached_section(redis, f"slo_short:{int(short_end.timestamp())}:{rules['slow']['window_hours']}",
                             SHORT_TTL_S, short_read, needs_cache=True, key_prefix=KEY_PREFIX),
        cache.cached_section(redis, f"slo_window:{int(long_end.timestamp())}:{rules['window_days']}",
                             WINDOW_TTL_S, long_read, needs_cache=True, key_prefix=KEY_PREFIX))
    rows = metric_rows(short, long_, env, starts, rules)
    for src in cloudwatch.app_sources(env):
        if src.kind == "probe":
            rows.append(await _health_row(pool, src.app, src.label, starts, rules))
    as_of = short.get("as_of") if short.get("status") == "ok" else None
    return sections.ok({"rules": rules, "rows": rows, "sample_minutes": SAMPLE_MINUTES,
                        "windows": {k: v.isoformat() for k, v in starts.items()}}, SHORT_TTL_S,
                       as_of=datetime.fromisoformat(as_of) if as_of else now)


def items(rows: list[dict]) -> list[tuple[str, str, dict]]:
    """(item id, severity, row) of every alerting burn: fast SEV2, slow SEV3 (one app may have both). Pure."""
    out = []
    for r in rows:
        if not r.get("measured"):
            continue
        if (r.get("fast") or {}).get("state") == "alert":
            out.append((f"slo-fast-{r['app']}", "sev2", r))
        if (r.get("slow") or {}).get("state") == "alert":
            out.append((f"slo-slow-{r['app']}", "sev3", r))
    return out


def _window_minutes(rules: dict, kind: str) -> Optional[int]:
    """The length of a burn window in minutes (S293: the page words it in EN or VI); None without rules. Pure."""
    try:
        return int(rules["fast"]["window_min"]) if kind == "fast" else int(rules["slow"]["window_hours"]) * 60
    except (KeyError, TypeError, ValueError):
        return None


def verdict_items(data: dict) -> list[dict]:
    """The Needs-attention rows of the alerting burns (``verdict._EXTRA_ITEMS``). Pure."""
    from api.services.ops_status.verdict import PAGES, RUNBOOK_INCIDENT, RUNBOOK_ROLLBACK, _item

    rules = data.get("rules") or {}
    out = []
    for iid, sev, r in items(data.get("rows") or []):
        kind = "fast" if iid.startswith("slo-fast-") else "slow"
        b = r[kind]
        window = (f"{rules['fast']['window_min']} min" if kind == "fast" else f"{rules['slow']['window_hours']} h"
                  ) if rules else ""
        out.append(_item(iid, sev, r["app"],
                         f"{r['label']} burns its error budget {b['burn']:g}x over {window} (alert above "
                         f"{b['factor']:g}x)",
                         f"{b['bad']} of {b['events']} {'requests' if r['basis'] == 'requests' else 'health samples'}"
                         f" failed; objective {r['target_pct']:g} %, budget left "
                         f"{'–' if r.get('budget_left_pct') is None else format(r['budget_left_pct'], 'g')} %",
                         source="error budget",
                         msg={"key": "slo_burn", "app": r["app"], "window": kind, "value": b["burn"],
                              "limit": b["factor"], "target": r["target_pct"], "budget_left": r.get("budget_left_pct"),
                              "minutes": _window_minutes(rules, kind)},
                         detail_msg={"key": "slo_burn_detail", "bad": b["bad"], "events": b["events"],
                                     "basis": "requests" if r["basis"] == "requests" else "samples",
                                     "target": r["target_pct"], "budget_left": r.get("budget_left_pct")},
                         links=[("App health", PAGES["apps"]), ("Rollback runbooks", RUNBOOK_ROLLBACK),
                                ("Incident response", RUNBOOK_INCIDENT), ("Settings", PAGES["settings"])]))
    return out
