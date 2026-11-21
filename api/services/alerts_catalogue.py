"""AA-1171 (S287 alerts catalogue P0, tech lead decisions D1, D2, D3, D5, D7) — Platform › Alerts › Catalogue: every
alert condition of the ecosystem in one list, and its summary for the Ecosystem status card.

One row per alert condition (a CloudWatch alarm, an EventBridge rule that notifies an SNS topic, an AWS budget or cost
anomaly subscription, an in-app check of CIS, Booking or the partner CRM). Each row says: name, app, component, type,
severity, condition, threshold and where it is set (``threshold_source``), how it is changed (``editable_via``:
``settings`` = Platform › Settings or the app's own policy page, maker-checker; ``terraform`` = an AA-CIS-Infra pull
request, D2: Terraform is the only writer of AWS alarm thresholds, the admin never calls PutMetricAlarm; ``none`` = a
code constant or a fixed AWS rule), channel, runbook, owner role, live state and freshness, and a coverage gap note.

Sources (no cross-schema DB read, no new write path):

- **AWS** (acc2, us-west-1; read-only grants of the CIS task role, AA-CIS-Infra modules/ecs
  ``ecs_task_infra_status_readonly``): ``cloudwatch:DescribeAlarms`` (definitions and states), ``tag:GetResources``
  (the App / Severity / Component / Mutable tags of alarms and rules in one paged call; ``ListTagsForResource`` per
  ARN when GetResources is refused), ``events:ListRules`` + ``ListTargetsByRule`` (which rules notify a topic). Free
  API calls, run on a cache miss only (5 minutes in Redis, :mod:`api.services.ops_overview.cache`); no GetMetricData.
- **Budgets**: acc2 from the Terraform docs output (Platform › Infra, :mod:`api.services.platform_infra`: the
  generated alarm page of the Docs bundle); acc1, acc3 and the management account statically from their Terraform
  roots (:data:`EXTERNAL_BUDGETS`, D7: no cross-account read role), with the account ID inserted at run time
  (``shared.aws_client.accounts`` and env ``AWS_MGMT_ACCOUNT_ID``); none is written in this public repo.
- **CIS in-app checks**: the registry of ``shared/ops_checks.py`` (fixed checks, per-stage families, digests) with the
  thresholds in force (``shared/ops_thresholds.py``, CIS 469) and the last bell of each check (shared.notifications).
- **Booking and CRM in-app checks**: each app exports its own check registry with the thresholds in force (Booking
  ``GET /booking/ops/checks``, CRM ``crm.ops.v1 GET /ops-checks``), called with the staff member's own Cognito token
  (the CRM console relay pattern). An app that does not answer leaves its rows out and says so in ``freshness``.

Never returned: an ARN, an account ID other than the budget rows' own, a dimension value (load balancer / target group
IDs), a secret. The answer is the same for every staff member allowed to read it (ops.read), so it is cached as one
document; the peer reads need a token, so a cache miss without a Cognito session leaves those two sources
``unavailable: cognito_session_required`` for that refresh only.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx
import structlog

from api.core.app_registry import APPS
from api.services.ops_overview import cloudwatch as cw

logger = structlog.get_logger()

from api.services.alerts_catalogue_rows import (  # noqa: E402  the row shape and its vocabulary
    APP_LABELS, EDIT_NONE, EDIT_SETTINGS, EDIT_TERRAFORM, INFRA_REPO, KNOWN_APPS, OWNER_ROLE, SETTINGS_PAGE,
    TYPE_ANOMALY, TYPE_BUDGET, TYPE_COMPOSITE, TYPE_EVENTBRIDGE, TYPE_IN_APP, TYPE_METRIC, TYPE_METRIC_MATH, TYPES,
    row as _row,
)
PEER_TIMEOUT_S = 4.0
MAX_PAGES = 10  # technical-constant: page bound of an AWS list call (a few dozen alarms and rules)
RUNBOOK_MARK = " Runbook: "
CACHE_PREFIX = "admin:platform:alerts"
CACHE_TTL_S = 300  # technical-constant: cache refresh of a read (the AWS describe calls), like /admin/overview/ops

_OPERATORS = {
    "GreaterThanThreshold": ">", "GreaterThanOrEqualToThreshold": ">=", "LessThanThreshold": "<",
    "LessThanOrEqualToThreshold": "<=", "LessThanLowerOrGreaterThanUpperThreshold": "outside band",
    "LessThanLowerThreshold": "below band", "GreaterThanUpperThreshold": "above band",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (v if isinstance(v, str) else None)


def environment() -> str:
    return cw.environment()


# ── helpers (pure) ───────────────────────────────────────────────────────────────────────────────

def split_runbook(description: str) -> tuple[str, Optional[str]]:
    """(text, runbook URL) of an alarm / rule description ending with ' Runbook: <url>' (AA-CIS-Infra
    alarm_runbooks.tf)."""
    text = description or ""
    if RUNBOOK_MARK in text:
        head, _, url = text.rpartition(RUNBOOK_MARK)
        url = url.strip()
        if url.startswith("https://"):
            return head.strip(), url
    return text.strip(), None


def app_from_name(name: str, env: str) -> Optional[str]:
    """The app of a resource name by its prefix (the fallback when the App tag is missing)."""
    for prefix, app in ((f"aa-cis-{env}-", "cis"), (f"aa-booking-{env}-", "booking"),
                        (f"aa-tripplanner-{env}-", "tripplanner"), (f"aa-crm-{env}-", "crm"),
                        (f"aa-identity-{env}-", "identity"), (f"aa-sec-{env}-", "shared"),
                        (f"aa-ecosys-{env}-", "shared"), (f"aa-email-{env}-", "shared")):
        if name.startswith(prefix):
            return app
    return None


def terraform_search_url(name: str, env: str) -> str:
    """A GitHub code search of AA-CIS-Infra for the alarm or rule name without its interpolated prefix (the Terraform
    name is built from variables, so the literal suffix is what the file holds)."""
    suffix = name
    for prefix in (f"aa-cis-{env}-", f"aa-booking-{env}-", f"aa-tripplanner-{env}-", f"aa-crm-{env}-",
                   f"aa-identity-{env}-", f"aa-sec-{env}-", f"aa-ecosys-{env}-", f"aa-email-{env}-"):
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            break
    q = quote(f'repo:{INFRA_REPO} "{suffix}"')
    return f"https://github.com/search?q={q}&type=code"


def settings_href(app: str, key: str) -> str:
    return f"{SETTINGS_PAGE}?app={app}&key={quote(key)}"


def topic_label(arn: str, alerts_arn: Optional[str], page_arn: Optional[str]) -> str:
    """A plain channel name for an alarm or rule action; never the ARN."""
    if alerts_arn and arn == alerts_arn:
        return "alerts topic"
    if page_arn and arn == page_arn:
        return "page topic"
    if ":automate:" in arn and arn.endswith(":ec2:recover"):
        return "EC2 recover"
    if arn.startswith("arn:aws:sns:"):
        name = arn.rsplit(":", 1)[-1]
        if name.endswith("-page-alerts"):
            return "page topic"
        if name.endswith("-acp-alerts"):
            return "alerts topic"
        return f"SNS topic {name}"
    return "other action"


def _channels(alarm_actions: list[str], ok_actions: list[str], alerts_arn: Optional[str],
              page_arn: Optional[str]) -> list[str]:
    ok = {topic_label(a, alerts_arn, page_arn) for a in ok_actions}
    out = []
    for a in alarm_actions:
        label = topic_label(a, alerts_arn, page_arn)
        out.append(f"{label} (ALARM + OK)" if label in ok else label)
    return sorted(set(out))


def _num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def alarm_condition(a: dict) -> tuple[str, str, Optional[str]]:
    """(type, condition text without dimension values, threshold text) of one DescribeAlarms metric alarm."""
    op = _OPERATORS.get(a.get("ComparisonOperator", ""), a.get("ComparisonOperator", ""))
    threshold = _num(a.get("Threshold")) if a.get("Threshold") is not None else None
    evals = int(a.get("EvaluationPeriods") or 1)
    dta = int(a.get("DatapointsToAlarm") or evals)
    window = f"{dta} of {evals}" if dta != evals else str(evals)
    missing = a.get("TreatMissingData")
    tail = f", missing data {missing}" if missing and missing != "missing" else ""
    if a.get("Metrics"):
        result = next((m for m in a["Metrics"] if m.get("ReturnData")), None) or {}
        label = result.get("Label") or result.get("Id") or "expression"
        expr = result.get("Expression") or ""
        metrics = [m["MetricStat"]["Metric"].get("MetricName", "") for m in a["Metrics"] if m.get("MetricStat")]
        text = f"{label} {op} {threshold} ({expr}; over {', '.join(metrics)}), {window} period(s){tail}"
        return TYPE_METRIC_MATH, text, threshold
    stat = a.get("Statistic") or a.get("ExtendedStatistic") or ""
    period = int(a.get("Period") or 0)
    text = (f"{a.get('Namespace', '')}/{a.get('MetricName', '')} {stat} {op} {threshold} per {period // 60 or period}"
            f"{' min' if period >= 60 else ' s'}, {window} period(s){tail}")
    return TYPE_METRIC, text, threshold


def _event_names(node: Any) -> list[str]:
    """The event names / ids / categories a pattern's detail matches (nested $or included)."""
    if isinstance(node, list):
        return [n for x in node for n in _event_names(x)]
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    for k, v in node.items():
        if k in ("eventName", "EventID", "EventCategories") and isinstance(v, list):
            out.extend(str(x) for x in v if isinstance(x, str))
        else:
            out.extend(_event_names(v))
    return out


