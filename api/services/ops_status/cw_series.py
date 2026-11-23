"""AA-1140 — the golden-signal and database series of the Ecosystem status page from CloudWatch GetMetricData, in the
chart step of the chosen range (read when the page asks, cached 5 minutes; never sampled 24/7: the S285 advisory
priced continuous sampling at about 3.50 USD a month, a read on page open at well under 1 USD).

One call per range (plus one for the previous period when compare is on), metrics of the existing read-only grant
(AA-CIS-Infra ``ecs_task_infra_status_readonly``: ``cloudwatch:GetMetricData``, no new IAM):

- CIS and Booking APIs: ``AWS/ApiGateway`` Count, 5XXError, 4XXError (Sum per step) and Latency p95 per step plus one
  p95 over the whole range, dimensions ``ApiName`` + ``Stage`` as in :mod:`api.services.ops_overview.cloudwatch`.
- TripPlanner: ``AWS/Lambda`` Invocations, Errors (Sum) and Duration p95 of the browse and assembly functions.
- Partner CRM: none (its API sits behind the shared load balancer whose target-group dimension the CIS task cannot
  look up without a new grant): the page says "not measured yet" for its traffic.
- The shared database: ``AWS/RDS`` CPUUtilization (Average), DatabaseConnections (Maximum) and FreeStorageSpace
  (Minimum) of ``aa-cis-<env>-db``.

About 25 metrics a call (0.01 USD per 1,000 metrics: 0.00025 USD; GetMetricData has no free tier). The caller
(:func:`api.services.ops_status.fetch_range`) reads a live range on a 5-minute grid and a previous period on an hourly
grid, so a page refreshing every minute costs at most one call per 5 minutes per range. Pure parsing, unit-tested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from api.services.ops_overview import cloudwatch
from api.services.ops_status.ranges import TimeRange

logger = structlog.get_logger()

GIB = 1024 ** 3


def db_instance(env: str) -> str:
    return f"aa-cis-{env}-db"


def _stat(qid: str, ns: str, metric: str, dims: list[dict], period: int, stat: str) -> dict:
    return {"Id": qid, "ReturnData": True, "MetricStat": {
        "Metric": {"Namespace": ns, "MetricName": metric, "Dimensions": dims}, "Period": period, "Stat": stat}}


def queries(env: str, rng: TimeRange) -> list[dict]:
    """The GetMetricData queries of one range. Ids: s_<app>_<n>_<key> (per step), t_<app>_<n>_p95 (whole range),
    r_<key> (database). Pure."""
    step, whole = rng.step_s, int(rng.span.total_seconds())
    out: list[dict] = []
    for src in cloudwatch.app_sources(env):
        for n, name in enumerate(src.names):
            if src.kind == "apigw":
                ns, dims = "AWS/ApiGateway", [{"Name": "ApiName", "Value": name}, {"Name": "Stage", "Value": env}]
                sums = (("requests", "Count"), ("errors", "5XXError"), ("client_errors", "4XXError"))
                lat = "Latency"
            elif src.kind == "lambda":
                ns, dims = "AWS/Lambda", [{"Name": "FunctionName", "Value": name}]
                sums = (("requests", "Invocations"), ("errors", "Errors"))
                lat = "Duration"
            else:
                continue
            for key, metric in sums:
                out.append(_stat(f"s_{src.app}_{n}_{key}", ns, metric, dims, step, "Sum"))
            out.append(_stat(f"s_{src.app}_{n}_p95", ns, lat, dims, step, "p95"))
            out.append(_stat(f"t_{src.app}_{n}_p95", ns, lat, dims, whole, "p95"))
    rds = [{"Name": "DBInstanceIdentifier", "Value": db_instance(env)}]
    out.append(_stat("r_cpu", "AWS/RDS", "CPUUtilization", rds, step, "Average"))
    out.append(_stat("r_connections", "AWS/RDS", "DatabaseConnections", rds, step, "Maximum"))
    out.append(_stat("r_free", "AWS/RDS", "FreeStorageSpace", rds, step, "Minimum"))
    return out


def _index(rng: TimeRange) -> dict[int, int]:
    return {int(b.timestamp()): i for i, b in enumerate(rng.buckets())}


def _series(result: Optional[dict], idx: dict[int, int], n: int, *, combine: str = "sum") -> list[Optional[float]]:
    """Values of one result laid on the range's steps (None = no datapoint in that step)."""
    out: list[Optional[float]] = [None] * n
    if not result:
        return out
    for ts, v in zip(result.get("Timestamps", []), result.get("Values", [])):
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        i = idx.get(int(ts.astimezone(timezone.utc).timestamp()))
        if i is None:
            continue
        v = float(v)
        prev = out[i]
        out[i] = v if prev is None else (prev + v if combine == "sum" else max(prev, v))
    return out


