"""AA-858 — app health from CloudWatch: requests, errors, p95, throttles and firing alarms per app, last 24 h.

Sources (acc2, us-west-1; read-only IAM on the CIS task role, AA-CIS-Infra modules/ecs/main.tf
``ecs_task_infra_status_readonly``: ``cloudwatch:GetMetricData`` + ``cloudwatch:DescribeAlarms``):

- CIS API and Booking API: ``AWS/ApiGateway`` with ``ApiName`` = ``aa-cis-<env>-api`` / ``aa-booking-<env>-api`` and
  ``Stage`` = ``<env>`` (the same dimensions as the Infra alarms): ``Count``, ``5XXError``, ``4XXError`` (Sum, hourly)
  and ``Latency`` p95 over the window.
- TripPlanner: ``AWS/Lambda`` ``FunctionName`` = ``aa-tripplanner-<env>-browse`` and ``-assembly``: ``Invocations``,
  ``Errors``, ``Throttles`` (Sum, hourly) and ``Duration`` p95; the two functions are summed, p95 = the slower one.
- Partner CRM (AA-1127 C3): no traffic metrics (its API sits behind the ALB, whose target-group dimension is not
  derivable from the environment name), so kind ``probe``: no GetMetricData query, only its alarms. Its status on the
  overview comes from its own health check (``platform_apps._crm_card``, the probe /admin/platform/apps already runs)
  plus these alarms, through ``app_health.overall_status``.
- Alarms: grouped by their ``App`` tag (AA-1171, S287: the name prefix put the shared RDS and the CRM WAF alarms under
  CIS and missed alarms with another prefix), read with ``tag:GetResources`` (one paged call); ``identity``, ``shared``
  and ``ecosystem`` go to the shared bucket. An alarm without an App tag, or every alarm when the tag read is refused,
  falls back to its name prefix (``aa-cis-<env>-``, ``aa-booking-<env>-``, ``aa-tripplanner-<env>-``,
  ``aa-crm-<env>-``; shared platform: ``aa-ecosys-<env>-``, ``aa-identity-<env>-``, ``aa-email-<env>-``,
  ``aa-sec-<env>-``). Each bucket and the whole account carry a total and counts per alarm type (``alarm_totals``).
  ``TargetTracking-*`` (ECS autoscaling, normal "AlarmLow") is skipped.

Cost: one GetMetricData call = 16 metrics ($0.01 per 1,000 metrics → $0.00016), DescribeAlarms is free. Called only
on a cache miss (5 min, ``cache.py``), never without Redis. No ``lambda:GetFunctionConfiguration`` (it returns env).

The boto3 calls are synchronous; the caller runs them in ``asyncio.to_thread`` with a fresh boto3 Session per call
(the default Session is not thread-safe, see api/routers/v1_integrations.py ``_sm_client``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

WINDOW_HOURS = 24
HOUR = timedelta(hours=1)
MAX_ALARM_PAGES = 5
# "Worth a look" when at least this share of requests failed with a server error (5xx / Lambda Errors).
WARN_ERROR_RATE_PCT = 1.0


def environment() -> str:
    env = (os.environ.get("ENVIRONMENT") or "dev").lower()
    return "dev" if env in ("test", "local", "") else env


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"


@dataclass(frozen=True)
class AppSource:
    app: str
    label: str
    kind: str                         # "apigw" | "lambda" | "probe" (no metrics: health check + alarms only)
    names: tuple[str, ...]            # API names or function names (empty for "probe")
    alarm_prefixes: tuple[str, ...]


def app_sources(env: str) -> tuple[AppSource, ...]:
    return (
        AppSource("cis", "CIS", "apigw", (f"aa-cis-{env}-api",), (f"aa-cis-{env}-",)),
        AppSource("tripplanner", "TripPlanner", "lambda",
                  (f"aa-tripplanner-{env}-browse", f"aa-tripplanner-{env}-assembly"), (f"aa-tripplanner-{env}-",)),
        AppSource("booking", "Booking", "apigw", (f"aa-booking-{env}-api",), (f"aa-booking-{env}-",)),
        AppSource("crm", "Partner CRM", "probe", (), (f"aa-crm-{env}-",)),
    )


def shared_alarm_prefixes(env: str) -> tuple[str, ...]:
    return (f"aa-ecosys-{env}-", f"aa-identity-{env}-", f"aa-email-{env}-", f"aa-sec-{env}-")


# AA-1171: App tag values that are not one app's own (the shared bucket).
SHARED_APP_TAGS = ("shared", "identity", "ecosystem")
# AA-1171: the alarm type shown in the counts, by CloudWatch namespace (metric math and composite by their shape).
_TYPE_BY_NAMESPACE = {"AWS/ApplicationELB": "load_balancer", "AWS/Lambda": "lambda", "AWS/RDS": "database",
                      "AWS/SES": "email", "AWS/EC2": "instance", "AWS/SNS": "alert_delivery", "AWS/ECS": "service",
                      "AWS/WAFV2": "waf", "AWS/ApiGateway": "api_gateway"}


def alarm_type(a: dict, composite: bool = False) -> str:
    """The type of one DescribeAlarms entry for the counts (AA-1171). Pure."""
    if composite:
        return "composite"
    if a.get("Metrics"):
        return "metric_math"
    ns = str(a.get("Namespace") or "")
    return _TYPE_BY_NAMESPACE.get(ns, "custom_metric" if ns and not ns.startswith("AWS/") else "other")


def read_alarm_tags(tagging: Any) -> dict[str, dict]:
    """{alarm ARN: tags} with tag:GetResources (paged). Raises on a refusal (the caller falls back to prefixes)."""
    out: dict[str, dict] = {}
    token: Optional[str] = None
    for _ in range(MAX_ALARM_PAGES):
        kw: dict[str, Any] = {"ResourceTypeFilters": ["cloudwatch:alarm"], "ResourcesPerPage": 100}
        if token:
            kw["PaginationToken"] = token
        resp = tagging.get_resources(**kw)
        for it in resp.get("ResourceTagMappingList") or []:
            out[it.get("ResourceARN", "")] = {t["Key"]: t["Value"] for t in it.get("Tags") or []}
        token = resp.get("PaginationToken")
        if not token:
            break
    return out


# (metric key, CloudWatch metric name, statistic) per kind; the key is what the parser sums.
_APIGW_SUMS = (("requests", "Count"), ("errors", "5XXError"), ("client_errors", "4XXError"))
_LAMBDA_SUMS = (("requests", "Invocations"), ("errors", "Errors"), ("throttles", "Throttles"))
_P95 = {"apigw": "Latency", "lambda": "Duration"}


def window(now: datetime, hours: int = WINDOW_HOURS) -> tuple[datetime, datetime]:
    """[start, end) aligned to the hour; ``end`` is the start of the next hour, so the current hour is included."""
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + HOUR
    return end - timedelta(hours=hours), end


def metric_queries(sources: tuple[AppSource, ...], env: str, hours: int = WINDOW_HOURS) -> list[dict]:
    """The GetMetricData queries. Id = q_<app>_<n>_<key>; n = index of the API/function within the app."""
    queries: list[dict] = []
    for src in sources:
        for n, name in enumerate(src.names):
            if src.kind == "apigw":
                ns, dims, sums = "AWS/ApiGateway", [{"Name": "ApiName", "Value": name},
                                                     {"Name": "Stage", "Value": env}], _APIGW_SUMS
            else:
                ns, dims, sums = "AWS/Lambda", [{"Name": "FunctionName", "Value": name}], _LAMBDA_SUMS
            for key, metric in sums:
                queries.append({"Id": f"q_{src.app}_{n}_{key}", "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": ns, "MetricName": metric, "Dimensions": dims},
                    "Period": 3600, "Stat": "Sum"}})
            queries.append({"Id": f"q_{src.app}_{n}_p95", "ReturnData": True, "MetricStat": {
                "Metric": {"Namespace": ns, "MetricName": _P95[src.kind], "Dimensions": dims},
                "Period": hours * 3600, "Stat": "p95"}})
    return queries


def get_metric_data(client: Any, queries: list[dict], start: datetime, end: datetime) -> tuple[list[dict], int]:
    """All result pages (NextToken) → (MetricDataResults, number of API calls)."""
    results: list[dict] = []
    token: Optional[str] = None
    calls = 0
    while True:
        kw: dict[str, Any] = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end,
                              "ScanBy": "TimestampAscending"}
        if token:
            kw["NextToken"] = token
        resp = client.get_metric_data(**kw)
        calls += 1
        # A later page carries more datapoints of the same Ids: merge by Id.
        by_id = {r["Id"]: r for r in results}
        for r in resp.get("MetricDataResults", []):
            prev = by_id.get(r["Id"])
            if prev is not None:
                prev["Timestamps"] = list(prev.get("Timestamps", [])) + list(r.get("Timestamps", []))
                prev["Values"] = list(prev.get("Values", [])) + list(r.get("Values", []))
                if r.get("StatusCode") != "Complete":
                    prev["StatusCode"] = r.get("StatusCode")
                else:
                    prev.setdefault("StatusCode", "Complete")
            else:
                results.append(dict(r))
                by_id[r["Id"]] = results[-1]
        token = resp.get("NextToken")
        if not token or calls >= 10:
            return results, calls


def _hour_key(ts: Any) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole else None


def parse_health(results: list[dict], sources: tuple[AppSource, ...], start: datetime,
                 hours: int = WINDOW_HOURS) -> list[dict]:
    """GetMetricData results → one dict per app (totals + hourly series). Pure, unit-tested."""
    by_id = {r["Id"]: r for r in results}
    hour_keys = [(start + i * HOUR).isoformat() for i in range(hours)]
    apps = []
    for src in sources:
        if src.kind == "probe":
            apps.append(_probe_app(src))
            continue
        sums = _APIGW_SUMS if src.kind == "apigw" else _LAMBDA_SUMS
        totals = {key: 0.0 for key, _ in sums}
        hourly = {h: {"requests": 0.0, "errors": 0.0} for h in hour_keys}
        p95s: list[float] = []
        seen = incomplete = 0
        for n in range(len(src.names)):
            for key, _ in sums:
                r = by_id.get(f"q_{src.app}_{n}_{key}")
                if r is None:
                    continue
                seen += 1
                if r.get("StatusCode") not in (None, "Complete"):
                    incomplete += 1
                for ts, v in zip(r.get("Timestamps", []), r.get("Values", [])):
                    totals[key] += float(v)
                    hk = _hour_key(ts)
                    if key in ("requests", "errors") and hk in hourly:
                        hourly[hk][key] += float(v)
            r = by_id.get(f"q_{src.app}_{n}_p95")
            if r is not None and r.get("Values"):
                p95s.append(max(float(v) for v in r["Values"]))
        requests = int(round(totals["requests"]))
        errors = int(round(totals["errors"]))
        app = {
            "app": src.app, "label": src.label, "kind": src.kind, "resources": list(src.names),
            "has_data": seen > 0, "partial": incomplete > 0,
            "requests": requests, "errors": errors, "error_rate_pct": _pct(errors, requests),
            "client_errors": int(round(totals["client_errors"])) if "client_errors" in totals else None,
            "throttles": int(round(totals["throttles"])) if "throttles" in totals else None,
            "p95_ms": round(max(p95s), 1) if p95s else None,
            "hourly": [{"hour": h, "requests": int(round(v["requests"])), "errors": int(round(v["errors"]))}
                       for h, v in hourly.items()],
        }
        apps.append(app)
    return apps


def _probe_app(src: AppSource) -> dict:
    """An app with no traffic metrics (kind ``probe``): the same keys, numbers None, no hourly series."""
    return {
        "app": src.app, "label": src.label, "kind": src.kind, "resources": list(src.names),
        "has_data": False, "partial": False, "requests": None, "errors": None, "error_rate_pct": None,
        "client_errors": None, "throttles": None, "p95_ms": None, "hourly": [],
    }


def alarm_owner(name: str, tag_app: Optional[str], apps: list[str],
                prefixes: list[tuple[str, str]]) -> Optional[str]:
    """AA-1171: the bucket of one alarm: its App tag (an app, or shared for identity / shared / ecosystem), else the
    name prefix, else None (not ours). Pure."""
    if tag_app in apps:
        return tag_app
    if tag_app in SHARED_APP_TAGS:
        return "shared"
    return next((app for p, app in prefixes if name.startswith(p)), None)


def fetch_alarms(client: Any, env: str, tags: Optional[dict[str, dict]] = None) -> tuple[dict, int]:
    """DescribeAlarms (all pages, metric + composite) → ({app or 'shared': {total, firing[], no_data, by_type,
    by_severity}}, calls). ``tags`` ({alarm ARN: tags}, AA-1171) puts each alarm under its App tag; without it (or
    for an alarm with no App tag) the name prefix decides."""
    prefixes = [(p, s.app) for s in app_sources(env) for p in s.alarm_prefixes]
    prefixes += [(p, "shared") for p in shared_alarm_prefixes(env)]
    apps = [s.app for s in app_sources(env)]
    out = {key: {"total": 0, "no_data": 0, "firing": [], "by_type": {}, "by_severity": {}}
           for key in apps + ["shared"]}
    tags = tags or {}
    token: Optional[str] = None
    calls = 0
    while True:
        kw: dict[str, Any] = {"MaxRecords": 100, "AlarmTypes": ["MetricAlarm", "CompositeAlarm"]}
        if token:
            kw["NextToken"] = token
        resp = client.describe_alarms(**kw)
        calls += 1
        composite = list(resp.get("CompositeAlarms", []))
        for a in list(resp.get("MetricAlarms", [])) + composite:
            name = a.get("AlarmName", "")
            if name.startswith("TargetTracking-"):
                continue
            t = tags.get(a.get("AlarmArn", ""), {})
            owner = alarm_owner(name, t.get("App"), apps, prefixes)
            if owner is None:
                continue
            bucket = out[owner]
            bucket["total"] += 1
            kind = alarm_type(a, composite=a in composite)
            bucket["by_type"][kind] = bucket["by_type"].get(kind, 0) + 1
            sev = t.get("Severity") or "untagged"
            bucket["by_severity"][sev] = bucket["by_severity"].get(sev, 0) + 1
            state = a.get("StateValue")
            if state == "ALARM":
                since = a.get("StateUpdatedTimestamp")
                bucket["firing"].append({
                    "name": name, "severity": t.get("Severity"),
                    "description": a.get("AlarmDescription") or "",  # at most 1024 characters (AWS)
                    "since": since.isoformat() if hasattr(since, "isoformat") else since,
                })
            elif state == "INSUFFICIENT_DATA":
                bucket["no_data"] += 1
        token = resp.get("NextToken")
        if not token or calls >= MAX_ALARM_PAGES:
            return out, calls


def app_state(app: dict, alarms: Optional[dict]) -> tuple[str, str]:
    """(state, plain-English reason). state: alarm | warn | ok | unknown."""
    firing = (alarms or {}).get("firing") or []
    if firing:
        n = len(firing)
        return "alarm", f"{n} alarm{'s' if n > 1 else ''} firing"
    if app.get("kind") == "probe":
        return "unknown", "Traffic numbers are not collected for this app; its health check and alarms decide"
    if not app.get("has_data"):
        return "unknown", "No metrics available"
    rate = app.get("error_rate_pct")
    if app.get("errors") and rate is not None and rate >= WARN_ERROR_RATE_PCT:
        return "warn", f"{rate:g}% of requests failed with a server error"
    if app.get("throttles"):
        return "warn", f"{app['throttles']} requests were throttled"
    if not app.get("requests"):
        return "ok", "No traffic in the last 24 hours"
    return "ok", "Working normally"


def _client(service: str) -> Any:
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"})
    return boto3.session.Session().client(service, region_name=region(), config=cfg)


def _error_code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) or type(e).__name__


def _is_denied(e: Exception) -> bool:
    code = _error_code(e)
    return "AccessDenied" in code or code in ("UnauthorizedOperation", "AuthorizationError")


# AA-1003 R2: the public names other modules use (the OPS-12 digest, the DATA-01 probe log read), so every CloudWatch
# caller gets the same short timeouts and bounded retries instead of botocore's 60 s defaults.
def aws_client(service: str) -> Any:
    """A boto3 client with this module's Config (connect 2 s, read 5 s, standard retry mode, max_attempts 2 = at
    most 3 tries a call)."""
    return _client(service)


def is_denied(e: Exception) -> bool:
    """True when an AWS error is a permission refusal (the caller says "not permitted", not "AWS did not answer")."""
    return _is_denied(e)


def collect_health(now: Optional[datetime] = None, env: Optional[str] = None, client: Any = None,
                   tagging: Any = None) -> dict:
    """The health section (sync; run it in a thread). Metrics and alarms fail independently."""
    now = now or datetime.now(timezone.utc)
    env = env or environment()
    sources = app_sources(env)
    start, end = window(now)
    injected = client is not None
    client = client or _client("cloudwatch")
    try:
        results, calls = get_metric_data(client, metric_queries(sources, env), start, end)
    except Exception as e:  # noqa: BLE001 — AWS / network; the section degrades
        logger.warning("ops_overview_cw_metrics_failed", error=repr(e)[:300])
        if _is_denied(e):
            return {"status": "unavailable", "reason": "not_permitted",
                    "message": "The CIS server is not allowed to read CloudWatch yet (an AWS permission change "
                               "is pending)."}
        return {"status": "unavailable", "reason": "aws_error",
                "message": "AWS CloudWatch did not answer. Try again in a few minutes."}
    apps = parse_health(results, sources, start)
    logger.info("ops_overview_cw_fetch", metrics=len(metric_queries(sources, env)), calls=calls)

    alarms: Optional[dict] = None
    alarms_error: Optional[str] = None
    tags: Optional[dict[str, dict]] = None
    try:
        # A caller that passes its own CloudWatch client (tests) passes its tagging client too, or gets prefixes.
        if tagging is None and not injected:
            tagging = _client("resourcegroupstaggingapi")
        if tagging is not None:
            tags = read_alarm_tags(tagging)
    except Exception as e:  # noqa: BLE001 — refused before the AA-1171 grant: group by name prefix
        logger.info("ops_overview_alarm_tags_unavailable", error="denied" if _is_denied(e) else type(e).__name__)
    try:
        alarms, alarm_calls = fetch_alarms(client, env, tags)
        logger.info("ops_overview_alarms_fetch", calls=alarm_calls, grouped_by="tag" if tags else "prefix")
    except Exception as e:  # noqa: BLE001
        logger.warning("ops_overview_cw_alarms_failed", error=repr(e)[:300])
        alarms_error = ("not_permitted" if _is_denied(e) else "aws_error")

    for app in apps:
        a = (alarms or {}).get(app["app"])
        app["alarms"] = a
        app["state"], app["state_reason"] = app_state(app, a)
    return {
        "status": "ok",
        "as_of": now.isoformat(),
        "window_hours": WINDOW_HOURS,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "environment": env,
        "apps": apps,
        "shared_alarms": (alarms or {}).get("shared"),
        "alarms_error": alarms_error,
        "alarm_totals": alarm_totals(alarms) if alarms is not None else None,
        "alarms_grouped_by": "tag" if tags else "prefix",
    }


def alarm_totals(alarms: dict) -> dict:
    """AA-1171: the account's alarm count, per type and per app bucket (Overview › Apps health total line). Pure."""
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for bucket in alarms.values():
        for k, n in (bucket.get("by_type") or {}).items():
            by_type[k] = by_type.get(k, 0) + n
        for k, n in (bucket.get("by_severity") or {}).items():
            by_severity[k] = by_severity.get(k, 0) + n
    return {"total": sum(b["total"] for b in alarms.values()),
            "firing": sum(len(b["firing"]) for b in alarms.values()),
            "by_app": {k: b["total"] for k, b in alarms.items()}, "by_type": dict(sorted(by_type.items())),
            "by_severity": dict(sorted(by_severity.items()))}