def event_summary(pattern_text: Optional[str], schedule: Optional[str]) -> str:
    if schedule:
        return f"schedule {schedule}"
    try:
        pattern = json.loads(pattern_text or "{}")
    except ValueError:
        return "event pattern"
    names = _event_names(pattern.get("detail"))
    text = ", ".join(pattern.get("source") or []) or "events"
    dtypes = ", ".join(pattern.get("detail-type") or [])
    if dtypes:
        text += f" / {dtypes}"
    if names:
        shown = sorted(dict.fromkeys(names))
        text += ": " + ", ".join(shown[:8]) + (f" (+{len(shown) - 8})" if len(shown) > 8 else "")
    return text


# ── AWS: alarms and rules ────────────────────────────────────────────────────────────────────────

def _paged(call: Callable[..., dict], key: str, token_in: str, token_out: str, **kw: Any) -> tuple[list[dict], int]:
    items: list[dict] = []
    token: Optional[str] = None
    calls = 0
    while True:
        args = dict(kw)
        if token:
            args[token_in] = token
        resp = call(**args)
        calls += 1
        items.extend(resp.get(key) or [])
        token = resp.get(token_out)
        if not token or calls >= MAX_PAGES:
            return items, calls


def read_tags(tagging: Any, cloudwatch: Any, events: Any, alarm_arns: list[str],
              rule_arns: list[str]) -> tuple[dict[str, dict], str]:
    """{ARN: tags} for alarms and rules, and how they were read: ``get_resources`` (one paged call) or
    ``per_resource`` (ListTagsForResource for each ARN when GetResources is refused or returns nothing)."""
    tags: dict[str, dict] = {}
    try:
        items, _ = _paged(tagging.get_resources, "ResourceTagMappingList", "PaginationToken", "PaginationToken",
                          ResourceTypeFilters=["cloudwatch:alarm", "events:rule"], ResourcesPerPage=100)
        for it in items:
            tags[it.get("ResourceARN", "")] = {t["Key"]: t["Value"] for t in it.get("Tags") or []}
        if tags or not (alarm_arns or rule_arns):
            return tags, "get_resources"
    except Exception as e:  # noqa: BLE001 — refused or unsupported: fall back to the per-resource reads
        logger.info("alerts_catalogue_get_resources_failed", error=cw.is_denied(e) and "denied" or type(e).__name__)
    for arn in alarm_arns:
        try:
            resp = cloudwatch.list_tags_for_resource(ResourceARN=arn)
            tags[arn] = {t["Key"]: t["Value"] for t in resp.get("Tags") or []}
        except Exception:  # noqa: BLE001 — one unreadable alarm keeps the others
            continue
    for arn in rule_arns:
        try:
            resp = events.list_tags_for_resource(ResourceARN=arn)
            tags[arn] = {t["Key"]: t["Value"] for t in resp.get("Tags") or []}
        except Exception:  # noqa: BLE001
            continue
    return tags, "per_resource"


