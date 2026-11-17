"""AA-1126 (S283) — the gateway's spend controls per environment, each mode tested with fake providers (no paid call,
no database: the loaders are replaced; conftest gives zero spend by default).

G1  connection cap mode (``cis.llm.connection_cap_mode``): hard_stop keeps today's budget stop; failover_continue
    still skips an over-cap connection while another rung answers, and when every rung is over its cap alerts once
    and sends the call to the first healthy rung (never refused for a connection cap).
G2  anomaly breaker (``cis.llm.anomaly_*``): the trip line (multiple x median, floor), the window after a reset, off /
    alert / enforce on the call path (enforce refuses with ``AnomalyBreakerTripped``, a BudgetExceeded, before any
    attempt), and a read failure never refuses.
G4  run caps (``cis.spend.run_cap_*``): which run a call belongs to, the payload cap wins over the setting, the stamp
    at enqueue, and the refusal once the run's ledger spend reached its cap.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from shared import cost_guard
from shared.llm_client import anomaly_breaker, app_profile, caps_settings, capacity, connection_budget, run_cap
from shared.llm_client.call_log import bind_eval_stage, bind_job
from tests.unit import test_aa1002_s3_deployment_routing as s3
from tests.unit.test_aa1002_s3_deployment_routing import _dep, _org, _route

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
JOB = "11111111-2222-4333-8444-555555555555"
POLICY = caps_settings.AnomalyPolicy(multiple=4.0, floor_usd=25.0, baseline_days=14)


@pytest.fixture
def world(monkeypatch):
    hook = s3.vendor_route.circuit_open
    w = s3.World(monkeypatch)
    yield w
    w.reset()
    s3.catalog.invalidate()
    s3.vendor_route.circuit_open = hook


def _settings(monkeypatch, **values):
    """The settings in force (key -> value); every other key reads as not seeded (today's behaviour)."""
    table = {
        "connection_cap_mode": caps_settings.CONNECTION_CAP_MODE, "anomaly_mode": caps_settings.ANOMALY_MODE,
        "multiple": caps_settings.ANOMALY_MULTIPLE, "floor": caps_settings.ANOMALY_FLOOR_USD,
        "days": caps_settings.ANOMALY_BASELINE_DAYS, "s1": caps_settings.RUN_CAP_S1_REWRITE_USD,
        "eval": caps_settings.RUN_CAP_EVAL_USD, "adhoc": caps_settings.RUN_CAP_ADHOC_USD,
    }
    by_key = {table[k]: v for k, v in values.items()}
    monkeypatch.setattr(caps_settings, "_get", lambda key: by_key.get(key))


def _budget(key, spent=20.0):
    return cost_guard.ConnectionBudget(connection_key=key, monthly_budget_usd=10.0, month_spent_usd=spent,
                                       alert_pct=80, hard_stop=True)


def _over(monkeypatch, *keys):
    """Connections at their hard monthly budget; ``stops`` honours the lift like the real one."""
    def stops(c):
        if connection_budget._LIFTED.get():
            return None
        return _budget(c.connection_key) if c.connection_key in keys else None
    monkeypatch.setattr(connection_budget, "stops", stops)


@pytest.fixture
def bells(monkeypatch):
    """Every bell the three controls ring (no database)."""
    rung: list = []

    monkeypatch.setattr(connection_budget, "send_continue", lambda keys, stage: rung.append(("continue", tuple(keys),
                                                                                              stage)))
    monkeypatch.setattr(anomaly_breaker, "send", lambda v: rung.append(("anomaly", v.app, v.refuses)))
    monkeypatch.setattr(run_cap, "send", lambda r, cap, spent, text: rung.append(("run", r.run_class, cap,
                                                                                   round(spent, 4))))
    return rung


# ── the settings reader ──────────────────────────────────────────────────────────────────────────────────────────

def test_unseeded_settings_keep_todays_behaviour(monkeypatch):
    monkeypatch.setattr(caps_settings, "_get", lambda key: None)
    assert caps_settings.connection_cap_mode() == caps_settings.HARD_STOP
    assert caps_settings.anomaly_mode() == caps_settings.OFF and caps_settings.anomaly_policy() is None
    assert all(caps_settings.run_cap_usd(c) is None for c in caps_settings.RUN_CAP_KEYS)


def test_an_unknown_value_is_the_safe_default(monkeypatch):
    _settings(monkeypatch, connection_cap_mode="always", anomaly_mode="panic", multiple=True)
    assert caps_settings.connection_cap_mode() == caps_settings.HARD_STOP
    assert caps_settings.anomaly_mode() == caps_settings.OFF
    assert caps_settings.anomaly_policy() is None


def test_a_settings_read_error_never_raises(monkeypatch):
    from shared import settings

    def boom(key):
        raise RuntimeError("snapshot broken")
    monkeypatch.setattr(settings, "get", boom)
    assert caps_settings.connection_cap_mode() == caps_settings.HARD_STOP
    assert caps_settings.run_cap_usd("eval") is None


# ── G1: the connection cap mode ──────────────────────────────────────────────────────────────────────────────────

def test_budget_keys_reads_the_cause_chain_and_the_route_failures():
    plain = connection_budget.exceeded("over", ("acc1",))
    assert connection_budget.budget_keys(plain) == ("acc1",)
    mixed = RuntimeError("route failed")
    mixed.route_failures = (RuntimeError("outage"), connection_budget.exceeded("over", ("openai-direct",)))
    assert connection_budget.budget_keys(mixed) == ("openai-direct",)
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = plain
    assert connection_budget.budget_keys(wrapped) == ("acc1",)
    assert connection_budget.budget_keys(RuntimeError("outage")) == ()


def test_hard_stop_keeps_the_budget_stop_without_a_call(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="hard_stop")
    _over(monkeypatch, "acc3")
    with pytest.raises(connection_budget.ConnectionBudgetExceeded):
        world.generate(model_tier="sonnet-5-5")
    assert world.calls == [] and bells == []


def test_failover_continue_still_skips_an_over_cap_connection_while_another_answers(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="failover_continue")
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    _over(monkeypatch, "acc3")
    assert world.generate(model_tier="sonnet-5-5").content == "from bedrock-llm-org"
    assert [c[1] for c in world.calls] == ["bedrock-llm-org"] and bells == []


def test_failover_continue_sends_the_call_to_the_first_rung_when_every_rung_is_over_cap(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="failover_continue")
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    _over(monkeypatch, "acc3")
    resp = world.generate(stage="adhoc_s3")
    assert resp.content == "from acc3"
    assert [c[1] for c in world.calls] == ["acc3"]                       # the first rung, once
    assert bells == [("continue", ("acc3",), "adhoc_s3")]
    assert connection_budget._LIFTED.get() is False                      # lifted for that call only


def test_failover_continue_does_not_retry_a_plain_outage(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="failover_continue")
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    monkeypatch.setattr(connection_budget, "stops", lambda c: None)
    world.fail = {"acc3": RuntimeError("acc3 outage")}
    with pytest.raises(RuntimeError) as e:
        world.generate(stage="adhoc_s3")
    assert not isinstance(e.value, cost_guard.BudgetExceeded) and bells == []


def test_failover_continue_raises_the_real_error_when_the_lifted_call_fails_too(monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="failover_continue")
    calls = []

    def call():
        calls.append(connection_budget._LIFTED.get())
        if not connection_budget._LIFTED.get():
            raise connection_budget.exceeded("every connection over", ("acc1", "acc3"))
        raise RuntimeError("provider outage")
    with pytest.raises(RuntimeError, match="provider outage"):
        connection_budget.run_with_cap_mode(call, "s1_generate")
    assert calls == [False, True] and bells == [("continue", ("acc1", "acc3"), "s1_generate")]


# ── G1: a partial plan (review S283) ─────────────────────────────────────────────────────────────────────────────

def _partial(world, monkeypatch):
    """sonnet-5-5 on acc3 (over its cap, healthy) and on a second Bedrock connection (down): one model, so the plan
    keeps the second deployment and raises only its outage."""
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    world.fail = {"bedrock-llm-org": RuntimeError("org outage")}
    _route(monkeypatch, "sonnet-5-5")
    _over(monkeypatch, "acc3")


def test_failover_continue_lifts_the_caps_when_the_kept_rungs_of_a_plan_fail(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="failover_continue")
    _partial(world, monkeypatch)
    resp = world.generate(stage="adhoc_s3")
    assert resp.content == "from acc3"
    assert [c[1] for c in world.calls] == ["bedrock-llm-org", "acc3"]   # the kept rung, then the over-cap one
    assert bells == [("continue", ("acc3",), "adhoc_s3")]


def test_hard_stop_keeps_the_provider_error_of_a_partial_plan(world, monkeypatch, bells):
    _settings(monkeypatch, connection_cap_mode="hard_stop")
    _partial(world, monkeypatch)
    with pytest.raises(RuntimeError, match="org outage"):
        world.generate(stage="adhoc_s3")
    assert [c[1] for c in world.calls] == ["bedrock-llm-org"] and bells == []


def test_the_continue_alert_never_breaks_the_call(monkeypatch):
    def boom(keys, stage):
        raise RuntimeError("no database")
    monkeypatch.setattr(connection_budget, "send_continue", boom)
    connection_budget.alert_continue(("acc1",), "t9_write")        # logged, not raised


# ── G2: the anomaly breaker ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spent, median, tripped", [
    (40.0, 10.0, False),     # at the line (4 x 10) is not above it
    (40.01, 10.0, True),
    (24.0, 1.0, False),      # under the floor whatever the median
    (25.5, 1.0, True),
    (25.5, 0.0, True),       # no history: the floor alone
])
def test_the_trip_line(spent, median, tripped):
    v = anomaly_breaker.verdict("cis", "enforce", POLICY, since=NOW, spent_usd=spent, median_usd=median)
    assert v.threshold_usd == max(4 * median, 25.0) and v.tripped is tripped and v.refuses is tripped