def _merge(a: list[Optional[float]], b: list[Optional[float]], combine: str) -> list[Optional[float]]:
    out = []
    for x, y in zip(a, b):
        if x is None or y is None:
            out.append(x if y is None else y)
        else:
            out.append(x + y if combine == "sum" else max(x, y))
    return out


def _total(values: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return float(sum(vals)) if vals else None


def parse(results: list[dict], env: str, rng: TimeRange) -> dict:
    """GetMetricData results -> {apps: {app: {requests[], errors[], client_errors[], p95_ms[], totals}}, rds: {...}}.
    Apps without metrics (the CRM) are listed with ``measured: False``. Pure."""
    by_id = {r["Id"]: r for r in results}
    idx = _index(rng)
    n = len(idx)
    apps: dict[str, dict] = {}
    for src in cloudwatch.app_sources(env):
        if src.kind not in ("apigw", "lambda"):
            apps[src.app] = {"measured": False, "label": src.label}
            continue
        keys = ("requests", "errors", "client_errors") if src.kind == "apigw" else ("requests", "errors")
        agg: dict[str, list[Optional[float]]] = {k: [None] * n for k in keys}
        agg["p95_ms"] = [None] * n
        whole: list[float] = []
        seen = False
        for i, _name in enumerate(src.names):
            for k in keys:
                r = by_id.get(f"s_{src.app}_{i}_{k}")
                seen = seen or r is not None
                agg[k] = _merge(agg[k], _series(r, idx, n), "sum")
            agg["p95_ms"] = _merge(agg["p95_ms"], _series(by_id.get(f"s_{src.app}_{i}_p95"), idx, n, combine="max"),
                                   "max")
            t = by_id.get(f"t_{src.app}_{i}_p95")
            if t and t.get("Values"):
                whole.append(max(float(v) for v in t["Values"]))
        req, err = _total(agg["requests"]), _total(agg["errors"])
        cerr = _total(agg["client_errors"]) if "client_errors" in agg else None
        apps[src.app] = {
            "measured": seen, "label": src.label, "kind": src.kind,
            **{k: [None if v is None else round(v, 3) for v in agg[k]] for k in agg},
            "totals": {
                "requests": int(round(req)) if req is not None else 0,
                "errors": int(round(err)) if err is not None else 0,
                "client_errors": int(round(cerr)) if cerr is not None else (0 if "client_errors" in agg else None),
                "p95_ms": round(max(whole), 1) if whole else None,
            },
        }
    free = _series(by_id.get("r_free"), idx, n, combine="max")
    rds = {
        "measured": any(by_id.get(k) for k in ("r_cpu", "r_connections", "r_free")),
        "instance": db_instance(env),
        "cpu_pct": [None if v is None else round(v, 2) for v in _series(by_id.get("r_cpu"), idx, n, combine="max")],
        "connections": [None if v is None else int(v) for v in
                        _series(by_id.get("r_connections"), idx, n, combine="max")],
        "free_gib": [None if v is None else round(v / GIB, 2) for v in free],
    }
    return {"apps": apps, "rds": rds}


def collect(rng: TimeRange, env: Optional[str] = None, client: Any = None) -> dict:
    """Sync (run it in a thread): {status, as_of, ...parse()} or ``unavailable``. Never raises."""
    env = env or cloudwatch.environment()
    client = client or cloudwatch.aws_client("cloudwatch")
    qs = queries(env, rng)
    try:
        results, calls = cloudwatch.get_metric_data(client, qs, rng.start, rng.end)
    except Exception as e:  # noqa: BLE001 — AWS / network: the section degrades
        logger.warning("ops_status_cw_series_failed", error=repr(e)[:300])
        reason = "not_permitted" if cloudwatch.is_denied(e) else "aws_error"
        return {"status": "unavailable", "reason": reason,
                "message": "AWS CloudWatch did not answer. Try again in a few minutes." if reason == "aws_error"
                else "The CIS server may not read CloudWatch metrics."}
    logger.info("ops_status_cw_series", metrics=len(qs), calls=calls, range=rng.key, step_s=rng.step_s)
    return {"status": "ok", "as_of": datetime.now(timezone.utc).isoformat(), "metrics": len(qs),
            **parse(results, env, rng)}