def read_aws(cloudwatch: Any, events: Any, tagging: Any) -> dict:
    """The raw AWS reads (sync; run in a thread): alarms, SNS-notifying rules with their targets, tags."""
    metric_alarms: list[dict] = []
    composite: list[dict] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        kw: dict[str, Any] = {"MaxRecords": 100, "AlarmTypes": ["MetricAlarm", "CompositeAlarm"]}
        if token:
            kw["NextToken"] = token
        resp = cloudwatch.describe_alarms(**kw)
        metric_alarms.extend(resp.get("MetricAlarms") or [])
        composite.extend(resp.get("CompositeAlarms") or [])
        token = resp.get("NextToken")
        if not token:
            break
    rules, _ = _paged(events.list_rules, "Rules", "NextToken", "NextToken", Limit=100)
    notifying: list[dict] = []
    for r in rules:
        try:
            targets, _ = _paged(events.list_targets_by_rule, "Targets", "NextToken", "NextToken", Rule=r["Name"])
        except Exception:  # noqa: BLE001 — a rule whose targets cannot be read is listed without them
            targets = []
        sns = [t.get("Arn", "") for t in targets if str(t.get("Arn", "")).startswith("arn:aws:sns:")]
        if sns:
            notifying.append({**r, "_sns_targets": sns})
    alarm_arns = [a.get("AlarmArn", "") for a in metric_alarms + composite if a.get("AlarmArn")]
    tags, tag_mode = read_tags(tagging, cloudwatch, events, alarm_arns, [r.get("Arn", "") for r in notifying])
    return {"metric_alarms": metric_alarms, "composite_alarms": composite, "rules": notifying, "tags": tags,
            "tag_mode": tag_mode}