def test_alert_mode_never_refuses():
    v = anomaly_breaker.verdict("crm", "alert", POLICY, since=NOW, spent_usd=500.0, median_usd=1.0)
    assert v.tripped and not v.refuses


def test_the_window_starts_at_midnight_or_the_reset_of_today():
    midnight = NOW.replace(hour=0)
    assert anomaly_breaker.window_start(NOW, None) == midnight
    assert anomaly_breaker.window_start(NOW, "2026-11-17T09:30:00+00:00") == NOW.replace(hour=9, minute=30)
    assert anomaly_breaker.window_start(NOW, "2026-11-16T23:00:00+00:00") == midnight     # yesterday's reset
    assert anomaly_breaker.window_start(NOW, "not a time") == midnight
    assert anomaly_breaker.baseline_window(date(2026, 11, 17), 14) == (date(2026, 11, 3), date(2026, 11, 17))


def _spend(monkeypatch, spent, median=1.0):
    monkeypatch.setattr(anomaly_breaker, "loader", lambda app, since, today, days, need_median: (spent, median))
    anomaly_breaker.invalidate()


def test_enforce_refuses_the_app_before_any_attempt(world, monkeypatch, bells):
    _settings(monkeypatch, anomaly_mode="enforce", multiple=4, floor=25, days=14)
    _spend(monkeypatch, 80.0)
    with pytest.raises(anomaly_breaker.AnomalyBreakerTripped) as e:
        world.generate(model_tier="sonnet-5-5")
    assert isinstance(e.value, cost_guard.BudgetExceeded) and isinstance(e.value, RuntimeError)
    assert e.value.app == "cis" and "Spend controls" in str(e.value)
    assert world.calls == [] and bells == [("anomaly", "cis", True)]
    with pytest.raises(anomaly_breaker.AnomalyBreakerTripped):
        world.generate(model_tier="sonnet-5-5")
    assert len(bells) == 1                                               # one bell per trip window


