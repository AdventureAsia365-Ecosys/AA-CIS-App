"""AA-1003 R4 part 2 — TP-01 (services/reports/planner.py), SEC-03 / SEC-08 (services/reports/security.py), DEV-01 /
DEV-03 / OPS-15 (services/reports/delivery.py). No DB, no AWS, no paid call.

Each renderer on fixture facts lists every KPI its registry entry names, a part that could not be read is a line in
the report (never an exception), the sources the worker cannot read are named "Source not available", and the
computations follow the kpis.yaml definitions.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from services.reports import delivery, generate, periods, planner, registry, security

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
MON, SUN = date(2026, 10, 26), date(2026, 11, 1)
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)
R4P2 = {"TP-01": planner, "SEC-03": security, "SEC-08": security, "DEV-01": delivery, "DEV-03": delivery,
        "OPS-15": delivery}


def _reg():
    return registry.load()


def _kpis(out) -> dict:
    return {k["id"]: k for k in out.output["kpis"]}


def _ts(day: date, hour: int = 3, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _assert_lists_registry_kpis(out, rid: str):
    assert [k["id"] for k in out.output["kpis"]] == list(_reg().report(rid).kpis), rid
    assert out.output["report_id"] == rid and out.markdown.startswith("# ")


# ── registry and dispatch ───────────────────────────────────────────────────────────────────────────────────────

def test_r4_part2_reports_are_generated_here_with_their_schedules():
    reg = _reg()
    for rid in R4P2:
        r = reg.report(rid)
        assert r.runnable and rid in generate.RENDERERS, rid
        assert r.automated_from == date(2026, 10, 31), rid
    assert {reg.report(r).schedule for r in ("TP-01", "DEV-03")} == {"weekly"}
    assert {reg.report(r).schedule for r in ("SEC-03", "SEC-08", "DEV-01")} == {"monthly"}
    assert reg.report("OPS-15").schedule == "event"
    assert reg.report("DEV-01").status == "partial"
    # first scheduled periods are expected (chain days on or after automated_from), not overdue for older periods
    assert generate.due_state(reg.report("DEV-03"), None, datetime(2026, 10, 31, 3, tzinfo=UTC))["state"] \
        == "pending_first"


@pytest.mark.parametrize("rid", sorted(R4P2))
def test_generate_dispatches_to_the_r4_part2_modules(monkeypatch, rid):
    seen = []

    async def fake(conn, reg, report, first, last, now):
        seen.append(report.id)
        return generate.Rendered("s", {}, "m")
    monkeypatch.setattr(R4P2[rid], "render", fake)
    reg = _reg()
    asyncio.run(generate.render(None, reg, reg.report(rid), MON, SUN, NOW))
    assert seen == [rid]


@pytest.mark.parametrize("rid", sorted(R4P2))
def test_module_render_refuses_a_report_it_does_not_own(rid):
    other = "CIS-01"
    with pytest.raises(LookupError):
        asyncio.run(R4P2[rid].render(None, _reg(), _reg().report(other), MON, SUN, NOW))


# ── TP-01 TripPlanner funnel ─────────────────────────────────────────────────────────────────────────────────────

def _traffic(first: date) -> dict:
    rows = []
    for w in range(5):                                   # four weeks before + the report week
        monday = first - timedelta(weeks=4 - w)
        for metric, n in (("planner_session_started", 40 + w * 10), ("trip_started", 10 + w), ("trip_edited", 4),
                          ("inquiry_sent", 1 + (w == 4))):
            rows.append({"day": monday + timedelta(days=2), "metric": metric, "n": n})
    rows.append({"day": first, "metric": "something_new", "n": 99})        # unknown metrics are ignored
    return {"rows": rows}


def test_tp01_funnel_conversions_and_trend():
    facts = {"traffic": _traffic(MON), "searches": [{"day": MON, "n": 7}, {"day": MON - timedelta(days=7), "n": 3}],
             "spend": [{"day": MON, "stage": "tp_compose", "calls": 4, "cost_usd": 4.2},
                       {"day": MON, "stage": "tp_search_embed", "calls": 7, "cost_usd": 0.01},
                       {"day": MON + timedelta(days=1), "stage": "tp_compose", "calls": 1, "cost_usd": 0.5}]}
    out = planner.render_tp01(_reg(), _reg().report("TP-01"), MON, SUN, facts, NOW)
    _assert_lists_registry_kpis(out, "TP-01")
    k = _kpis(out)
    assert k["K-TP-SESSIONS"]["value"] == 80.0
    assert k["K-TP-S2T"]["value"] == pytest.approx(14 / 80, abs=1e-4)
    assert k["K-TP-T2I"]["value"] == pytest.approx(2 / 14, abs=1e-4)
    # 4.21 USD on 26/10 = 84 % of the default 5 USD cap: red
    assert k["K-TP-LLMCAP"]["value"] == pytest.approx(4.21) and k["K-TP-LLMCAP"]["status"] == "red"
    f = out.output["funnel"]
    assert len(f["weeks"]) == 5 and f["this"]["ai_search"] == 7 and f["previous"]["ai_search"] == 3
    assert "Top pinned components" in out.markdown and "## Not readable from CIS" in out.markdown
    assert "80 sessions, 14 trips started, 2 inquiries sent" in out.summary


def test_tp01_missing_contract_view_and_quiet_week():
    facts = {"traffic": {"error": "contract view tripplanner.v_ops_traffic_daily not readable (X)"},
             "searches": [], "spend": []}
    out = planner.render_tp01(_reg(), _reg().report("TP-01"), MON, SUN, facts, NOW)
    k = _kpis(out)
    assert {k[i]["status"] for i in ("K-TP-SESSIONS", "K-TP-S2T", "K-TP-T2I")} == {"no_data"}
    assert k["K-TP-LLMCAP"]["status"] == "ok" and k["K-TP-LLMCAP"]["value"] == 0.0
    assert "not readable" in out.summary
    assert len(out.output["llm_spend"]["days"]) == 7                     # every day of the week, zeros included
    # a week with no session: ratios are no_data, not a division by zero
    empty = planner.render_tp01(_reg(), _reg().report("TP-01"), MON, SUN, {"traffic": {"rows": []}, "searches": [],
                                                                          "spend": {"error": "boom"}}, NOW)
    ke = _kpis(empty)
    assert ke["K-TP-S2T"]["status"] == "no_data" and ke["K-TP-LLMCAP"]["status"] == "no_data"


def test_tp01_reads_the_contract_view_and_the_ledger_only():
    sql = planner.SPEND_SQL + planner.activity.TP_VIEW_SQL + planner.activity.TP_SEARCH_SQL
    assert "tripplanner.v_ops_traffic_daily" in sql and "shared.llm_call_log" in sql
    assert "tripplanner.llm_spend_daily" not in sql and "tripplanner.trip" not in sql


# ── SEC-08 privileged actions ────────────────────────────────────────────────────────────────────────────────────

CHAIN_OK = {"checked": 3100, "ok": True, "chain_ok": True, "first_bad_seq": None, "problem": None, "last_seq": 3100,
            "from_seq": None, "pre_chain_rows": 40, "unchained_after_genesis": 0,
            "chain_boundary": "2026-10-16T00:00:00"}


def _sec_facts(**over) -> dict:
    facts = {
        "chain": CHAIN_OK,
        "actions": [{"actor_type": "staff", "action": "staff.request", "method": "POST", "status_class": "2xx/3xx",
                     "n": 120}],
        "directory": [{"action": "staff.roles_changed", "actor": "admin:1", "resource_id": "u-2",
                       "created_at": "2026-10-12T02:00:00+00:00"}],
        "refused": [{"actor": "admin:9", "status": 403, "n": 4, "last_at": "2026-10-20T01:00:00+00:00"}],
        # 16:00 UTC = 23:00 ICT (off hours); 03:00 UTC = 10:00 ICT (office hours)
        "hourly": [{"actor": "admin:1", "hour": _ts(date(2026, 10, 5), 16), "n": 80},
                   {"actor": "machine", "hour": _ts(date(2026, 10, 6), 3), "n": 300}],
    }
    facts.update(over)
    return facts


def test_sec08_chain_directory_refusals_and_off_hours():
    out = security.render_sec08(_reg(), _reg().report("SEC-08"), OCT1, OCT31, _sec_facts(), NOW)
    _assert_lists_registry_kpis(out, "SEC-08")
    k = _kpis(out)
    assert k["K-AUD-CHAIN"]["value"] == "pass" and k["K-AUD-CHAIN"]["status"] == "ok"
    assert k["K-AUD-BREAKGLASS"]["status"] == "not wired" and k["K-AUD-OVERRIDES"]["status"] == "not wired"
    assert out.output["off_hours"] == [{"actor": "admin:1", "hour_ict": "2026-10-05 23:00", "calls": 80}]
    assert "staff.roles_changed" in out.markdown and "admin:9" in out.markdown
    assert "1 staff directory change(s), 1 off-hours bulk hour(s)" in out.summary


def test_sec08_broken_chain_is_red_and_unreadable_parts_are_lines():
    broken = {**CHAIN_OK, "ok": False, "chain_ok": True, "unchained_after_genesis": 2}
    out = security.render_sec08(_reg(), _reg().report("SEC-08"), OCT1, OCT31,
                                _sec_facts(chain=broken, actions={"error": "boom"}, hourly={"error": "x"}), NOW)
    k = _kpis(out)
    assert k["K-AUD-CHAIN"]["value"] == "fail" and k["K-AUD-CHAIN"]["status"] == "red"
    assert "2 row(s) without a chain_seq" in k["K-AUD-CHAIN"]["note"]
    assert "Not readable: boom" in out.markdown and out.output["sources_unreadable"]["actions"] == "boom"


def test_sec08_off_hours_window_is_ict():
    rows = [{"actor": "a", "hour": _ts(date(2026, 10, 5), h), "n": 60} for h in range(24)]
    got = {r["hour_ict"][-5:-3] for r in security.off_hours(rows)}
    assert got == {"22", "23", "00", "01", "02", "03", "04", "05"}


def test_sec08_sql_reads_the_audit_log_by_time_only():
    for sql in (security.ACTIONS_SQL, security.DIRECTORY_SQL, security.REFUSED_SQL, security.HOURLY_SQL):
        assert "acp_shared.audit_log" in sql and "created_at >= $1 AND created_at < $2" in sql
    assert "password_hash IS NOT NULL" in security.STAFF_SQL and "email" not in security.STAFF_SQL


# ── SEC-03 security hygiene ──────────────────────────────────────────────────────────────────────────────────────

def test_sec03_cis_facts_actions_and_named_sources():
    facts = _sec_facts(staff={"active": 9, "owners": 2, "password_logins": 1, "stale": 3, "not_linked": 0,
                              "disabled": 2},
                       probes=[{"kpi_id": "K-DATA-PROBES", "scope": "all", "day": date(2026, 10, 3), "value": 0.5},
                               {"kpi_id": "K-DATA-PROBES", "scope": "all", "day": date(2026, 10, 4), "value": 1}])
    out = security.render_sec03(_reg(), _reg().report("SEC-03"), OCT1, OCT31, facts, NOW)
    _assert_lists_registry_kpis(out, "SEC-03")
    assert {k["status"] for k in out.output["kpis"]} == {"not wired"}
    assert "Source not available" in out.markdown and "Dependabot" in out.markdown
    acts = out.output["actions"]
    assert any("password hash" in a for a in acts) and any("90 days" in a for a in acts)
    assert any("1 day(s)" in a for a in acts)
    assert out.output["probes"] == {"days": 2, "red_days": ["2026-10-03"]}
    assert "(draft)" in out.summary and "audit chain pass" in out.summary


def test_sec03_broken_chain_is_the_first_action():
    facts = _sec_facts(chain={**CHAIN_OK, "ok": False, "problem": "row_hash does not match", "first_bad_seq": 7},
                       staff={"error": "x"}, probes={"error": "y"})
    out = security.render_sec03(_reg(), _reg().report("SEC-03"), OCT1, OCT31, facts, NOW)
    assert out.output["actions"][0].startswith("Audit chain broken")
    assert "Not readable: x" in out.markdown and "Not readable: y" in out.markdown


# ── deploys (DEV-01 / DEV-03) ────────────────────────────────────────────────────────────────────────────────────

def _start(sha: str, day: date, hour: int = 3, rev: int = 1) -> dict:
    return {"git_sha": sha, "task_revision": rev, "started_at": _ts(day, hour)}


def test_deploys_count_commit_changes_and_rollbacks_inside_the_window():
    start, end = periods.range_bounds(MON, SUN)
    rows = [_start("aaa", date(2026, 10, 24)),             # before the window: the commit live at its start
            _start("aaa", MON, 1),                         # a restart of the same commit: not a deploy
            _start("bbb", MON, 5, 2), _start("bbb", MON, 6, 2),
            _start("unknown", date(2026, 10, 27)),         # a worker without a commit: ignored
            _start("ccc", date(2026, 10, 28), 2, 3),
            _start("bbb", date(2026, 10, 29), 2, 4),       # back to bbb: rollback
            _start("ddd", date(2026, 11, 2), 2, 5)]        # after the window
    dl = delivery.deploys(rows, start, end)
    assert [(d["git_sha"], d["previous"], d["rollback"]) for d in dl] == [
        ("bbb", "aaa", False), ("ccc", "bbb", False), ("bbb", "ccc", True)]
    assert delivery.deploys({"error": "x"}, start, end) is None
    # no row before the window: the first start has no known predecessor and is not counted
    assert delivery.deploys([_start("bbb", MON, 5)], start, end) == []


def test_dev03_lists_deploys_schema_changes_and_unavailable_sources():
    facts = {"starts": [_start("aaa", date(2026, 10, 24)), _start("bbb", MON, 5, 2)],
             "versions": [{"version": "311", "description": "x", "applied_at": _ts(date(2026, 10, 27))},
                          {"version": "bk-036", "description": "y", "applied_at": _ts(date(2026, 10, 20))}]}
    out = delivery.render_dev03(_reg(), _reg().report("DEV-03"), MON, SUN, facts, NOW)
    _assert_lists_registry_kpis(out, "DEV-03")
    assert {k["status"] for k in out.output["kpis"]} == {"not wired"}
    assert [d["git_sha"] for d in out.output["cis_deploys"]] == ["bbb"]
    assert out.output["schema"]["owners"]["cis"]["applied"] == 1
    assert "1 CIS deploy(s), 1 migration(s) registered" in out.summary
    assert "plan.json" in out.markdown and "Source not available" in out.markdown
    json.dumps(out.output, default=str)                                   # storable as report_run.output


def test_dev01_merges_weekly_outputs_with_the_rows_still_kept():
    weekly = [{"period_start": date(2026, 9, 28), "period_end": date(2026, 10, 4),
               "cis_deploys": [{"git_sha": "s1", "at": "2026-09-30T03:00:00+00:00", "rollback": False},
                               {"git_sha": "s2", "at": "2026-10-02T03:00:00+00:00", "rollback": False}]},
              {"period_start": date(2026, 10, 5), "period_end": date(2026, 10, 11),
               "cis_deploys": [{"git_sha": "s3", "at": "2026-10-06T03:00:00+00:00", "rollback": True}]}]
    starts = [_start("s4", date(2026, 10, 26)), _start("s5", date(2026, 10, 27)), _start("s5", date(2026, 10, 28))]
    facts = {"weekly": weekly, "starts": starts, "versions": []}
    md = delivery.month_deploys(facts, OCT1, OCT31)
    assert [d["git_sha"] for d in md["deploys"]] == ["s2", "s3", "s5"]     # s1 is September
    assert md["covered_days"] == 11 + 6 and md["days"] == 31               # 1-11/10 weekly, 26-31/10 worker rows
    out = delivery.render_dev01(_reg(), _reg().report("DEV-01"), OCT1, OCT31, facts, NOW)
    _assert_lists_registry_kpis(out, "DEV-01")
    k = _kpis(out)
    assert k["K-DORA-DF"]["value"] == pytest.approx(round(3 / (31 / 7), 2))
    assert "14 of 31 days not covered" in k["K-DORA-DF"]["note"]
    assert k["K-DORA-CFR"]["value"] == pytest.approx(1 / 3, abs=1e-4) and k["K-DORA-CFR"]["status"] == "amber"
    assert {k[i]["status"] for i in ("K-DORA-LT", "K-DORA-MTTR", "K-CI-MAINFAIL")} == {"not wired"}


def test_dev01_with_nothing_covered_is_no_data():
    out = delivery.render_dev01(_reg(), _reg().report("DEV-01"), OCT1, OCT31,
                                {"weekly": {"error": "x"}, "starts": {"error": "y"}, "versions": {"error": "z"}}, NOW)
    k = _kpis(out)
    assert k["K-DORA-DF"]["status"] == "no_data" and k["K-DORA-CFR"]["status"] == "no_data"
    assert "migrations not read" in out.summary


# ── OPS-15 state snapshot ────────────────────────────────────────────────────────────────────────────────────────

class FakeEcs:
    def __init__(self):
        self.calls = []

    def describe_services(self, cluster, services):
        self.calls.append(("services", cluster, tuple(services)))
        out = []
        for i, name in enumerate(services[:4]):
            out.append({"serviceName": name, "status": "ACTIVE", "desiredCount": 1, "runningCount": 1,
                        "pendingCount": 0, "taskDefinition": f"arn:aws:ecs:us-west-1:123456789012:task-definition/"
                                                            f"{name}:{900 + i}",
                        "deployments": [{"status": "PRIMARY", "rolloutState": "COMPLETED"}]})
        return {"services": out, "failures": [{"arn": f"arn:aws:ecs:us-west-1:123456789012:service/{services[4]}"}]}

    def describe_task_definition(self, taskDefinition):
        self.calls.append(("td", taskDefinition))
        fam = taskDefinition.rsplit("/", 1)[-1].split(":")[0]
        return {"taskDefinition": {"containerDefinitions": [
            {"name": "api", "image": f"123456789012.dkr.ecr.us-west-1.amazonaws.com/{fam}:dev-1878349b"}]}}


def test_describe_ecs_keeps_no_account_id_and_calls_once_per_task_definition():
    fake = FakeEcs()
    got = delivery.describe_ecs(fake, "dev")
    assert got["cluster"] == "aa-cis-dev-cluster" and len(got["services"]) == 4
    assert got["missing"] == ["aa-crm-dev-web"]
    s = got["services"][0]
    assert s["task_definition"] == "aa-cis-dev-api:900" and s["rollout"] == "COMPLETED"
    assert s["containers"] == [{"container": "api", "repository": "aa-cis-dev-api", "tag": "dev-1878349b"}]
    assert "123456789012" not in json.dumps(got)
    assert sum(1 for c in fake.calls if c[0] == "services") == 1 and sum(1 for c in fake.calls if c[0] == "td") == 4


def _ops15_facts() -> dict:
    return {
        "ecs": delivery.describe_ecs(FakeEcs(), "dev"),
        "apps": {"items": [{"id": "cis", "status": "ok", "git_sha": "1878349babcdef", "reason": "ok",
                            "schema_version": "310"},
                           {"id": "tripplanner", "status": "no_probe", "reason": "no health endpoint yet"}],
                 "workers": {"alive": 1, "commits": {"1878349babcdef": 1}, "same_commit_as_api": True}},
        "versions": [{"version": "310", "description": "", "applied_at": _ts(date(2026, 10, 30))},
                     {"version": "bk-035", "description": "", "applied_at": _ts(date(2026, 10, 29))},
                     {"version": "bk-9", "description": "", "applied_at": _ts(date(2026, 10, 1))}],
        "jobs": [{"kind": "place_relocate", "status": "queued", "n": 1, "oldest": "2026-10-31T00:12:00+00:00"}],
        "waves": [],
    }


def test_ops15_snapshot_coverage_and_machine_readable_state():
    out = delivery.render_ops15(_reg(), _reg().report("OPS-15"), MON, SUN, _ops15_facts(), NOW)
    _assert_lists_registry_kpis(out, "OPS-15")
    snap = out.output["snapshot"]
    # ecs (one service missing -> not captured), lambda, vercel, PRs and other job tables are not captured
    assert set(snap["missing"]) == {"ecs_services", "other_job_tables", "lambda_versions", "vercel_commits",
                                    "open_prs"}
    k = _kpis(out)
    assert k["K-SNAP-COVERAGE"]["value"] == pytest.approx(5 / 10) and k["K-SNAP-COVERAGE"]["status"] == "amber"
    assert k["K-SNAP-DRIFT"]["status"] == "not wired" and "same commit" in k["K-SNAP-DRIFT"]["note"]
    assert snap["state"]["migrations"]["booking"]["version"] == "bk-035"   # bk-035 > bk-9 by number
    assert "place_relocate" in out.markdown and "dev-1878349b" in out.markdown
    assert "CIS jobs active 1, open waves 0" in out.summary
    json.dumps(out.output, default=str)


def test_ops15_unreadable_parts_are_lines():
    facts = {"ecs": {"error": "not permitted (ecs:DescribeServices)"}, "apps": {"error": "a"},
             "versions": {"error": "b"}, "jobs": {"error": "c"}, "waves": {"error": "d"}}
    out = delivery.render_ops15(_reg(), _reg().report("OPS-15"), MON, SUN, facts, NOW)
    assert out.output["snapshot"]["captured"] == []
    assert _kpis(out)["K-SNAP-COVERAGE"]["value"] == 0.0
    for why in ("not permitted (ecs:DescribeServices)", "Not readable: a", "Not readable: b", "Not readable: c"):
        assert why in out.markdown


def test_ops15_ecs_state_says_why_when_refused(monkeypatch):
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    def boom(*a, **k):
        raise Denied("no")
    monkeypatch.setattr(delivery, "describe_ecs", boom)
    assert asyncio.run(delivery.ecs_state()) == {"error": "not permitted (ecs:DescribeServices)"}