def aws_rows(raw: dict, env: str, as_of: str, *, alerts_arn: Optional[str], page_arn: Optional[str]) -> list[dict]:
    """Catalogue rows of the AWS reads. Pure."""
    rows: list[dict] = []
    tags = raw.get("tags") or {}
    for a in list(raw.get("metric_alarms") or []) + list(raw.get("composite_alarms") or []):
        name = a.get("AlarmName", "")
        if name.startswith("TargetTracking-"):
            continue
        t = tags.get(a.get("AlarmArn", ""), {})
        app = t.get("App") or app_from_name(name, env)
        desc, runbook = split_runbook(a.get("AlarmDescription") or "")
        if a in (raw.get("composite_alarms") or []):
            kind, condition, threshold = TYPE_COMPOSITE, f"composite: {a.get('AlarmRule', '')}", None
        else:
            kind, condition, threshold = alarm_condition(a)
        channels = _channels(list(a.get("AlarmActions") or []), list(a.get("OKActions") or []), alerts_arn,
                             page_arn)
        gap = None
        if not t.get("Severity"):
            gap = "No Severity tag: it cannot be routed as page or ticket."
        elif not channels:
            gap = "Notifies nobody: the alarm has no action."
        elif a.get("StateValue") == "INSUFFICIENT_DATA":
            gap = "No data: the alarm cannot fire until its metric reports."
        if not a.get("ActionsEnabled", True):
            gap = "Actions are disabled: the alarm notifies nobody."
        rows.append(_row(
            id=f"cw:{name}", name=name, app=app, component=t.get("Component"), type=kind, source="cloudwatch",
            severity=t.get("Severity"), mutable=(t.get("Mutable") == "true") if t.get("Mutable") else None,
            condition=condition, threshold=threshold, threshold_source="Terraform (AA-CIS-Infra)",
            editable_via=EDIT_TERRAFORM, edit_href=terraform_search_url(name, env), channel=channels,
            runbook=runbook, state=a.get("StateValue"), state_since=_iso(a.get("StateUpdatedTimestamp")),
            freshness=as_of, coverage_gap=gap, description=desc[:400]))
    for r in raw.get("rules") or []:
        name = r.get("Name", "")
        t = tags.get(r.get("Arn", ""), {})
        app = t.get("App") or app_from_name(name, env)
        desc, runbook = split_runbook(r.get("Description") or "")
        channels = sorted({topic_label(a, alerts_arn, page_arn) for a in r.get("_sns_targets") or []})
        rows.append(_row(
            id=f"eb:{name}", name=name, app=app, component=t.get("Component"), type=TYPE_EVENTBRIDGE,
            source="eventbridge", severity=t.get("Severity"), mutable=False,
            condition=event_summary(r.get("EventPattern"), r.get("ScheduleExpression")), threshold=None,
            threshold_source="Terraform event pattern (AA-CIS-Infra)", editable_via=EDIT_TERRAFORM,
            edit_href=terraform_search_url(name, env), channel=channels, runbook=runbook,
            state=r.get("State"), freshness=as_of,
            coverage_gap=None if t.get("Severity") else "No Severity tag: it cannot be routed as page or ticket.",
            description=desc[:400]))
    return rows


# ── Budgets: api/services/alerts_catalogue_budgets.py (re-exported for the tests and the status card) ───────────────

from api.services.alerts_catalogue_budgets import (  # noqa: E402
    EXTERNAL_BUDGETS, EXTERNAL_ROLE, external_account_id, external_budget_rows, main_budget_rows,
)


# ── CIS in-app checks ────────────────────────────────────────────────────────────────────────────