@pytest.mark.parametrize("mode", ["alert", "off"])
def test_alert_and_off_never_refuse(world, monkeypatch, bells, mode):
    _settings(monkeypatch, anomaly_mode=mode, multiple=4, floor=25, days=14)
    _spend(monkeypatch, 80.0)
    assert world.generate(model_tier="sonnet-5-5").content == "from acc3"
    assert bells == ([("anomaly", "cis", False)] if mode == "alert" else [])


def test_an_unreadable_spend_never_refuses(world, monkeypatch, bells):
    _settings(monkeypatch, anomaly_mode="enforce", multiple=4, floor=25, days=14)

    def boom(*a):
        raise RuntimeError("no database")
    monkeypatch.setattr(anomaly_breaker, "loader", boom)
    anomaly_breaker.invalidate()
    assert world.generate(model_tier="sonnet-5-5").content == "from acc3" and bells == []


def test_a_reset_today_counts_the_spend_from_the_reset(monkeypatch):
    _settings(monkeypatch, anomaly_mode="enforce", multiple=4, floor=25, days=14)
    seen = []

    def loader(app, since, today, days, need_median):
        seen.append(since)
        return (0.5 if since > NOW.replace(hour=0) else 90.0), 1.0
    monkeypatch.setattr(anomaly_breaker, "loader", loader)
    anomaly_breaker.invalidate()
    assert anomaly_breaker.current("cis", NOW).refuses
    reset = app_profile.AppProfile(app="cis", anomaly_reset_at="2026-11-17T14:00:00+00:00", anomaly_reset_by="admin:x")
    monkeypatch.setattr(app_profile, "loader", lambda: {"cis": reset})
    app_profile.invalidate()
    v = anomaly_breaker.current("cis", NOW)
    assert not v.tripped and v.reset_by == "admin:x" and seen[-1] == NOW.replace(hour=14)


