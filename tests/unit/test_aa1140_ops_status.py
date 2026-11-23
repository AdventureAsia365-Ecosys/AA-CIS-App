"""AA-1140 (S289) — the Ecosystem status page backend without a database or AWS: the time range and its steps, the
CloudWatch series parse, the sampler's pure helpers and one tick against fakes, the uptime strip, the section shapes,
the verdict, the job kind's schedule and the route (policy, 422 on a bad range). Invented names only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.ops_status import cw_series, ranges, sampler, sections, store, verdict
from api.services.ops_status.ranges import RangeError, TimeRange
from shared import ops_status_thresholds as th

NOW = datetime(2026, 11, 24, 2, 7, 30, tzinfo=timezone.utc)
_SECRET = "aa1140-test-secret"


# ── ranges ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,span_h,step", [("1h", 1, 60), ("6h", 6, 300), ("24h", 24, 3600), ("7d", 168, 21600)])
def test_presets_end_at_the_next_step_and_carry_their_step(key, span_h, step):
    r = ranges.resolve(key, None, None, now=NOW, history_days=14)
    assert r.step_s == step and r.span == timedelta(hours=span_h)
    assert r.end > NOW and r.end - NOW <= timedelta(seconds=step)
    assert int(r.end.timestamp()) % step == 0
    assert len(r.buckets()) == int(span_h * 3600 / step)


def test_previous_period_has_the_same_length():
    r = ranges.resolve("6h", None, None, now=NOW, history_days=14)
    p = r.previous()
    assert p.end == r.start and p.span == r.span and p.step_s == r.step_s


def test_custom_range_is_aligned_and_checked():
    r = ranges.resolve("custom", "2026-11-23T01:03:00+07:00", "2026-11-23T05:00:00+07:00", now=NOW, history_days=14)
    assert r.key == "custom" and r.step_s == 300 and r.start == datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
    for a, b, why in (("2026-11-23T05:00:00Z", "2026-11-23T01:00:00Z", "after"),
                      ("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", "kept"),
                      ("2026-11-24T00:00:00Z", "2026-11-25T00:00:00Z", "future"),
                      ("2026-11-23T00:00:00", "2026-11-23T05:00:00Z", "zone"),
                      ("2026-11-23T00:00:00Z", "2026-11-23T00:05:00Z", "10 minutes")):
        with pytest.raises(RangeError, match=why):
            ranges.resolve("custom", a, b, now=NOW, history_days=14)
    with pytest.raises(RangeError):
        ranges.resolve("30d", None, None, now=NOW, history_days=14)
    with pytest.raises(RangeError):
        ranges.resolve("custom", None, None, now=NOW, history_days=14)


def test_step_follows_the_span():
    assert [ranges.step_for(timedelta(minutes=m)) for m in (60, 90, 91, 480, 481, 4320, 4321)] == \
        [60, 60, 300, 300, 3600, 3600, 21600]


# ── CloudWatch series ──────────────────────────────────────────────────────────────────────────────────────────

def _rng() -> TimeRange:
    start = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    return TimeRange("custom", start, start + timedelta(hours=3), 3600)


def test_queries_cover_apis_functions_and_the_database_but_not_the_crm():
    qs = cw_series.queries("dev", _rng())
    ids = {q["Id"] for q in qs}
    assert {"s_cis_0_requests", "s_cis_0_errors", "s_cis_0_client_errors", "s_cis_0_p95", "t_cis_0_p95",
            "s_booking_0_requests", "s_tripplanner_1_requests", "r_cpu", "r_connections", "r_free"} <= ids
    assert not any("_crm_" in i for i in ids)
    whole = next(q for q in qs if q["Id"] == "t_cis_0_p95")
    assert whole["MetricStat"]["Period"] == 3 * 3600 and whole["MetricStat"]["Stat"] == "p95"
    assert all(len(q["Id"]) <= 255 and q["Id"][0].islower() for q in qs)


def test_parse_lays_values_on_steps_sums_functions_and_marks_the_crm_unmeasured():
    r = _rng()
    t0, t1 = r.start, r.start + timedelta(hours=1)
    res = [
        {"Id": "s_cis_0_requests", "Timestamps": [t0, t1], "Values": [100.0, 50.0]},
        {"Id": "s_cis_0_errors", "Timestamps": [t1], "Values": [2.0]},
        {"Id": "s_cis_0_client_errors", "Timestamps": [], "Values": []},
        {"Id": "s_cis_0_p95", "Timestamps": [t0], "Values": [900.0]},
        {"Id": "t_cis_0_p95", "Timestamps": [t0], "Values": [1200.0]},
        {"Id": "s_tripplanner_0_requests", "Timestamps": [t0], "Values": [3.0]},
        {"Id": "s_tripplanner_1_requests", "Timestamps": [t0], "Values": [4.0]},
        {"Id": "r_free", "Timestamps": [t0], "Values": [20 * 1024 ** 3]},
        {"Id": "r_cpu", "Timestamps": [t0.isoformat()], "Values": [12.5]},
    ]
    out = cw_series.parse(res, "dev", r)
    cis = out["apps"]["cis"]
    assert cis["requests"] == [100.0, 50.0, None] and cis["errors"] == [None, 2.0, None]
    assert cis["totals"] == {"requests": 150, "errors": 2, "client_errors": 0, "p95_ms": 1200.0}
    assert out["apps"]["tripplanner"]["requests"][0] == 7.0
    assert out["apps"]["crm"] == {"measured": False, "label": "Partner CRM"}
    assert out["rds"]["free_gib"][0] == 20.0 and out["rds"]["cpu_pct"][0] == 12.5 and out["rds"]["measured"]


def test_collect_degrades_on_a_refusal():
    from botocore.exceptions import ClientError

    client = MagicMock()
    client.get_metric_data.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetMetricData")
    out = cw_series.collect(_rng(), env="dev", client=client)
    assert out["status"] == "unavailable" and out["reason"] == "not_permitted"


# ── sampler helpers ────────────────────────────────────────────────────────────────────────────────────────────

def _svc(desired=1, running=1, rollout="COMPLETED", deployments=None):
    return {"serviceName": "aa-cis-dev-api", "desiredCount": desired, "runningCount": running, "pendingCount": 0,
            "taskDefinition": "arn:aws:ecs:zz:task-definition/aa-cis-dev-api:41",
            "deployments": deployments if deployments is not None else
            [{"id": "ecs-svc/1", "status": "PRIMARY", "rolloutState": rollout, "taskDefinition": "td:41",
              "createdAt": NOW - timedelta(minutes=10), "updatedAt": NOW - timedelta(minutes=5)}],
            "images": [{"container": "api", "repository": "aa-cis-dev-api", "tag": "dev-zz11"}]}


@pytest.mark.parametrize("desired,running,rollout,state", [
    (1, 1, "COMPLETED", "ok"), (1, 1, "IN_PROGRESS", "warn"), (2, 1, "COMPLETED", "degraded"),
    (1, 1, "FAILED", "degraded"), (1, 0, "COMPLETED", "down"), (0, 0, "COMPLETED", "down")])
def test_service_state(desired, running, rollout, state):
    assert sampler.service_state(_svc(desired, running, rollout))[0] == state


def test_service_state_unknown_when_missing():
    assert sampler.service_state(None) == ("unknown", "not found in the cluster")


def test_deploy_events_carry_the_deployment_id_and_end_when_completed():
    ev = sampler.deploy_events("cis", "api", "aa-cis-dev-api", _svc())
    assert len(ev) == 1 and ev[0]["dedupe_key"] == "deploy:ecs-svc/1" and ev[0]["ended_at"] is not None
    assert "dev-zz11" in ev[0]["title"] and "rev 41" in ev[0]["title"]
    failed = sampler.deploy_events("cis", "api", "aa-cis-dev-api", _svc(rollout="FAILED"))
    assert [e["kind"] for e in failed] == ["deploy", "rollout_failed"] and failed[1]["severity"] == "sev2"
    running = sampler.deploy_events("cis", "api", "aa-cis-dev-api", _svc(rollout="IN_PROGRESS"))
    assert running[0]["ended_at"] is None


def test_alarm_helpers():
    alarms = {"crm": {"firing": [{"name": "aa-crm-dev-zz-p95", "severity": "page", "since": NOW.isoformat()}]},
              "cis": {"firing": []}, "shared": {"firing": [{"name": "aa-ecosys-dev-zz", "severity": None}]}}
    firing = sampler.alarm_firing(alarms)
    assert [f["app"] for f in firing] == ["crm", "shared"]
    assert sampler.alarm_severity("page") == "sev2" and sampler.alarm_severity("ticket") == "sev3"
    assert sampler.alarm_severity(None) == "sev3"


def test_fit_cuts_lists_to_the_check():
    big = {"firing": [{"name": "zz-" + "x" * 100} for _ in range(200)], "total": 200}
    out = sampler.fit(big)
    assert out["truncated"] is True and len(json.dumps(out).encode()) <= 8192
    assert sampler.fit({"a": 1}) == {"a": 1}


def test_queue_and_pct_states():
    assert sampler.queue_state(60, 0, 15, 60) == "down"
    assert sampler.queue_state(16 * 60, 1, 15, 60) == "warn"
    assert sampler.queue_state(61 * 60, 2, 15, 60) == "degraded"
    assert sampler.queue_state(None, 1, 15, 60) == "unknown"
    assert sampler.pct_state(96, 80, 95) == "degraded" and sampler.pct_state(None, 80, 95) == "unknown"


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    async def executemany(self, sql, rows):
        self.sink.append((sql, list(rows)))


class _Acquire:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return _Conn(self.sink)

    async def __aexit__(self, *a):
        return False


def _pool():
    sink: list = []
    pool = MagicMock()
    pool.acquire = lambda: _Acquire(sink)
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.fetchrow = AsyncMock(return_value=None)
    return pool, sink


@pytest.mark.asyncio
async def test_ecs_part_writes_samples_and_deploy_events(monkeypatch):
    pool, sink = _pool()
    described = {"aa-cis-dev-api": _svc(), "aa-cis-dev-worker": {**_svc(running=0), "serviceName": "w"},
                 "__missing__": ["aa-booking-dev-admin"]}
    monkeypatch.setattr(sampler, "describe_services", lambda env, client=None: described)
    out: dict = {}
    await sampler.ecs_part(pool, "dev", NOW, "00000000-0000-0000-0000-0000000000aa", out)
    sample_rows = next(rows for sql, rows in sink if "ops_sample" in sql)
    assert len(sample_rows) == len(sampler.SERVICES)
    states = {r[2]: r[4] for r in sample_rows}
    assert states["aa-cis-dev-api"] == "ok" and states["aa-cis-dev-worker"] == "down"
    assert states["aa-crm-dev-web"] == "unknown"
    event_rows = next(rows for sql, rows in sink if "ops_event" in sql)
    kinds = sorted(r[2] for r in event_rows)
    assert kinds.count("deploy") == 2 and "service_down" in kinds
    assert out["ecs_missing"] == ["aa-booking-dev-admin"]


@pytest.mark.asyncio
async def test_tick_never_raises_and_names_the_failed_part(monkeypatch):
    pool, _ = _pool()

    async def boom(*a, **k):
        raise RuntimeError("zz down")

    for name in ("ecs_part", "alarms_part", "health_part", "queue_part", "database_part"):
        monkeypatch.setattr(sampler, name, boom)
    out = await sampler.tick(pool, NOW, env="dev")
    assert {k for k in out if k.endswith("_error")} == {"ecs_error", "alarms_error", "health_error", "queue_error",
                                                         "database_error"}


# ── uptime strip and series ────────────────────────────────────────────────────────────────────────────────────

def test_strip_counts_up_down_and_unmeasured_and_the_budget():
    r = _rng()   # 3 h -> 5-minute cells would be 36: cell 5 min
    ss = [{"sampled_at": r.start + timedelta(minutes=5 * i), "state": "ok"} for i in range(30)]
    ss += [{"sampled_at": r.start + timedelta(minutes=5 * i), "state": "down"} for i in range(30, 32)]
    ss += [{"sampled_at": r.start + timedelta(minutes=5 * 33), "state": "unknown"}]
    out = store.strip(ss, r, 99.5)
    assert out["cell_minutes"] == 5 and len(out["cells"]) == 36
    assert out["cells"][0] == "ok" and out["cells"][30] == "down" and out["cells"][33] == "none"
    assert out["uptime_pct"] == round(100 * 30 / 32, 3) and out["down_min"] == 10 and out["below_slo"] is True
    assert out["budget_left_pct"] < 0
    empty = store.strip([], r, 99.5)
    assert empty["uptime_pct"] is None and empty["budget_left_pct"] is None and set(empty["cells"]) == {"none"}


def test_cell_length_keeps_at_most_48_cells():
    for key in ("1h", "6h", "24h", "7d"):
        r = ranges.resolve(key, None, None, now=NOW, history_days=14)
        assert r.span.total_seconds() / 60 / store.cell_minutes(r) <= 48


def test_step_series_takes_the_max_per_step():
    r = _rng()
    ss = [{"sampled_at": r.start + timedelta(minutes=5), "value": {"w": 30}},
          {"sampled_at": r.start + timedelta(minutes=50), "value": {"w": 90}},
          {"sampled_at": r.start + timedelta(minutes=70), "value": json.dumps({"w": 6})},
          {"sampled_at": r.start + timedelta(minutes=80), "value": {"w": "x"}}]
    assert store.step_series(ss, r, "w", scale=1 / 60) == [1.5, 0.1, None]


# ── sections ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_level_both_directions():
    assert sections.level(None, 1, 5) == "none"
    assert [sections.level(v, 1, 5) for v in (0.5, 1, 5)] == ["ok", "warn", "crit"]
    assert [sections.level(v, 15, 10, below=True) for v in (20, 15, 9)] == ["ok", "warn", "crit"]


def test_app_signal_levels_use_the_settings_and_the_minimum():
    lv = sections.app_signal_levels("cis", {"requests": 2000, "errors": 30, "client_errors": 300, "p95_ms": 3200})
    assert lv["errors_level"] == "warn" and lv["client_errors_level"] == "warn" and lv["p95_level"] == "crit"
    few = sections.app_signal_levels("cis", {"requests": 20, "errors": 5, "client_errors": 0, "p95_ms": 100})
    assert few["errors_level"] == "few" and few["few_requests"] is True
    tp = sections.app_signal_levels("tripplanner", {"requests": 200, "errors": 0, "client_errors": None,
                                                    "p95_ms": 7000})
    assert tp["p95_level"] == "warn" and tp["client_error_rate_pct"] is None


def test_app_tiles_mark_the_crm_traffic_not_measured_and_never_green_without_a_card():
    sig = {"status": "ok", "apps": {"cis": {"measured": True, "totals": {"requests": 100, "errors": 0,
                                                                          "client_errors": 1, "p95_ms": 200}},
                                    "crm": {"measured": False}}}
    cards = {"cis": {"overall": {"status": "ok"}, "api": {"git_sha": "zz12"}, "workers": [{}],
                     "schema": {"status": "ok"}}}
    tiles = {t["app"]: t for t in sections.app_tiles(sig, cards, {}, {})}
    assert tiles["cis"]["traffic"]["status"] == "ok" and tiles["cis"]["deployed_sha"] == "zz12"
    assert tiles["cis"]["ready"] == "ok" and tiles["cis"]["workers"] == 1
    assert tiles["crm"]["traffic"]["status"] == sections.NOT_MEASURED
    assert tiles["booking"]["overall"]["status"] == "unknown"
    assert tiles["booking"]["activity"]["status"] == sections.NOT_MEASURED


def test_runtime_rows_turn_old_samples_into_no_sample():
    r = _rng()
    latest = [{"sampled_at": NOW - timedelta(hours=2), "kind": "ecs_service", "subject": "aa-cis-dev-api",
               "app": "cis", "state": "ok", "value": {"role": "api", "running": 1, "desired": 1,
                                                      "image_tag": "dev-zz"}},
              {"sampled_at": NOW, "kind": "app_health", "subject": "booking", "app": "booking", "state": "ok",
               "value": {"reason": "fine", "git_sha": "bb"}}]
    rows = sections.runtime_rows([], latest, r, 99.5, NOW, NOW - timedelta(minutes=15))
    assert [x["state"] for x in rows] == ["no_sample", "ok"]
    assert rows[0]["version"] == "dev-zz" and rows[1]["role"] == "health check"


def test_spend_by_connection_and_caps():
    r = _rng()
    rows = [{"hour": r.start, "provider": "bedrock-zz", "account": "acc-a", "app": "cis", "cost": 1.0,
             "requests": 10, "failed": 1},
            {"hour": r.start + timedelta(hours=1), "provider": "anthropic", "account": None, "app": "tripplanner",
             "cost": 2.5, "requests": 4, "failed": 0}]
    out = sections.spend_by_connection(rows, r)
    assert [c["key"] for c in out["connections"]] == ["anthropic", "bedrock:acc-a"]
    assert out["connections"][1]["error_pct"] == 10.0 and out["connections"][0]["series"] == [0.0, 2.5, 0.0]
    assert out["apps"] == {"cis": 1.0, "tripplanner": 2.5}
    caps = {c["app"]: c for c in sections.app_caps({"cis": 4.5, "tripplanner": 5.0}, {"cis": 5.0,
                                                                                       "tripplanner": 5.0}, 80)}
    assert caps["cis"]["level"] == "warn" and caps["tripplanner"]["level"] == "crit"
    assert caps["crm"]["level"] == "none" and caps["booking"]["spent_usd"] is None


@pytest.mark.asyncio
async def test_ai_section_keeps_the_cap_list_apart_from_the_range_spend(monkeypatch):
    """The range's spend per app must not overwrite today's cap list: the verdict iterates ``apps`` as rows."""
    from api.services import platform_apps

    r = _rng()
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=[
        [{"app": "cis", "spent": 4.5}],
        [{"app": "cis", "cap_usd": 5.0}],
        [{"hour": r.start, "provider": "anthropic", "account": None, "app": "cis", "cost": 1.0, "requests": 2,
          "failed": 0}],
    ])
    monkeypatch.setattr(platform_apps, "crm_llm_usage", AsyncMock(return_value={"status": "ok", "cost_usd": 0.5}))
    out = await sections.ai(pool, r, NOW, None)
    data = out["data"]
    assert isinstance(data["apps"], list) and {a["app"] for a in data["apps"]} == set(sections.APPS)
    assert data["range_by_app"] == {"cis": 1.0} and data["connections"][0]["key"] == "anthropic"
    items = verdict.ai_items(data["apps"])
    assert [i["id"] for i in items] == ["ai-cis"] and items[0]["severity"] == "sev3"


def test_cost_card_sums_ai_and_aws():
    f = {"month": "2026-11", "providers_total": {"mtd_usd": 60.0, "forecast_usd": 100.0},
         "providers": [{"budget_usd": 150.0}, {"budget_usd": None}],
         "aws_accounts": [{"mtd_usd": 200.0, "forecast_usd": 300.0, "budget_usd": 300.0}],
         "complete_days": 23, "days_in_month": 30}
    c = sections.cost_card(f)
    assert (c["spent_usd"], c["forecast_usd"], c["budget_usd"], c["headroom_usd"]) == (260.0, 400.0, 450.0, 50.0)
    assert c["forecast_pct_of_budget"] == 88.9
    assert sections.cost_card({"providers_total": {}, "aws_accounts": []})["budget_usd"] is None


def test_event_rows_merge_sources_newest_first():
    ev = [{"event_id": 1, "occurred_at": NOW - timedelta(hours=3), "ended_at": None, "kind": "alarm",
           "severity": "sev2", "app": "crm", "title": "Alarm: zz", "detail": None},
          {"event_id": 2, "occurred_at": NOW - timedelta(hours=5), "ended_at": NOW - timedelta(hours=4),
           "kind": "deploy", "severity": "info", "app": "cis", "title": "Deploy zz", "detail": None}]
    bells = [{"id": 9, "event_type": "platform.ops_check.jobs_starved", "entity_id": "jobs_starved",
              "payload": json.dumps({"title": "Job queue starved", "message": "zz waited"}), "is_read": True,
              "created_at": NOW - timedelta(hours=1)}]
    fails = [{"kind": "zz_kind", "hour": NOW - timedelta(hours=2), "n": 3, "last_error": "Timeout"}]
    rows = sections.event_rows(ev, bells, fails)
    assert [r["lane"] for r in rows] == ["check", "job", "alarm", "deploy"]
    assert rows[2]["open"] is True and rows[3]["open"] is False and rows[0]["open"] is False
    assert rows[1]["text"] == "3 jobs failed: zz_kind"


def test_guard_rows_passed_failed_missing_and_not_measured():
    kpis = [{"kpi_id": "K-DATA-LIVENESS", "day": NOW.date(), "value": 0.0, "computed_at": NOW - timedelta(hours=2),
             "data_as_of": None},
            {"kpi_id": "K-DATA-STALE", "day": NOW.date(), "value": 2.0, "computed_at": NOW - timedelta(hours=40),
             "data_as_of": None}]
    rows = {g["key"]: g for g in sections.guard_rows(kpis, NOW, 30)}
    assert rows["liveness"]["state"] == "passed" and rows["freshness"]["state"] == "missing"
    assert sum(1 for g in rows.values() if g["state"] == sections.NOT_MEASURED) >= 4
    kpis[1]["computed_at"] = NOW
    assert {g["key"]: g for g in sections.guard_rows(kpis, NOW, 30)}["freshness"]["state"] == "failed"


# ── verdict ────────────────────────────────────────────────────────────────────────────────────────────────────

def _sections(**over):
    base = {
        "apps": {"status": "ok", "data": [{"app": "cis", "label": "CIS", "health": {"state": "ok"},
                                           "traffic": {"status": "ok", "requests": 100, "errors": 0,
                                                       "errors_level": "ok", "p95_level": "ok",
                                                       "client_errors_level": "ok"}}]},
        "runtimes": {"status": "ok", "data": {"rows": []}},
        "jobs": {"status": "ok", "data": {"queues": [], "deploy_gate": {"safe_to_deploy": True}}},
        "resources": {"status": "ok", "data": {"tiles": []}},
        "ai": {"status": "ok", "data": {"apps": []}},
        "delivery": {"status": "ok", "data": {"guards": [], "repos": []}},
    }
    base.update(over)
    return base


def test_verdict_all_normal():
    v = verdict.decide(_sections(), th.snapshot(), [])
    assert v["status"] == "ok" and v["open"] == 0 and v["chips"]["deploy_gate"] == "open"


def test_verdict_orders_by_severity_and_counts_open_only():
    apps = {"status": "ok", "data": [{"app": "crm", "label": "Partner CRM", "health": {"state": "down",
                                                                                       "reason": "503"},
                                      "traffic": {"status": "not_measured"}},
                                     {"app": "cis", "label": "CIS", "health": {"state": "ok"},
                                      "traffic": {"status": "ok", "requests": 2000, "errors": 120,
                                                  "error_rate_pct": 6.0, "errors_level": "crit",
                                                  "p95_level": "warn", "p95_ms": 1800,
                                                  "client_errors_level": "ok"}}]}
    events = [{"id": "e1", "kind": "alarm", "open": True, "severity": "sev3", "app": "shared",
               "text": "Alarm: zz", "t": NOW.isoformat()},
              {"id": "n1", "kind": "check", "open": False, "severity": "sev3", "app": "cis", "check": "jobs_starved",
               "text": "Ops check: Job queue starved", "t": NOW.isoformat()},
              {"id": "n2", "kind": "check", "open": False, "severity": "sev3", "app": "cis", "check": "jobs_starved",
               "text": "Ops check: Job queue starved", "t": NOW.isoformat()}]
    jobs = {"status": "ok", "data": {"queues": [{"id": "shared", "name": "shared.job", "app": "cis", "status": "ok",
                                                 "workers": 1, "level": "warn", "oldest_wait_min": 20.0}],
                                     "deploy_gate": {"safe_to_deploy": False, "blocked_by": ["zz wave"]}}}
    v = verdict.decide(_sections(apps=apps, jobs=jobs), th.snapshot(), events)
    sevs = [i["severity"] for i in v["attention"]]
    assert sevs == sorted(sevs) and sevs[0] == "sev1"
    titles = [i["title"] for i in v["attention"]]
    assert any("6 % server errors, limit 5 %" in t for t in titles)
    assert any("p95 1.80 s, limit 1.50 s" in t for t in titles)
    check = next(i for i in v["attention"] if i["source"] == "Ops check")
    assert check["count"] == 2 and check["status"] == "acked"
    assert v["status"] == "attention" and v["open"] == len(v["attention"]) - 1
    assert v["chips"] == {"alarms_firing": 1, "open_alerts": v["open"], "deploy_gate": "closed",
                          "deploy_gate_blocked_by": ["zz wave"]}
    assert all(i["links"] for i in v["attention"])


def test_resource_items_with_and_without_a_limit():
    tiles = [{"id": "cpu", "label": "Database CPU", "unit": "%", "latest": 93.0, "level": "crit", "warn": 70.0,
              "crit": 90.0, "dir": "above"},
             {"id": "ecs", "label": "ECS tasks running", "latest": 6, "level": "warn", "note": "6 of 7 desired"},
             {"id": "free_storage", "label": "Free storage", "unit": "GiB", "latest": 12.0, "level": "warn",
              "warn": 15.0, "crit": 10.0, "dir": "below"}]
    items = verdict.resource_items(tiles)
    assert [i["severity"] for i in items] == ["sev2", "sev3", "sev3"]
    assert items[0]["title"] == "Database CPU 93.0 %, at or above the critical limit 90"
    assert items[1]["title"] == "ECS tasks running: 6 of 7 desired"
    assert "at or below the warn limit 15" in items[2]["title"]


def test_verdict_unknown_when_nothing_answers():
    down = {"status": "unavailable", "reason": "error"}
    v = verdict.decide(_sections(apps=down, runtimes=down, jobs=down, resources=down, ai=down), th.snapshot(), [])
    assert v["status"] == "unknown" and {"apps", "runtimes", "jobs"} <= set(v["unknown"])


def test_freshness_marks_a_stale_section():
    from api.services import ops_status

    old = {"status": "ok", "as_of": (NOW - timedelta(minutes=20)).isoformat(), "refresh_s": 300}
    assert ops_status.freshness("runtimes", old, NOW, 3.0)["state"] == "stale"
    assert ops_status.freshness("runtimes", {**old, "as_of": NOW.isoformat()}, NOW, 3.0)["state"] == "ok"
    assert ops_status.freshness("jobs", {"status": "unavailable", "reason": "x"}, NOW, 3.0)["state"] == "unavailable"


def test_series_view_rate_per_minute_and_error_share():
    from api.services import ops_status

    r = _rng()
    sig = {"status": "ok", "as_of": NOW.isoformat(),
           "apps": {"cis": {"measured": True, "requests": [120.0, None, 0.0], "errors": [6.0, None, 0.0],
                            "p95_ms": [100.0, None, None]}, "crm": {"measured": False}}}
    v = ops_status.series_view(sig, r)
    assert v["apps"]["cis"]["rate_per_min"] == [2.0, None, 0.0]
    assert v["apps"]["cis"]["error_pct"] == [5.0, None, None] and v["apps"]["crm"] == {"measured": False}


# ── job kind and schedule ──────────────────────────────────────────────────────────────────────────────────────

def test_bucket_key_and_switch(monkeypatch):
    from services.jobs import ops_status_sample_job as job

    assert job.bucket_key(NOW) == "ops_status_sample:2026-11-24T02:05"
    monkeypatch.setenv("OPS_STATUS_SAMPLER", "off")
    assert job.schedule_enabled() is False
    monkeypatch.delenv("OPS_STATUS_SAMPLER")
    assert job.schedule_enabled() is True


@pytest.mark.asyncio
async def test_enqueue_tick_is_idempotent_per_bucket(monkeypatch):
    from services.jobs import ops_status_sample_job as job

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=True)
    assert await job.enqueue_tick(pool, NOW) == (None, False, "ops_status_sample:2026-11-24T02:05")
    pool.fetchval = AsyncMock(return_value=False)
    enq = AsyncMock(return_value=("jid", True))
    monkeypatch.setattr(job, "enqueue", enq)
    assert await job.enqueue_tick(pool, NOW) == ("jid", True, "ops_status_sample:2026-11-24T02:05")
    kw = enq.call_args.kwargs
    assert kw["idempotency_key"] == "ops_status_sample:2026-11-24T02:05" and kw["max_attempts"] == 1


@pytest.mark.asyncio
async def test_a_late_run_writes_nothing(monkeypatch):
    from api.services.ops_status import sampler as smp
    from services.jobs import ops_status_sample_job as job

    tick = AsyncMock()
    monkeypatch.setattr(smp, "tick", tick)
    ctx = MagicMock()
    ctx.payload = {"bucket": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    out = await job.run(ctx)
    assert out["status"] == "skipped_late" and not tick.called
    ctx.payload = {"bucket": datetime.now(timezone.utc).isoformat(), "trigger": "manual"}
    tick.return_value = {"sampled_at": "x"}
    out = await job.run(ctx)
    assert out["status"] == "sampled" and tick.called


def test_kind_is_registered_background_and_exempt():
    from shared.jobs import registry
    from shared.jobs.worker import load_kinds

    load_kinds()
    k = registry.get_kind("ops_status_sample")
    assert k is not None and k.background and k.deploy_gate_exempt and k.max_attempts == 1


@pytest.mark.asyncio
async def test_sampler_part_queues_only_while_registered(monkeypatch):
    from services.jobs import ops_status_sample_job as job

    enq = AsyncMock(return_value=("jid", True, "k"))
    monkeypatch.setattr(job, "enqueue_tick", enq)
    out: dict = {}
    await job.tick_part(MagicMock(), {}, NOW, out)
    assert out == {} and not enq.called
    await job.tick_part(MagicMock(), {"ops_status_sample": object()}, NOW, out)
    assert out["ops_status_sample"] == {"job_id": "jid", "key": "k"}


# ── route ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin
    from api.services import ops_status

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", MagicMock(), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    real = ops_status.build_status

    async def fake(pool, redis, **kw):
        ops_status.range_of(kw["range_key"], kw["start"], kw["end"])   # the real validation
        return {"generated_at": NOW.isoformat(), "range": kw["range_key"], "compare": kw["compare"]}

    monkeypatch.setattr(ops_status, "build_status", fake)
    yield TestClient(app)
    monkeypatch.setattr(ops_status, "build_status", real)


@pytest.mark.parametrize("role,status", [("admin", 200), ("content", 403), ("viewer", 403), (None, 403)])
def test_route_policy(client, role, status):
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa1140"}
    if role:
        headers["x-admin-role"] = role
    resp = client.get("/admin/overview/status?range=6h&compare=true", headers=headers)
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json() == {"generated_at": NOW.isoformat(), "range": "6h", "compare": True}


def test_route_refuses_a_bad_range(client):
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa1140", "x-admin-role": "admin"}
    assert client.get("/admin/overview/status?range=30d", headers=headers).status_code == 422
    resp = client.get("/admin/overview/status?range=custom&from=2026-11-23T05:00:00Z&to=2026-11-23T01:00:00Z",
                      headers=headers)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "bad_range"


def test_policy_entry_is_ops_read_and_not_a_machine_route():
    from api.core.staff_authz import MACHINE_ROUTES, OPS_READ, POLICY

    assert POLICY[("GET", "/admin/overview/status")] == OPS_READ
    assert ("GET", "/admin/overview/status") not in MACHINE_ROUTES