# Where the threshold of a check family without a cis.ops.* key lives.
_ROUTE_POLICY = "AI gateway › Trace › Alert settings (route policy)"
_FAMILY_SOURCE: dict[str, tuple[str, str, Optional[str]]] = {
    # prefix -> (editable_via, threshold_source, edit_href)
    "route_": (EDIT_SETTINGS, _ROUTE_POLICY, "/admin/ai-gateway?tab=trace"),
    "qf_digest": (EDIT_SETTINGS, "the quality funnel settings of the findings it sums up (cis.ops.qf_*, "
                  "cis.ops.reply_cut_alert_pct)", f"{SETTINGS_PAGE}?app=cis&area=ops"),
    "llm_stage_coverage.": (EDIT_NONE, "no threshold: any active stage without a live model", None),
    "research_auto_": (EDIT_SETTINGS, "Platform › Settings, area seo (cis.seo.research_*)",
                       f"{SETTINGS_PAGE}?app=cis&area=seo"),
    "research_new_waiting": (EDIT_SETTINGS, "Platform › Settings, cis.seo.research_new_wait_alert_days",
                             settings_href("cis", "cis.seo.research_new_wait_alert_days")),
    "research_month_spend": (EDIT_SETTINGS, "Platform › Settings, area spend (research monthly cap)",
                             f"{SETTINGS_PAGE}?app=cis&area=spend"),
    "llm_spend_alert.": (EDIT_SETTINGS, "the app profile daily budget (AI gateway › App profiles)",
                         "/admin/ai-gateway?tab=budgets"),
}

_STATE_COMPONENT = {"jobs_starved": "jobs", "jobs_overdue": "jobs", "rds_role_connections": "db",
                    "rds_xid_age": "db", "cis_app_too_many_connections": "db", "recompute_lifecycle_lag": "jobs"}

LAST_ALERTS_SQL = """
    SELECT substr(event_type, length('platform.ops_check.') + 1) AS check_name, max(created_at) AS last_at
      FROM shared.notifications
     WHERE event_type LIKE 'platform.ops_check.%' AND created_at >= now() - interval '7 days'
     GROUP BY 1
"""


def _threshold_text(keys: tuple[str, ...]) -> str:
    from shared import settings

    parts = []
    for k in keys:
        try:
            d = settings.definition(k)
            parts.append(f"{_num(settings.get(k))}{(' ' + d.unit) if d.unit else ''}")
        except Exception:  # noqa: BLE001 — an unknown key shows as such, never fails the page
            parts.append(f"{k} (not readable)")
    return "; ".join(parts)


def cis_check_rows(as_of: str, last_alerts: dict[str, datetime], now: datetime) -> list[dict]:
    """The CIS in-app checks with their thresholds in force and their last bell (state ALARM while the last bell is
    within the check's bell window). One row per fixed check, one per per-stage family and digest."""
    from shared import ops_checks, ops_thresholds

    rows: list[dict] = []
    entries: list[tuple[str, Any, bool]] = [(name, chk, False) for name, chk in ops_checks.CHECKS.items()]
    entries.append((ops_checks.STAGE_COVERAGE_PREFIX, ops_checks.STAGE_COVERAGE_CHECK, True))
    entries += [(p, chk, True) for p, chk in ops_checks.PREFIX_CHECKS.items()]
    entries += [(name, chk, False) for name, chk in ops_checks.PREFIX_CHECKS_EXACT.items()]
    log_events = set(ops_checks.EVENT_CHECKS.values()) | {"tenant_route_admin_pool", "cis_app_too_many_connections"}
    for name, chk, family in entries:
        keys = ops_thresholds.BY_CHECK.get(name, ())
        if keys:
            editable, source, href = EDIT_SETTINGS, "Platform › Settings, " + ", ".join(keys), settings_href(
                "cis", keys[0])
            if name == ops_checks.QF_FALLBACK_PREFIX:
                source += "; fallback share: " + _ROUTE_POLICY
            threshold = _threshold_text(keys)
        elif name in log_events:
            editable, source, href, threshold = EDIT_NONE, "no threshold: any matching log line", None, "1 line"
        else:
            match = next((v for p, v in _FAMILY_SOURCE.items() if name.startswith(p)), None)
            editable, source, href = match if match else (EDIT_NONE, "code (shared/ops_checks.py)", None)
            threshold = None
        if family:
            last = max((t for c, t in last_alerts.items() if c.startswith(name)), default=None)
            label = f"{name}<stage>"
        else:
            last = last_alerts.get(name)
            label = name
        firing = last is not None and last >= now - chk.bell_every
        if chk.fixed_bell is None:
            source += f"; bell repeat: Platform › Settings, {chk.bell_key}"
        gap = "Runs in the CIS api process: silent while the api is down (its no-healthy-task alarm covers that)."
        rows.append(_row(
            id=f"check:cis:{label}", name=label, app="cis", component=_STATE_COMPONENT.get(name, "observability"),
            type=TYPE_IN_APP, source="cis", severity="ticket", mutable=False, condition=chk.title,
            threshold=threshold, threshold_source=source, editable_via=editable, edit_href=href,
            channel=["admin bell", "alerts topic e-mail (once per check and UTC day)"], runbook=None,
            state="ALARM" if firing else "OK", last_alert_at=_iso(last), freshness=as_of, coverage_gap=gap,
            description=chk.action[:400]))
    return rows


