"""AA-1171 (S287 alerts catalogue P0) — api/services/alerts_catalogue.py and its two routes.

Offline: stub CloudWatch / EventBridge / tagging clients (invented names, a made-up ARN account), a fake pool, an
httpx MockTransport for the Booking and CRM registries. Covers: one row per alarm, rule, budget and in-app check with
app / type / severity from the tags; no ARN or dimension value in the answer; the per-resource tag fallback; the
settings threshold of a CIS check and its link; the peer registries mapped, and a peer that does not answer leaves its
rows out with a freshness reason; budgets of the other accounts with the account ID from run time only; the summary
counts; the routes' policy (ops.read)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from api.core.staff_authz import OPS_READ, POLICY
from api.services import alerts_catalogue as cat
from api.services import crm_console

ACCT = "111122223333"
ALERTS = f"arn:aws:sns:us-west-1:{ACCT}:aa-cis-dev-acp-alerts"
PAGE = f"arn:aws:sns:us-west-1:{ACCT}:aa-ecosys-dev-page-alerts"
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)


def _alarm(name, arn_tail, **kw):
    base = {"AlarmName": name, "AlarmArn": f"arn:aws:cloudwatch:us-west-1:{ACCT}:alarm:{arn_tail}",
            "AlarmDescription": f"{name} fired. Runbook: https://github.com/x/AA-Ecosys/blob/master/docs/ops/a.md",
            "StateValue": "OK", "StateUpdatedTimestamp": NOW, "ActionsEnabled": True,
            "AlarmActions": [ALERTS], "OKActions": [ALERTS], "Namespace": "AWS/ApplicationELB",
            "MetricName": "HealthyHostCount", "Statistic": "Minimum", "Period": 60, "EvaluationPeriods": 5,
            "ComparisonOperator": "LessThanThreshold", "Threshold": 1.0, "TreatMissingData": "notBreaching",
            "Dimensions": [{"Name": "TargetGroup", "Value": "targetgroup/aa-test-tg/0123456789abcdef"}]}
    base.update(kw)
    return base


class CW:
    def __init__(self, alarms, tags_ok=True):
        self.alarms, self.tags_ok, self.tag_calls = alarms, tags_ok, 0

    def describe_alarms(self, **kw):
        return {"MetricAlarms": self.alarms, "CompositeAlarms": []}

    def list_tags_for_resource(self, ResourceARN):
        self.tag_calls += 1
        return {"Tags": [{"Key": "App", "Value": "cis"}, {"Key": "Severity", "Value": "ticket"}]}


class Events:
    def list_rules(self, **kw):
        return {"Rules": [
            {"Name": "aa-sec-dev-root-activity", "Arn": f"arn:aws:events:us-west-1:{ACCT}:rule/aa-sec-dev-root",
             "State": "ENABLED", "Description": "Root used. Runbook: https://github.com/x/AA-Ecosys/blob/master/r.md",
             "EventPattern": json.dumps({"source": ["aws.signin"], "detail-type": ["AWS Console Sign In via "
                                                                                  "CloudTrail"]})},
            {"Name": "aa-cis-dev-nightly", "Arn": f"arn:aws:events:us-west-1:{ACCT}:rule/aa-cis-dev-nightly",
             "State": "ENABLED", "ScheduleExpression": "rate(1 day)"},
        ]}

    def list_targets_by_rule(self, Rule, **kw):
        if Rule == "aa-sec-dev-root-activity":
            return {"Targets": [{"Id": "a", "Arn": ALERTS}, {"Id": "p", "Arn": PAGE}]}
        return {"Targets": [{"Id": "l", "Arn": f"arn:aws:lambda:us-west-1:{ACCT}:function:x"}]}

    def list_tags_for_resource(self, ResourceARN):
        return {"Tags": [{"Key": "App", "Value": "shared"}, {"Key": "Severity", "Value": "page"}]}


class Tagging:
    def __init__(self, ok=True):
        self.ok = ok

    def get_resources(self, **kw):
        if not self.ok:
            raise RuntimeError("AccessDenied")
        return {"ResourceTagMappingList": [
            {"ResourceARN": f"arn:aws:cloudwatch:us-west-1:{ACCT}:alarm:api",
             "Tags": [{"Key": "App", "Value": "cis"}, {"Key": "Severity", "Value": "ticket"},
                      {"Key": "Component", "Value": "api"}, {"Key": "Mutable", "Value": "true"}]},
            {"ResourceARN": f"arn:aws:cloudwatch:us-west-1:{ACCT}:alarm:rds",
             "Tags": [{"Key": "App", "Value": "shared"}, {"Key": "Severity", "Value": "ticket"}]},
            {"ResourceARN": f"arn:aws:events:us-west-1:{ACCT}:rule/aa-sec-dev-root",
             "Tags": [{"Key": "App", "Value": "shared"}, {"Key": "Severity", "Value": "page"}]},
        ]}


ALARMS = [
    _alarm("aa-cis-dev-api-no-healthy-task", "api"),
    _alarm("aa-cis-dev-rds-cpu-high", "rds", StateValue="ALARM", Namespace="AWS/RDS", MetricName="CPUUtilization"),
    _alarm("aa-cis-dev-untagged", "untagged", AlarmActions=[], OKActions=[]),
]


def _env(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", ALERTS)
    monkeypatch.setenv("PAGE_TOPIC_ARN", PAGE)


def test_aws_rows_from_tags_without_arns_or_dimensions(monkeypatch):
    _env(monkeypatch)
    raw = cat.read_aws(CW(ALARMS), Events(), Tagging())
    assert raw["tag_mode"] == "get_resources" and [r["Name"] for r in raw["rules"]] == ["aa-sec-dev-root-activity"]
    rows = {r["name"]: r for r in cat.aws_rows(raw, "dev", NOW.isoformat(), alerts_arn=ALERTS, page_arn=PAGE)}
    api = rows["aa-cis-dev-api-no-healthy-task"]
    assert (api["app"], api["type"], api["severity"], api["component"], api["mutable"]) == ("cis", "metric", "ticket",
                                                                                          "api", True)
    assert api["channel"] == ["alerts topic (ALARM + OK)"] and api["editable_via"] == "terraform"
    assert api["edit_href"].startswith("https://github.com/search?") and "api-no-healthy-task" in api["edit_href"]
    assert api["runbook"].endswith("/docs/ops/a.md") and api["coverage_gap"] is None
    assert rows["aa-cis-dev-rds-cpu-high"]["app"] == "shared" and rows["aa-cis-dev-rds-cpu-high"]["state"] == "ALARM"
    untagged = rows["aa-cis-dev-untagged"]
    assert untagged["severity"] is None and "Severity" in untagged["coverage_gap"] and untagged["app"] == "cis"
    rule = rows["aa-sec-dev-root-activity"]
    assert (rule["type"], rule["severity"], rule["channel"]) == ("eventbridge", "page", ["alerts topic", "page topic"])
    text = json.dumps(list(rows.values()), default=str)
    assert "arn:" not in text and ACCT not in text and "0123456789abcdef" not in text


def test_tags_fall_back_to_per_resource_reads():
    cw = CW(ALARMS)
    raw = cat.read_aws(cw, Events(), Tagging(ok=False))
    assert raw["tag_mode"] == "per_resource" and cw.tag_calls == 3


def test_condition_text_of_metric_math():
    kind, text, threshold = cat.alarm_condition({
        "ComparisonOperator": "GreaterThanThreshold", "Threshold": 5.0, "EvaluationPeriods": 3, "DatapointsToAlarm": 2,
        "Metrics": [{"Id": "rate", "Expression": "IF(requests >= 20, 100 * errors / requests, 0)", "ReturnData": True,
                     "Label": "5xx percent"},
                    {"Id": "errors", "MetricStat": {"Metric": {"MetricName": "HTTPCode_Target_5XX_Count"}}}]})
    assert kind == "metric_math" and threshold == "5" and text.startswith("5xx percent > 5") and "2 of 3" in text


def test_cis_check_rows_carry_settings_and_state():
    rows = {r["name"]: r for r in cat.cis_check_rows(NOW.isoformat(), {"jobs_starved": NOW}, NOW)}
    starved = rows["jobs_starved"]
    assert starved["editable_via"] == "settings" and starved["threshold"] == "900 seconds"
    assert starved["edit_href"] == "/admin/platform/settings?app=cis&key=cis.ops.jobs_starved_after_s"
    assert starved["state"] == "ALARM" and rows["rds_xid_age"]["state"] == "OK"
    assert rows["llm_call_log_write_failed"]["threshold"] == "1 line"
    assert "llm_stage_coverage.<stage>" in rows and "route_fb.<stage>" in rows and "qf_digest" in rows
    assert rows["qf_cut.<stage>"]["editable_via"] == "none"
    assert all(r["type"] == "in_app_check" and r["app"] == "cis" for r in rows.values())


def test_peer_rows_map_the_registry_contract():
    body = {"app": "booking", "as_of": NOW.isoformat(), "checks": [
        {"key": "jobs_stuck", "title": "Job queue stuck", "component": "jobs", "severity": "ticket",
         "thresholds": [{"setting": "ops_jobs_stuck_after_s", "value": 900, "unit": "seconds",
                         "editable_via": "settings", "where": "Booking staff admin › Pricing › Policy"}],
         "channel": ["alerts topic e-mail"], "state": "OK"},
        {"key": "bad"},
        "not a dict",
    ]}
    rows = cat.peer_rows("booking", body, NOW.isoformat())
    assert [r["name"] for r in rows] == ["jobs_stuck", "bad"]
    assert rows[0]["threshold"] == "900 seconds" and rows[0]["editable_via"] == "settings"
    assert rows[0]["threshold_source"] == "Booking staff admin › Pricing › Policy"
    crm = cat.peer_rows("crm", {"checks": [{"key": "audit_export_lag", "thresholds": [
        {"setting": "crm.ops.audit_export_lag_min", "value": 180, "unit": "minutes", "editable_via": "settings"}]}]},
        NOW.isoformat())
    assert crm[0]["edit_href"] == "/admin/platform/settings?app=crm&key=crm.ops.audit_export_lag_min"


def test_external_budgets_take_account_ids_at_run_time_only():
    rows = cat.external_budget_rows(NOW.isoformat(), {"acc1": "123456789012", "acc3": None, "mgmt": None}.get)
    assert {r["account_id"] for r in rows} == {"123456789012", None}
    assert all(r["editable_via"] == "terraform" and r["state"] == "not_live" for r in rows)
    src = open(cat.__file__, encoding="utf-8").read()
    import re
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", src), "no account ID in the public repo"


def test_main_budget_rows_from_the_infra_output():
    infra = {"groups": [{"group": "budgets", "items": [
        {"key": "total_monthly_usd", "value": "300"}, {"key": "total_alerts", "value": "ACTUAL > 80 %"},
        {"key": "per_app_monthly_usd.cis", "value": "120"}, {"key": "cost_anomaly_alert_usd", "value": "5"}]}]}
    rows = cat.main_budget_rows(infra, NOW.isoformat(), "210987654321")
    assert [r["name"] for r in rows] == ["monthly-total", "monthly-app-cis", "cost-anomaly-immediate"]
    assert rows[1]["app"] == "cis" and rows[0]["threshold"] == "ACTUAL > 80 % of 300 USD"


def test_summary_counts():
    rows = [cat._row(id="a", name="a", app="cis", type="metric", severity="page", state="ALARM", source="cloudwatch"),
            cat._row(id="b", name="b", app="cis", type="in_app_check", severity="ticket", state="OK", source="cis"),
            cat._row(id="c", name="c", app="crm", type="eventbridge", severity=None, source="eventbridge",
                     coverage_gap="No Severity tag")]
    s = cat.summarize(rows, [{"key": "x", "text": "y"}])
    assert s["total"] == 3 and s["by_type"]["metric"] == 1 and s["by_app"] == {"cis": 2, "crm": 1}
    assert s["by_severity"] == {"page": 1, "ticket": 1, "untagged": 1} and s["firing"] == 1 and s["page_firing"] == 1
    assert s["gaps"] == 2


def test_build_with_peers_and_without_redis(monkeypatch):
    """No Redis: the AWS section is cache_offline (no AWS call), the peers are read with the staff token, the CIS
    checks and the budgets are listed."""
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path == "/booking/ops/checks":
            return httpx.Response(200, json={"app": "booking", "checks": [{"key": "jobs_stuck", "title": "x"}]})
        if request.url.path.endswith("/ops-checks"):
            return httpx.Response(503, json={"detail": "crm_unavailable"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cat, "_peer_transport", transport)
    monkeypatch.setattr(crm_console, "_transport", transport)
    monkeypatch.setattr(cat, "_aws_clients", lambda: pytest.fail("no AWS call without the cache"))

    async def infra():
        return {"groups": [], "doc": {"missing": True}}

    from api.services import platform_infra
    monkeypatch.setattr(platform_infra, "read_infra", infra)
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"iss": "https://issuer.example"}).encode()).decode().rstrip("=")
    auth = f"Bearer x.{payload}.y"
    doc = asyncio.run(cat.build(None, None, auth))
    assert doc["freshness"]["aws"]["status"] == "unavailable" and doc["freshness"]["booking"]["status"] == "ok"
    assert doc["freshness"]["crm"]["status"] == "unavailable"
    sources = {r["source"] for r in doc["rows"]}
    assert sources == {"cis", "booking", "budgets"}
    view = cat.summary_view(doc)
    assert view["summary"]["total"] == len(doc["rows"]) and "rows" not in view


def test_routes_are_ops_read():
    assert POLICY[("GET", "/admin/platform/alerts/catalogue")] == OPS_READ
    assert POLICY[("GET", "/admin/platform/alerts/summary")] == OPS_READ
    assert crm_console.is_allowed("GET", "/ops-checks")