def test_the_embed_path_and_the_batch_lane_follow_the_breaker(monkeypatch):
    from shared.llm_client import batch_lane
    _settings(monkeypatch, anomaly_mode="enforce", multiple=4, floor=25, days=14)
    _spend(monkeypatch, 80.0)
    monkeypatch.setattr(anomaly_breaker, "alert", lambda v: None)
    monkeypatch.setattr(batch_lane, "get_stage_config_sync", lambda stage: None)
    assert "anomaly breaker tripped" in (batch_lane.stage_refusal("s1_generate") or "")


def test_the_app_profile_reads_the_reset_columns_and_their_absence():
    p = app_profile.profile_of({"app": "cis", "anomaly_reset_at": "2026-11-17T10:00:00+00:00",
                                "anomaly_reset_by": "admin:zz"})
    assert (p.anomaly_reset_at, p.anomaly_reset_by) == ("2026-11-17T10:00:00+00:00", "admin:zz")
    old = app_profile.profile_of({"app": "crm"})                      # a row from before CIS 446
    assert old.anomaly_reset_at is None and old.anomaly_reset_by is None
    kept = app_profile.merged(p, {"bulk_paused": True})
    assert kept.anomaly_reset_at == p.anomaly_reset_at


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    async def fetchval(self, sql, *args):
        self.sql.append((sql, args))
        return self.rows.pop(0)


def test_status_reads_every_app_with_ai(monkeypatch):
    _settings(monkeypatch, anomaly_mode="alert", multiple=4, floor=25, days=14)

    async def no_profiles(conn):
        return {}
    monkeypatch.setattr(app_profile, "read_all", no_profiles)
    conn = _Conn([30.0, 5.0, 1.0, 0.0, 0.0, 0.0])                       # (today, median) per app
    out = asyncio.run(anomaly_breaker.status(conn, now=NOW))
    assert [a["app"] for a in out["apps"]] == ["cis", "tripplanner", "crm"]
    cis = out["apps"][0]
    assert cis["tripped"] is True and cis["refuses"] is False and cis["threshold_usd"] == 25.0
    assert out["policy"] == {"multiple": 4.0, "floor_usd": 25.0, "baseline_days": 14}
    assert all("shared.llm_call_log" in sql for sql, _ in conn.sql)


def test_status_reads_nothing_when_off(monkeypatch):
    _settings(monkeypatch, anomaly_mode="off")
    out = asyncio.run(anomaly_breaker.status(_Conn([]), now=NOW))
    assert out == {"mode": "off", "policy": None, "apps": []}


# ── G4: run caps ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_run_of_a_call():
    assert run_cap.run_of("s1_generate", job_id=JOB, job_kind="s1_rewrite", eval_label=None, now=NOW) == \
        run_cap.Run("s1_rewrite", "job", JOB)
    assert run_cap.run_of("t9_write", job_id=JOB, job_kind="t9_write", eval_label=None, now=NOW) is None
    assert run_cap.run_of("t9_write", job_id=None, job_kind=None, eval_label=None, now=NOW) is None
    ev = run_cap.run_of("t9_write", job_id=None, job_kind=None, eval_label=("writing_memory_eval", "run-a"), now=NOW)
    assert (ev.run_class, ev.kind, ev.ref, ev.stage) == ("eval", "eval_run", "run-a", "writing_memory_eval")
    in_job = run_cap.run_of("t9_write", job_id=JOB, job_kind="writing_memory_r2",
                            eval_label=("adhoc_zz_eval", "run-b"), now=NOW)
    assert (in_job.run_class, in_job.kind) == ("eval", "job")
    ad = run_cap.run_of("adhoc_zz_probe", job_id=None, job_kind=None, eval_label=None, now=NOW)
    assert (ad.run_class, ad.kind, ad.ref) == ("adhoc", "stage", "adhoc_zz_probe")


@pytest.mark.parametrize("payload, cap", [
    ({"cap_usd": 1.5}, 1.5), ({"cap_usd": 0}, None), ({"cap_usd": -1}, None), ({"cap_usd": "2"}, None),
    ({"cap_usd": True}, None), ({}, None), (None, None),
])
def test_the_payload_cap(payload, cap):
    assert run_cap.cap_of_payload(payload) == cap