async def last_cis_alerts(pool: Any) -> dict[str, datetime]:
    if pool is None:
        return {}
    try:
        rows = await pool.fetch(LAST_ALERTS_SQL)
    except Exception as e:  # noqa: BLE001 — the rows still list without a state
        logger.warning("alerts_catalogue_bells_unreadable", error=type(e).__name__)
        return {}
    return {str(r["check_name"]): r["last_at"] for r in rows}


# ── Booking and CRM registries ───────────────────────────────────────────────────────────────────

_peer_transport: Optional[httpx.AsyncBaseTransport] = None   # tests swap in an httpx.MockTransport


def peer_rows(app: str, body: dict, as_of: str) -> list[dict]:
    """Rows of a peer app's check registry (``{app, checks: [...]}``, AA-Booking / AA-Partner ops check registry
    contract v1). Unknown keys are ignored; a malformed check is skipped. Pure."""
    rows = []
    for c in body.get("checks") or []:
        if not isinstance(c, dict) or not isinstance(c.get("key"), str):
            continue
        thresholds = [t for t in c.get("thresholds") or [] if isinstance(t, dict)]
        editable = EDIT_SETTINGS if any(t.get("editable_via") == "settings" for t in thresholds) else EDIT_NONE
        threshold = "; ".join(f"{_num(t.get('value'))}{(' ' + str(t['unit'])) if t.get('unit') else ''}"
                              for t in thresholds if t.get("value") is not None) or None
        keys = [str(t.get("setting")) for t in thresholds if t.get("setting")]
        where = next((str(t["where"]) for t in thresholds if t.get("where")), None)
        href = None
        if app == "crm" and keys:
            href = settings_href("crm", keys[0])
        state = c.get("state") if c.get("state") in ("OK", "ALARM") else None
        rows.append(_row(
            id=f"check:{app}:{c['key']}", name=c["key"], app=app, component=c.get("component") or "observability",
            type=TYPE_IN_APP, source=app, severity=c.get("severity") if c.get("severity") in ("page", "ticket")
            else "ticket", mutable=False, condition=str(c.get("title") or c["key"]), threshold=threshold,
            threshold_source=(where or (", ".join(keys) if keys else "no threshold")), editable_via=editable,
            edit_href=href, channel=[str(x) for x in c.get("channel") or []] or ["alerts topic e-mail"],
            state=state, last_alert_at=c.get("last_alert_at"), freshness=body.get("as_of") or as_of,
            coverage_gap=c.get("coverage_gap") or (f"Runs in the {APP_LABELS.get(app, app)} API process: silent "
                                                   "while that API is down (its no-healthy-task alarm covers that)."),
            description=str(c.get("action") or "")[:400]))
    return rows


async def fetch_peer(app: str, authorization: Optional[str]) -> tuple[Optional[dict], dict]:
    """(registry body or None, freshness entry) of Booking or the CRM, with the staff member's own token."""
    from api.services.platform_apps import _cognito_bearer

    token = _cognito_bearer(authorization)
    if token is None:
        return None, {"status": "unavailable", "reason": "cognito_session_required"}
    if app == "crm":
        from api.services import crm_console

        status, body = await crm_console.crm_ops_call("GET", "/ops-checks", authorization)
        if status != 200 or not isinstance(body, dict):
            return None, {"status": "unavailable", "reason": str(body.get("detail") or status)}
        return body, {"status": "ok", "as_of": body.get("as_of")}
    url = f"{APPS['booking'].health_base}/booking/ops/checks"
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT_S, transport=_peer_transport) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except Exception as e:  # noqa: BLE001 — timeout, DNS, TLS
        logger.warning("alerts_catalogue_peer_failed", app=app, error=type(e).__name__)
        return None, {"status": "unavailable", "reason": "unreachable"}
    if r.status_code != 200:
        return None, {"status": "unavailable", "reason": f"http_{r.status_code}"}
    try:
        body = r.json()
    except ValueError:
        return None, {"status": "unavailable", "reason": "bad_response"}
    if not isinstance(body, dict):
        return None, {"status": "unavailable", "reason": "bad_response"}
    return body, {"status": "ok", "as_of": body.get("as_of")}


