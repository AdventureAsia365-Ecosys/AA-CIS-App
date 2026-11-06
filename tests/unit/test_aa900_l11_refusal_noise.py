"""AA-900 (S271 lane l11) — fewer refused rows while a model has no grant, refusals shown as expected (not errors).

Part A: the access_denied open period (300 s doubling to 6 h); the recovery paths (the probe bypasses an open circuit
and its success closes it; an account that answers again re-probes the refusals it may have caused, never the known
refusals of one model); the log level of a known refusal; the rate-limited llm_deployment_skip line.
Part B: the expected-refusal rule (SQL + Python), the availability card, the route digest. The SQL runs against
Postgres in tests/integration/test_aa900_expected_refusal_db.py.
Invented model and connection names only, apart from the acc3 / gpt-6-luna keys the gateway already uses.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from api.services import llm_availability as av
from shared.llm_client import breaker, expected_refusal as er, route_watch, vendor_route
from shared.llm_client.breaker import (ACCESS_DENIED, CLOSED, HALF_OPEN, OPEN, QUOTA_EXHAUSTED, RULES, THROTTLED,
                                       Health, after_failure, open_seconds, recovery_candidates, state_of)
from shared.llm_client.connections import ProviderConnection

T0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
ERROR_002 = RuntimeError("An error occurred (AccessDeniedException) when calling the Converse operation: "
                         "You don't have access to the model with the specified model ID.")
CONN = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="task_role", label="x",
                          data_scope="aws_only", enabled=True, region="us-west-1")


def _denied(model, opened_at, *, conn="acc3", open_s=21600, streak=9, ok_at=None):
    return Health(conn, model, last_ok_at=ok_at, last_error_at=opened_at + timedelta(hours=1),
                  last_error_class=ACCESS_DENIED, last_error="boom", consecutive_failures=streak,
                  circuit_open_until=opened_at + timedelta(seconds=open_s), open_streak=streak, opened_at=opened_at)


def _ok(model, at, conn="acc3"):
    return Health(conn, model, last_ok_at=at)


@pytest.fixture
def clock(monkeypatch):
    c = {"now": T0}
    monkeypatch.setattr(breaker, "_now", lambda: c["now"])
    return c


@pytest.fixture
def no_alerts(monkeypatch):
    monkeypatch.setattr(breaker, "on_connection_open", lambda h, models: None)


def _load(*rows):
    with breaker._lock:
        breaker._rows.update({(h.connection_key, h.model_key): h for h in rows})


# ── Part A: the open period ─────────────────────────────────────────────────────────────────────────────────────────

def test_access_denied_doubles_from_five_minutes_up_to_six_hours():
    assert [open_seconds(ACCESS_DENIED, n) for n in range(1, 10)] == \
        [300, 600, 1200, 2400, 4800, 9600, 19200, 21600, 21600]
    assert RULES[ACCESS_DENIED].max_open_s == breaker.ACCESS_DENIED_MAX_OPEN_S == 6 * 3600


def test_the_other_classes_keep_their_periods():
    assert (RULES[QUOTA_EXHAUSTED].open_s, RULES[QUOTA_EXHAUSTED].max_open_s) == (300, 1800)
    assert (RULES[breaker.CREDENTIAL].open_s, RULES[breaker.CREDENTIAL].max_open_s) == (120, 900)
    assert (RULES[THROTTLED].threshold, RULES[THROTTLED].max_open_s) == (3, 300)


def test_a_long_refused_model_reopens_for_six_hours():
    h = _denied("gpt-6-luna", T0 - timedelta(days=8), open_s=60)
    at = h.circuit_open_until + timedelta(seconds=1)              # half-open: the real-traffic probe fails
    h2 = after_failure(h, ACCESS_DENIED, "again", at, keep_streak_start=True)
    assert h2.circuit_open_until - at == timedelta(hours=6) and h2.opened_at == h.opened_at


def test_only_a_known_refusal_gets_the_six_hour_cap():
    old = T0 - timedelta(days=8)
    luna = _denied("gpt-6-luna", old, open_s=60)
    assert breaker.failure_rules(luna, [luna, _ok("sonnet-4-6", T0 - timedelta(hours=1))]) is RULES   # known
    assert breaker.failure_rules(luna, [luna]) is breaker.ACCOUNT_DENIED_RULES       # nothing answered: maybe the account
    assert breaker.failure_rules(luna, [luna, _ok("sonnet-4-6", old - timedelta(hours=1))]) is \
        breaker.ACCOUNT_DENIED_RULES                                                  # answered before the streak only
    assert breaker.failure_rules(None, []) is breaker.ACCOUNT_DENIED_RULES
    assert breaker.ACCOUNT_DENIED_RULES[ACCESS_DENIED].max_open_s == breaker.ACCOUNT_DENIED_MAX_OPEN_S == 1800
    assert {k: v for k, v in breaker.ACCOUNT_DENIED_RULES.items() if k != ACCESS_DENIED} == \
        {k: v for k, v in RULES.items() if k != ACCESS_DENIED}


@pytest.mark.real_breaker
def test_a_known_refusal_reopens_for_six_hours_through_record_failure(clock, no_alerts):
    luna = _denied("gpt-6-luna", T0 - timedelta(days=8), open_s=8 * 86400 - 60)      # half-open since a minute
    _load(luna, _ok("sonnet-4-6", T0 - timedelta(hours=1)))
    assert breaker.admit("acc3", "gpt-6-luna")                                     # the one real-traffic probe
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    h = breaker._get(("acc3", "gpt-6-luna"))
    assert h.circuit_open_until - T0 == timedelta(hours=6) and h.opened_at == luna.opened_at


@pytest.mark.real_breaker
def test_an_account_wide_refusal_stays_capped_at_thirty_minutes(clock, no_alerts):
    """Error 002 on every model for days, nothing answered since: each failed probe reopens for 30 min at most, so the
    connection circuit half-opens within 30 min and real traffic finds a recovered account by itself."""
    start = T0 - timedelta(days=2)
    models = ("gpt-6-luna", "sonnet-5-5", "haiku-4-5")
    _load(*[_denied(m, start, open_s=2 * 86400 - 60) for m in models],               # every row half-open
          _ok("sonnet-4-6", start - timedelta(hours=1)))                             # last answer before the outage
    assert breaker.admit("acc3", "gpt-6-luna")                                       # a real call probes, refused
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    assert breaker._get(("acc3", "gpt-6-luna")).circuit_open_until - T0 == timedelta(minutes=30)   # not 6 h
    assert not breaker.known_refusal("acc3", "gpt-6-luna")
    assert breaker.connection_open("acc3") == ACCESS_DENIED                          # the account is refusing
    assert not breaker.admit("acc3", "sonnet-5-5")                                   # every real call turned away
    assert breaker.connection_snapshot("acc3").circuit_open_until - T0 == timedelta(minutes=30)
    clock["now"] = T0 + timedelta(minutes=30, seconds=1)
    assert breaker.connection_open("acc3") is None                                   # half-open within 30 min
    assert breaker.admit("acc3", "gpt-6-luna")                                       # one real call probes it
    breaker.record_success("acc3", "gpt-6-luna")                                     # AWS fixed the account
    assert not breaker.is_open("acc3", "gpt-6-luna")
    for m in ("sonnet-5-5", "haiku-4-5"):                                            # re-probed at once
        assert state_of(breaker._get(("acc3", m)), clock["now"]) == HALF_OPEN


# ── Part A: recovery paths ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_the_probe_reaches_an_open_circuit_and_its_success_closes_it(clock):
    _load(_denied("gpt-6-luna", T0 - timedelta(days=8), open_s=10 * 86400), _ok("sonnet-4-6", T0 - timedelta(hours=1)))
    assert breaker.is_open("acc3", "gpt-6-luna") and not breaker.admit("acc3", "gpt-6-luna")
    call = MagicMock(return_value="OK")
    assert breaker.guarded(CONN, "gpt-6-luna", call, admit_call=False) == "OK"     # LLMClient.probe's path
    call.assert_called_once()
    h = breaker._get(("acc3", "gpt-6-luna"))
    assert state_of(h, T0) == CLOSED and h.open_streak == 0 and h.last_ok_at == T0
    assert not breaker.is_open("acc3", "gpt-6-luna")


@pytest.mark.real_breaker
def test_a_refused_probe_keeps_the_circuit_open_and_known(clock, no_alerts):
    _load(_denied("gpt-6-luna", T0 - timedelta(days=8), open_s=10 * 86400), _ok("sonnet-4-6", T0 - timedelta(hours=1)))
    with pytest.raises(RuntimeError):
        breaker.guarded(CONN, "gpt-6-luna", MagicMock(side_effect=ERROR_002), admit_call=False)
    assert breaker.is_open("acc3", "gpt-6-luna") and breaker.known_refusal("acc3", "gpt-6-luna")


@pytest.mark.real_breaker
def test_an_answer_does_not_reprobe_the_known_refusals(clock):
    old = T0 - timedelta(days=8)
    rows = [_denied(m, old, open_s=8 * 86400 + 5 * 3600) for m in ("gpt-6-luna", "sonnet-5-5", "opus-5-5")]
    _load(*rows, _ok("sonnet-4-6", T0 - timedelta(hours=2)))
    assert recovery_candidates(breaker.snapshot(), "acc3", "sonnet-4-6", T0) == []
    breaker.record_success("acc3", "sonnet-4-6")
    for h in rows:
        assert state_of(breaker._get(("acc3", h.model_key)), T0) == OPEN        # 6 h kept: no grant yet
        assert breaker.known_refusal("acc3", h.model_key)
    assert breaker.known_model_refusal("gpt-6-luna")


@pytest.mark.real_breaker
def test_an_account_that_answers_again_reprobes_its_refusals_at_once(clock):
    start = T0 - timedelta(days=2)                                 # Error 002 on every model, no answer since
    rows = [_denied(m, start, open_s=3 * 86400) for m in ("gpt-6-luna", "sonnet-5-5", "haiku-4-5")]
    _load(*rows, _denied("embed-x", start, conn="other-acc", open_s=3 * 86400))
    assert recovery_candidates(breaker.snapshot(), "acc3", "sonnet-4-6", T0) == ["gpt-6-luna", "haiku-4-5",
                                                                                   "sonnet-5-5"]
    breaker.record_success("acc3", "sonnet-4-6")                   # the daily probe / Test connection / a real call
    for h in rows:
        now_row = breaker._get(("acc3", h.model_key))
        assert state_of(now_row, T0) == HALF_OPEN and now_row.open_streak == h.open_streak
        assert now_row.opened_at == h.opened_at and not breaker.is_open("acc3", h.model_key)
    assert state_of(breaker._get(("other-acc", "embed-x")), T0) == OPEN             # another account: untouched


@pytest.mark.real_breaker
def test_a_fresh_refusal_after_an_answer_is_reprobed_once_only(clock, no_alerts):
    _load(_ok("sonnet-4-6", T0 - timedelta(minutes=5)))
    breaker.record_failure("acc3", "new-model", ERROR_002)           # refused after the last answer: maybe the account
    clock["now"] = T0 + timedelta(minutes=1)
    breaker.record_success("acc3", "sonnet-4-6")
    assert state_of(breaker._get(("acc3", "new-model")), clock["now"]) == HALF_OPEN
    assert breaker.admit("acc3", "new-model")                       # the one probe
    breaker.record_failure("acc3", "new-model", ERROR_002)           # refused again: on its own
    h = breaker._get(("acc3", "new-model"))
    assert h.open_streak == 2 and h.opened_at == T0
    clock["now"] = T0 + timedelta(minutes=2)
    breaker.record_success("acc3", "sonnet-4-6")
    assert state_of(breaker._get(("acc3", "new-model")), clock["now"]) == OPEN       # not again
    assert breaker.known_refusal("acc3", "new-model")


def test_recovery_candidates_skip_throttles_and_closed_rows():
    start = T0 - timedelta(hours=3)
    throttled = Health("acc3", "t", last_error_at=start, last_error_class=THROTTLED, consecutive_failures=3,
                       circuit_open_until=T0 + timedelta(minutes=1), open_streak=1, opened_at=start)
    rows = [throttled, Health("acc3", "c"), _denied("d", start, open_s=60)]       # d: half-open already
    assert recovery_candidates(rows, "acc3", "x", T0) == []


# ── Part A: log levels ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_a_known_refusal_reopens_at_info_and_a_new_one_warns(clock, no_alerts, monkeypatch):
    logged = []
    monkeypatch.setattr(breaker.logger, "info", lambda ev, **kw: logged.append(("info", ev, kw)))
    monkeypatch.setattr(breaker.logger, "warning", lambda ev, **kw: logged.append(("warning", ev, kw)))
    _load(_denied("gpt-6-luna", T0 - timedelta(days=8), open_s=60), _ok("sonnet-4-6", T0 - timedelta(hours=1)))
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)          # half-open probe failed
    breaker.record_failure("acc3", "fresh-model", ERROR_002)         # a model that answered before (no row)
    opened = [(lvl, kw["model"], kw["known_refusal"]) for lvl, ev, kw in logged if ev == "llm_circuit_opened"]
    assert opened == [("info", "gpt-6-luna", True), ("warning", "fresh-model", False)]


def test_known_helpers_never_raise_and_answer_false_without_rows():
    assert breaker.known_refusal("nope", "nope") is False
    assert breaker.known_model_refusal("nope") is False


def test_a_refused_model_with_an_unknown_row_is_not_a_known_model_refusal(clock):
    old = T0 - timedelta(days=8)
    _load(_denied("gpt-6-luna", old, open_s=10 * 86400), _ok("sonnet-4-6", T0 - timedelta(hours=1)),
          _denied("gpt-6-luna", T0 - timedelta(minutes=1), conn="openai-x", open_s=600, streak=1))
    assert breaker.known_refusal("acc3", "gpt-6-luna")
    assert not breaker.known_model_refusal("gpt-6-luna")             # its other deployment: no answer on that account


# ── Part A: the rate-limited skip line ──────────────────────────────────────────────────────────────────────────────

def test_the_skip_line_is_logged_once_per_window_with_the_suppressed_count(monkeypatch):
    vendor_route.reset_skip_log()
    t = {"now": 1000.0}
    monkeypatch.setattr(vendor_route.time, "monotonic", lambda: t["now"])
    lines = []
    monkeypatch.setattr(vendor_route.logger, "info", lambda ev, **kw: lines.append((ev, kw)))
    for _ in range(5):
        vendor_route._skip_log("gpt-6-luna", "acc3", "circuit open")
    vendor_route._skip_log("gpt-6-luna", "acc3", "connection circuit open (access_denied)")    # another reason
    vendor_route._skip_log("sonnet-5-5", "acc3", "circuit open")                               # another model
    assert [(kw["model"], kw["reason"], kw.get("suppressed")) for _, kw in lines] == [
        ("gpt-6-luna", "circuit open", None), ("gpt-6-luna", "connection circuit open (access_denied)", None),
        ("sonnet-5-5", "circuit open", None)]
    t["now"] += vendor_route.SKIP_LOG_INTERVAL_S + 1
    vendor_route._skip_log("gpt-6-luna", "acc3", "circuit open")
    ev, kw = lines[-1]
    assert ev == "llm_deployment_skip" and kw["suppressed"] == 4 and kw["window_s"] == 600
    vendor_route.reset_skip_log()


def test_the_skip_memo_is_bounded(monkeypatch):
    vendor_route.reset_skip_log()
    monkeypatch.setattr(vendor_route.logger, "info", lambda ev, **kw: None)
    for i in range(vendor_route._SKIP_LOG_MAX_KEYS + 5):
        vendor_route._skip_log(f"m{i}", "acc3", "circuit open")
    assert len(vendor_route._skip_logged) <= vendor_route._SKIP_LOG_MAX_KEYS
    vendor_route.reset_skip_log()


# ── Part B: the rule ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,cls,dep,conn,expected", [
    ("refused", "access_denied", "access_denied", "ok", True),
    ("refused", "access_denied", "ok", "ok", False),                 # a model that answered before: an error
    ("refused", "access_denied", "access_denied", "access_denied", False),   # account-wide: an error
    ("refused", "access_denied", "access_denied", "unknown", False),
    ("refused", "throttled", "access_denied", "ok", False),
    ("error", "access_denied", "access_denied", "ok", False),
    ("refused", "access_denied", None, "ok", False),                 # no deployment row
])
def test_is_expected(status, cls, dep, conn, expected):
    assert er.is_expected(status, cls, dep, conn) is expected


def test_the_sql_names_both_states_and_qualifies_the_outer_row():
    sql = er.expected_sql("l")
    assert "l.status = 'refused'" in sql and "l.error_class = 'access_denied'" in sql
    assert "xr_d.connection_key = l.connection_key AND xr_d.model_key = l.model" in sql
    assert "xr_d.access_state = 'access_denied'" in sql and "xr_c.access_state = 'ok'" in sql
    assert er.EXPECTED_SQL == er.expected_sql("shared.llm_call_log")


# ── Part B: the availability card ───────────────────────────────────────────────────────────────────────────────────

def _h(model, cls=None, *, open_until=None, dep_state=None, conn_state="ok", ok_at=None, failures=0, conn="acc3"):
    return {"connection_key": conn, "kind": "bedrock", "label": conn, "enabled": True, "model_key": model,
            "last_ok_at": ok_at, "last_error_at": T0 - timedelta(hours=1) if cls else None, "last_error_class": cls,
            "last_error": "boom" if cls else None, "consecutive_failures": failures,
            "circuit_open_until": open_until, "open_streak": 3 if cls else 0,
            "opened_at": T0 - timedelta(days=8) if cls else None, "probe_until": None,
            "deployment_access_state": dep_state, "connection_access_state": conn_state}


def test_a_model_with_no_grant_does_not_make_the_connection_open():
    rows = [_h("sonnet-4-6", ok_at=T0 - timedelta(minutes=5)),
            _h("gpt-6-luna", ACCESS_DENIED, open_until=T0 + timedelta(hours=5), dep_state="access_denied",
               failures=9)]
    (c,) = av.build_connections(rows, now=T0)
    assert c["status"] == "ok" and c["open_models"] == 0 and c["known_refused_models"] == ["gpt-6-luna"]
    assert [m["expected_refusal"] for m in c["models"]] == [False, True]


def test_a_model_that_answered_and_now_refuses_still_opens_the_connection():
    rows = [_h("sonnet-4-6", ok_at=T0 - timedelta(minutes=5)),
            _h("gpt-6-luna", ACCESS_DENIED, open_until=T0 + timedelta(minutes=5), dep_state="ok", failures=1)]
    (c,) = av.build_connections(rows, now=T0)
    assert c["status"] == "open" and c["open_models"] == 1 and c["known_refused_models"] == []


def test_an_account_wide_refusal_is_never_expected():
    rows = [_h("gpt-6-luna", ACCESS_DENIED, open_until=T0 + timedelta(minutes=5), dep_state="access_denied",
               conn_state="access_denied", failures=1)]
    (c,) = av.build_connections(rows, now=T0)
    assert c["status"] == "open" and c["known_refused_models"] == []


# ── Part B: the route digest ────────────────────────────────────────────────────────────────────────────────────────

def test_the_digest_reports_expected_refusals_apart_and_does_not_alert_on_them():
    st = {"answered": 10, "fallback_answered": 0, "lost": 0, "failed_attempts": 0, "expected_refusals": 14,
          "cost_usd": 0.01}
    quiet = route_watch.digest({"invented_judge": st}, {}, {})
    assert quiet["attention_total"] == 0 and route_watch.digest_finding(quiet) is None
    lost = route_watch.digest({"invented_judge": {**st, "lost": 1}}, {}, {})
    (s,) = lost["attention"]
    assert (s["failed_attempts"], s["expected_refusals"], s["lost_requests"]) == (0, 14, 1)


def test_the_non_llm_provider_circuits_keep_the_30_minute_access_denied_cap():
    from shared import provider_circuit  # noqa: F401 — imports PROVIDER_RULES from the breaker
    assert breaker.PROVIDER_RULES[ACCESS_DENIED].max_open_s == 1800
    assert {k: v for k, v in breaker.PROVIDER_RULES.items() if k != ACCESS_DENIED} == \
        {k: v for k, v in RULES.items() if k != ACCESS_DENIED}
    h = Health("typesafe", "*")
    for _ in range(12):                                             # a long run of failed probes
        at = (h.circuit_open_until or T0) + timedelta(seconds=1)
        h = after_failure(h, ACCESS_DENIED, "401", at, rules=breaker.PROVIDER_RULES)
    assert h.circuit_open_until - at == timedelta(seconds=1800)