def test_the_stamp_at_enqueue(monkeypatch):
    _settings(monkeypatch, s1=2)
    assert run_cap.stamp("s1_rewrite", {"tour_id": "t-1"}) == {"tour_id": "t-1", "cap_usd": 2.0}
    assert run_cap.stamp("s1_rewrite", {"tour_id": "t-1", "cap_usd": 0.7}) == {"tour_id": "t-1", "cap_usd": 0.7}
    assert run_cap.stamp("a3_atomize", {"tour_id": "t-1"}) == {"tour_id": "t-1"}
    _settings(monkeypatch)                                               # not seeded: no stamp
    assert run_cap.stamp("s1_rewrite", {"tour_id": "t-1"}) == {"tour_id": "t-1"}


def _run_spend(monkeypatch, usd):
    seen = []

    def loader(run):
        seen.append(run)
        return usd
    monkeypatch.setattr(run_cap, "loader", loader)
    run_cap.invalidate()
    return seen


def test_an_s1_job_at_its_setting_cap_is_refused_before_any_attempt(world, monkeypatch, bells):
    _settings(monkeypatch, s1=2)
    _run_spend(monkeypatch, 2.0)
    with bind_job(JOB), capacity.bind_kind("s1_rewrite"), run_cap.bind_payload({"tour_id": "t-1"}):
        with pytest.raises(run_cap.RunCapReached) as e:
            world.generate(model_tier="sonnet-5-5")
    assert isinstance(e.value, cost_guard.BudgetExceeded) and e.value.run_class == "s1_rewrite"
    assert world.calls == [] and bells == [("run", "s1_rewrite", 2.0, 2.0)]


def test_the_payload_cap_wins_over_the_setting(world, monkeypatch, bells):
    _settings(monkeypatch, s1=2)
    _run_spend(monkeypatch, 1.2)
    with bind_job(JOB), capacity.bind_kind("s1_rewrite"), run_cap.bind_payload({"cap_usd": 5.0}):
        assert world.generate(model_tier="sonnet-5-5").content == "from acc3"
    with bind_job(JOB), capacity.bind_kind("s1_rewrite"), run_cap.bind_payload({"cap_usd": 1.0}):
        with pytest.raises(run_cap.RunCapReached):
            world.generate(model_tier="sonnet-5-5")


def test_an_eval_run_counts_its_own_rows(world, monkeypatch, bells):
    _settings(monkeypatch, eval=10)
    seen = _run_spend(monkeypatch, 10.5)
    with bind_eval_stage("writing_memory_eval", "run-zz"):
        with pytest.raises(run_cap.RunCapReached):
            world.generate(model_tier="sonnet-5-5")
    assert seen[0].kind == "eval_run" and seen[0].ref == "run-zz"


def test_an_adhoc_run_under_its_cap_goes_on_and_an_uncapped_call_reads_nothing(world, monkeypatch, bells):
    _settings(monkeypatch, adhoc=5)
    seen = _run_spend(monkeypatch, 4.9)
    _route(monkeypatch, "sonnet-5-5")
    assert world.generate(stage="adhoc_s3").content == "from acc3"
    assert seen[0].run_class == "adhoc"
    seen.clear()
    world.generate(model_tier="sonnet-5-5")                              # no job, no eval, no adhoc stage
    assert seen == [] and bells == []


def test_an_unreadable_run_spend_never_refuses(world, monkeypatch, bells):
    _settings(monkeypatch, s1=0.05)

    def boom(run):
        raise RuntimeError("no database")
    monkeypatch.setattr(run_cap, "loader", boom)
    run_cap.invalidate()
    with bind_job(JOB), capacity.bind_kind("s1_rewrite"):
        assert world.generate(model_tier="sonnet-5-5").content == "from acc3"


def test_the_spend_queries_use_the_indexed_columns():
    assert "job_id = $1" in run_cap.JOB_SPEND_SQL
    assert "stage = $1 AND created_at >= $2" in run_cap.EVAL_SPEND_SQL and "eval_run" in run_cap.EVAL_SPEND_SQL
    assert "stage = $1 AND created_at >= $2" in run_cap.STAGE_SPEND_SQL


# ── review S283: a spend stop is noted for the job (the S1 job ends stopped_budget: test_aa1126_spend_stops) ──

def test_the_gateway_notes_a_run_cap_refusal_for_the_job(world, monkeypatch, bells):
    _settings(monkeypatch, s1=2)
    monkeypatch.setattr(run_cap, "loader", lambda run: 2.0)
    run_cap.invalidate()
    with bind_job(JOB), capacity.bind_kind("s1_rewrite"), run_cap.bind_payload({}):
        with pytest.raises(run_cap.RunCapReached) as e:
            world.generate(model_tier="sonnet-5-5")
    assert run_cap.job_stop(JOB) is e.value
    assert run_cap.job_stop(JOB, pop=True) is e.value and run_cap.job_stop(JOB) is None