# ── Gaps and summary ─────────────────────────────────────────────────────────────────────────────

STATIC_GAPS = (
    {"key": "no_synthetic_check", "text": "No outside check calls the public APIs and sites (uptime, DNS, "
     "certificate): an outage that the load balancer does not see is found by users first. Planned for the pilot."},
    {"key": "sentry_rules_not_listed", "text": "Sentry alert rules of the four apps are configured in Sentry and "
     "not listed here."},
    {"key": "ci_guards_not_listed", "text": "Scheduled CI guards, Dependabot and secret scanning alert by GitHub "
     "e-mail and are not listed here."},
    {"key": "vendor_mail_not_listed", "text": "Provider e-mails (Vercel, AWS Health, certificate expiry, model "
     "provider usage) go to their account owner and are not listed here."},
)

RECIPIENT_COUNTS_SQL = """
    SELECT topic, count(*) FILTER (WHERE status = 'confirmed') AS confirmed
      FROM shared.alert_recipient WHERE status <> 'removed' GROUP BY topic
"""


async def recipient_gaps(pool: Any) -> list[dict]:
    """Live gaps of the alert path: one confirmed recipient or fewer on the alerts topic (plus the Terraform
    bootstrap), none on the page topic."""
    if pool is None:
        return []
    try:
        rows = await pool.fetch(RECIPIENT_COUNTS_SQL)
    except Exception as e:  # noqa: BLE001 — before CIS 469 (no topic column) or DB down: no live gap
        logger.info("alerts_catalogue_recipients_unreadable", error=type(e).__name__)
        return []
    counts = {str(r["topic"]): int(r["confirmed"] or 0) for r in rows}
    out = []
    if counts.get("alerts", 0) == 0:
        out.append({"key": "single_recipient", "text": "Only the Terraform bootstrap address receives the alerts "
                    "topic: if that inbox is lost, alerts reach nobody. Add a second confirmed recipient."})
    if counts.get("page", 0) == 0:
        out.append({"key": "page_no_recipient", "text": "The page topic has no recipient: page alerts reach the "
                    "alerts topic recipients only. Expected on Dev; add a recipient or a paging channel before "
                    "production."})
    return out


def summarize(rows: list[dict], gaps: list[dict]) -> dict:
    """Counts for the Ecosystem status card and the page header. Pure."""
    by_type = {t: 0 for t in TYPES}
    by_app: dict[str, int] = {}
    by_sev = {"page": 0, "ticket": 0, "untagged": 0}
    firing_by_app: dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        app = r["app"] or "unknown"
        by_app[app] = by_app.get(app, 0) + 1
        by_sev[r["severity"] if r["severity"] in ("page", "ticket") else "untagged"] += 1
        if r["state"] == "ALARM":
            firing_by_app[app] = firing_by_app.get(app, 0) + 1
    return {"total": len(rows), "by_type": by_type, "by_app": dict(sorted(by_app.items())),
            "by_severity": by_sev, "firing": sum(firing_by_app.values()), "firing_by_app": firing_by_app,
            "page_firing": sum(1 for r in rows if r["state"] == "ALARM" and r["severity"] == "page"),
            "gaps": len(gaps) + sum(1 for r in rows if r["coverage_gap"] and r["source"] in ("cloudwatch",
                                                                                            "eventbridge"))}


# ── Build ────────────────────────────────────────────────────────────────────────────────────────

def _topics() -> tuple[Optional[str], Optional[str]]:
    return ((os.environ.get("ALERTS_TOPIC_ARN") or "").strip() or None,
            (os.environ.get("PAGE_TOPIC_ARN") or "").strip() or None)


def _aws_clients() -> tuple[Any, Any, Any]:
    return cw.aws_client("cloudwatch"), cw.aws_client("events"), cw.aws_client("resourcegroupstaggingapi")


async def aws_section(clients: Optional[Callable[[], tuple[Any, Any, Any]]] = None) -> dict:
    """The AWS rows (cached section: no Redis = no AWS call)."""
    env = environment()
    as_of = _now().isoformat()
    alerts_arn, page_arn = _topics()

    def run() -> dict:
        cwc, ev, tg = (clients or _aws_clients)()
        return read_aws(cwc, ev, tg)

    started = time.monotonic()
    try:
        raw = await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        logger.warning("alerts_catalogue_aws_failed", error=repr(e)[:300])
        reason = "not_permitted" if cw.is_denied(e) else "aws_error"
        return {"status": "unavailable", "reason": reason, "rows": []}
    rows = aws_rows(raw, env, as_of, alerts_arn=alerts_arn, page_arn=page_arn)
    logger.info("alerts_catalogue_aws_read", rows=len(rows), tag_mode=raw.get("tag_mode"),
                ms=round((time.monotonic() - started) * 1000))
    return {"status": "ok", "as_of": as_of, "tag_mode": raw.get("tag_mode"), "rows": rows}


async def peers_section(authorization: Optional[str]) -> dict:
    as_of = _now().isoformat()
    out: dict[str, Any] = {"status": "ok", "as_of": as_of, "rows": [], "freshness": {}}
    results = await asyncio.gather(fetch_peer("booking", authorization), fetch_peer("crm", authorization))
    for app, (body, fresh) in zip(("booking", "crm"), results):
        out["freshness"][app] = fresh
        if body is not None:
            out["rows"] += peer_rows(app, body, as_of)
    if all(f["status"] != "ok" for f in out["freshness"].values()):
        out["status"] = "unavailable"
    return out


async def budgets_section() -> dict:
    from api.services import platform_infra
    from shared.aws_client import accounts

    as_of = _now().isoformat()
    try:
        infra = await platform_infra.read_infra()
    except Exception as e:  # noqa: BLE001
        logger.warning("alerts_catalogue_infra_unreadable", error=type(e).__name__)
        infra = {"groups": []}
    main_id = await asyncio.to_thread(accounts.account_id, accounts.MAIN_ACCOUNT_KEY)
    ids = {k: await asyncio.to_thread(external_account_id, k) for k in ("acc1", "acc3", "mgmt")}
    rows = main_budget_rows(infra, as_of, main_id) + external_budget_rows(as_of, ids.get)
    doc = infra.get("doc") or {}
    return {"status": "ok" if not doc.get("missing") else "partial", "as_of": as_of, "rows": rows,
            "source": {"generated_at": doc.get("generated_at"), "terraform_sha": doc.get("terraform_sha")}}


async def build(pool: Any, redis: Any, authorization: Optional[str], *,
                clients: Optional[Callable[[], tuple[Any, Any, Any]]] = None) -> dict:
    """The whole catalogue: cached AWS, peer and budget sections, live CIS checks and gaps."""
    from api.services.ops_overview import cache

    now = _now()
    aws = await cache.cached_section(redis, "aws", CACHE_TTL_S, lambda: aws_section(clients), needs_cache=True,
                                     key_prefix=CACHE_PREFIX)
    peers = await cache.cached_section(redis, "peers", CACHE_TTL_S, lambda: peers_section(authorization),
                                       needs_cache=False, key_prefix=CACHE_PREFIX)
    budgets = await cache.cached_section(redis, "budgets", CACHE_TTL_S, budgets_section, needs_cache=False,
                                         key_prefix=CACHE_PREFIX)
    cis_rows = cis_check_rows(now.isoformat(), await last_cis_alerts(pool), now)
    rows = list(aws.get("rows") or []) + cis_rows + list(peers.get("rows") or []) + list(budgets.get("rows") or [])
    gaps = [dict(g) for g in STATIC_GAPS] + await recipient_gaps(pool)
    freshness = {
        "aws": {"status": aws.get("status"), "as_of": aws.get("as_of"), "reason": aws.get("reason"),
                "message": aws.get("message"), "tag_mode": aws.get("tag_mode")},
        "cis": {"status": "ok", "as_of": now.isoformat()},
        "budgets": {"status": budgets.get("status"), "as_of": budgets.get("as_of"),
                    "source": budgets.get("source")},
    }
    for app in ("booking", "crm"):
        freshness[app] = (peers.get("freshness") or {}).get(app) or {"status": peers.get("status"),
                                                                     "reason": peers.get("reason")}
    return {"as_of": now.isoformat(), "environment": environment(), "summary": summarize(rows, gaps),
            "freshness": freshness, "rows": sorted(rows, key=lambda r: (r["app"] or "", r["type"], r["name"] or "")),
            "gaps": gaps}


def summary_view(doc: dict) -> dict:
    """The status-card answer (no rows): counts, firing per app and the firing page-severity rows by name."""
    firing = [{"name": r["name"], "app": r["app"], "severity": r["severity"], "since": r["state_since"]
               or r["last_alert_at"]} for r in doc.get("rows") or [] if r.get("state") == "ALARM"]
    return {"as_of": doc.get("as_of"), "environment": doc.get("environment"), "summary": doc.get("summary"),
            "freshness": doc.get("freshness"), "firing": firing[:50], "catalogue_href": "/admin/platform/alerts"
            "?tab=catalogue"}
