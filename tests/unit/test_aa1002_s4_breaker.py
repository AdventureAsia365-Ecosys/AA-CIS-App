"""AA-1002 (gateway slice S4) — the deployment circuit breaker and the connection limiter (shared/llm_client/breaker.py).

Pure transitions per error class; the in-process breaker end to end through LLMClient (no DB: conftest stubs
breaker._db, so the transitions run on the in-process copy, exactly the fallback used before migration 310); the DB
transition (row lock, then the same pure function) against a fake connection; the limiter. The SQL itself runs against
Postgres in tests/integration/test_aa1002_migration_310.py.
"""
import asyncio
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import breaker, connections, vendor_route
from shared.llm_client.breaker import (CLOSED, CREDENTIAL, HALF_OPEN, OPEN, Health, after_failure, after_success,
                                       claimed, state_of)
from shared.llm_client.client import BilledUnusableReply, LLMClient
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.errors import LLMAccessDenied, classify
from tests.unit.test_aa1002_s3_deployment_routing import _route, world  # noqa: F401 — the S3 fake world fixture

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
H = Health("acc3", "sonnet-5-5")
REAL_DB = breaker._db                 # captured at import, before conftest's autouse stub replaces it
ERROR_002 = RuntimeError("AccessDeniedException: Error 002: Access to Bedrock models is not allowed for this account")


def _fail(h, cls, at, n=1):
    for _ in range(n):
        h = after_failure(h, cls, "boom", at)
    return h


# ── pure state machine ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls,threshold,open_s", [
    ("access_denied", 1, 300), ("quota_exhausted", 1, 300), (CREDENTIAL, 1, 120), ("throttled", 3, 30),
    ("other", 5, 60),
])
def test_each_class_opens_after_its_threshold_for_its_period(cls, threshold, open_s):
    h = _fail(H, cls, T0, threshold - 1)
    assert state_of(h, T0) == CLOSED and h.consecutive_failures == threshold - 1
    h = after_failure(h, cls, "boom", T0)
    assert state_of(h, T0) == OPEN
    assert h.circuit_open_until == T0 + timedelta(seconds=open_s) and h.open_streak == 1 and h.opened_at == T0
    assert h.last_error_class == cls
    assert state_of(h, T0 + timedelta(seconds=open_s)) == HALF_OPEN


def test_an_unknown_class_counts_as_other():
    h = _fail(H, "weird", T0, 5)
    assert h.last_error_class == "other" and state_of(h, T0) == OPEN


def test_a_failure_while_open_does_not_extend_the_open_period():
    h = _fail(H, "access_denied", T0)
    later = after_failure(h, "access_denied", "in flight", T0 + timedelta(seconds=10))
    assert later.circuit_open_until == h.circuit_open_until and later.open_streak == 1
    assert later.consecutive_failures == 2 and later.last_error_at == T0 + timedelta(seconds=10)


def test_a_failed_probe_doubles_the_period_up_to_the_cap():
    h = _fail(H, "throttled", T0, 3)                                 # 30 s
    periods = []
    at = T0
    for _ in range(6):
        at = h.circuit_open_until
        h = claimed(h, at)
        assert h is not None and state_of(h, at) == HALF_OPEN
        h = after_failure(h, "throttled", "again", at)
        periods.append((h.circuit_open_until - at).total_seconds())
    assert periods == [60, 120, 240, 300, 300, 300]                  # doubled, capped at 5 min
    assert h.probe_until is None                                     # the lease ends with the probe


def test_one_success_closes_and_resets_everything():
    h = claimed(_fail(H, "other", T0, 5), T0 + timedelta(minutes=2))
    ok = after_success(h, T0 + timedelta(minutes=2))
    assert state_of(ok, T0) == CLOSED
    assert (ok.consecutive_failures, ok.open_streak, ok.circuit_open_until, ok.probe_until, ok.opened_at) == \
        (0, 0, None, None, None)
    assert ok.last_ok_at == T0 + timedelta(minutes=2) and ok.last_error_class == "other"   # history kept


def test_only_one_probe_lease_at_a_time():
    h = _fail(H, "access_denied", T0)
    assert claimed(h, T0) is None                                    # still open
    half = h.circuit_open_until
    first = claimed(h, half)
    assert first is not None and first.probe_until == half + timedelta(seconds=breaker.PROBE_LEASE_S)
    assert claimed(first, half + timedelta(seconds=1)) is None       # the lease is held
    assert claimed(first, first.probe_until) is not None             # a hung probe frees it after the lease
    assert claimed(H, T0) is None                                    # closed: nothing to probe


def test_failure_class_reads_credential_errors_first():
    err = RuntimeError("wrapped")
    err.__cause__ = connections.ConnectionCredentialError("connection 'x': secret unreadable (AccessDenied)")
    assert breaker.failure_class(err) == CREDENTIAL                  # not access_denied: the key, not the provider
    assert breaker.failure_class(ERROR_002) == "access_denied"
    assert breaker.failure_class(RuntimeError("Read timed out")) == "other"


def test_circuit_error_keeps_the_class_that_opened_it():
    e = breaker.circuit_error("m", [("acc3", "access_denied"), ("org", "quota_exhausted")])
    assert classify(e) == "access_denied" and "circuit open on acc3 (access_denied), org (quota_exhausted)" in str(e)
    assert classify(breaker.circuit_error("m", [("acc3", "throttled")])) == "throttled"
    assert classify(breaker.circuit_error("m", [("acc3", "throttled"), ("x", "access_denied")])) == "other"
    assert classify(breaker.circuit_error("m", [("acc3", None)])) == "other"


# ── the DB transition (row lock, then the pure function) ──────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, row=None):
        self.row = row
        self.log: list = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.log.append("BEGIN")

            async def __aexit__(self, *a):
                conn.log.append("COMMIT" if a[0] is None else "ROLLBACK")
        return _Tx()

    async def execute(self, sql, *args):
        self.log.append(sql.split()[0])
        if sql.startswith("UPDATE"):
            self.updated = args

    async def fetchrow(self, sql, *args):
        self.log.append("SELECT FOR UPDATE" if "FOR UPDATE" in sql else "SELECT")
        base = {c: None for c in breaker._COLUMNS}
        base.update(connection_key=args[0], model_key=args[1], consecutive_failures=0, open_streak=0)
        base.update(self.row or {})
        return base


def test_apply_transition_locks_the_row_and_writes_the_pure_result():
    conn = FakeConn({"consecutive_failures": 4})
    before, after = asyncio.run(breaker.apply_transition(
        conn, ("acc3", "gpt-6-luna"), lambda cur: after_failure(cur, "other", "x", T0)))
    assert conn.log == ["BEGIN", "INSERT", "SELECT FOR UPDATE", "UPDATE", "COMMIT"]
    assert before.consecutive_failures == 4 and after.consecutive_failures == 5 and after.open_streak == 1
    assert conn.updated[:2] == ("acc3", "gpt-6-luna") and conn.updated[6] == 5 and conn.updated[7] == after.circuit_open_until


def test_apply_transition_writes_nothing_when_the_probe_is_taken():
    conn = FakeConn({"circuit_open_until": T0 - timedelta(seconds=1), "probe_until": T0 + timedelta(seconds=60)})
    before, after = asyncio.run(breaker.apply_transition(conn, ("acc3", "m"), lambda cur: claimed(cur, T0)))
    assert after is None and "UPDATE" not in conn.log


def test_the_db_path_mirrors_the_written_row(monkeypatch):
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    conn = FakeConn()
    monkeypatch.setattr(breaker, "_db", lambda fn: asyncio.run(fn(conn)))
    breaker.record_failure("acc3", "m", ERROR_002)
    (row,) = breaker.snapshot()
    assert row.last_error_class == "access_denied" and row.circuit_open_until is not None


def test_a_missing_table_backs_off_and_the_breaker_runs_in_process(monkeypatch):
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    monkeypatch.setattr(breaker, "_db", REAL_DB)

    class UndefinedTableError(Exception):
        pass
    calls = []

    def run_sync(fn):
        calls.append(1)
        raise UndefinedTableError('relation "shared.llm_provider_health" does not exist')
    monkeypatch.setattr(connections, "run_sync", run_sync)
    breaker.record_failure("acc3", "m", ERROR_002)
    breaker.record_failure("acc3", "m2", ERROR_002)
    assert len(calls) == 1                                           # the second write did not even connect
    assert breaker.is_open("acc3", "m") and breaker.is_open("acc3", "m2")
    assert breaker._db_down_until - time.monotonic() > breaker.DB_RETRY_S


# ── in process, through LLMClient (the S3 fake world) ─────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_an_access_denied_deployment_is_skipped_by_the_next_call(world):
    world.fail = {"acc3": ERROR_002}
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="gpt-6-luna")
    assert len(world.calls) == 1
    with pytest.raises(LLMAccessDenied, match="circuit open on acc3"):
        world.generate(model_tier="gpt-6-luna")                     # no second provider call, still fails fast
    assert len(world.calls) == 1
    world.fail = {}
    world.generate(model_tier="sonnet-5-5")                          # another model on acc3 is not affected
    assert len(world.calls) == 2


@pytest.mark.real_breaker
def test_a_route_whose_models_are_all_open_still_raises_llm_access_denied(world, monkeypatch):
    _route(monkeypatch, "gpt-6-luna", "sonnet-5-5")
    world.fail = {"acc3": ERROR_002}
    with pytest.raises(LLMAccessDenied):
        world.generate(stage="adhoc_s3")
    n = len(world.calls)
    with pytest.raises(LLMAccessDenied):                            # the job runner fails it at once, as before S4
        world.generate(stage="adhoc_s3")
    assert len(world.calls) == n


@pytest.mark.real_breaker
def test_the_half_open_probe_closes_the_circuit(world, monkeypatch):
    clock = {"now": T0}
    monkeypatch.setattr(breaker, "_now", lambda: clock["now"])
    world.fail = {"acc3": ERROR_002}
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="gpt-6-luna")
    world.fail = {}
    clock["now"] = T0 + timedelta(seconds=301)
    assert not breaker.is_open("acc3", "gpt-6-luna")                 # half-open, no probe yet: in the plan
    resp = world.generate(model_tier="gpt-6-luna")
    assert resp.content == "from acc3"
    (row,) = breaker.snapshot()
    assert state_of(row, clock["now"]) == CLOSED and row.consecutive_failures == 0


@pytest.mark.real_breaker
def test_a_held_probe_keeps_other_calls_away(world, monkeypatch):
    clock = {"now": T0}
    monkeypatch.setattr(breaker, "_now", lambda: clock["now"])
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    clock["now"] = T0 + timedelta(seconds=301)
    assert breaker.admit("acc3", "gpt-6-luna") is True               # this caller is the probe
    assert breaker.is_open("acc3", "gpt-6-luna")                     # everyone else skips it meanwhile
    assert breaker.admit("acc3", "gpt-6-luna") is False


@pytest.mark.real_breaker
def test_other_failures_open_only_after_five_in_a_row(world):
    world.fail = {"acc3": RuntimeError("Read timed out")}
    for _ in range(5):
        with pytest.raises(RuntimeError, match="timed out"):
            world.generate(model_tier="gpt-6-luna")
    assert len(world.calls) == 5
    with pytest.raises(RuntimeError, match="circuit open"):
        world.generate(model_tier="gpt-6-luna")
    assert len(world.calls) == 5


@pytest.mark.real_breaker
def test_an_unusable_reply_is_an_answer_not_a_failure():
    conn = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="task_role", label="x",
                              data_scope="aws_only", enabled=True, region="us-west-1")
    for _ in range(6):
        with pytest.raises(BilledUnusableReply):
            breaker.guarded(conn, "m", MagicMock(side_effect=BilledUnusableReply("schema")),
                            answered=(BilledUnusableReply,))
    (row,) = breaker.snapshot()
    assert row.consecutive_failures == 0 and row.last_ok_at is not None


@pytest.mark.real_breaker
def test_the_switch_off_restores_the_s3_behaviour(world, monkeypatch):
    monkeypatch.setenv(breaker.SWITCH_ENV, "off")
    world.fail = {"acc3": ERROR_002}
    for _ in range(3):
        with pytest.raises(LLMAccessDenied):
            world.generate(model_tier="gpt-6-luna")
    assert len(world.calls) == 3 and breaker.snapshot() == []


def test_the_plan_hook_is_the_breaker():
    assert vendor_route.circuit_open is breaker.is_open


# ── limiter ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _limited(**kw):
    return ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="x",
                              data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct",
                              **kw)


def test_no_limits_means_no_limiter():
    assert breaker._limiter_for(_limited()) is None


def test_max_concurrency_waits_then_saturates_without_a_breaker_count(monkeypatch):
    monkeypatch.setattr(breaker, "LIMIT_WAIT_S", 0.05)
    conn = _limited(max_concurrency=1)
    inside, release = threading.Event(), threading.Event()

    def slow():
        inside.set()
        release.wait(2)
        return "ok"
    t = threading.Thread(target=lambda: breaker.guarded(conn, "m", slow))
    t.start()
    inside.wait(2)
    with pytest.raises(breaker.LimiterSaturated, match="max_concurrency 1"):
        breaker.guarded(conn, "m", lambda: "never")
    release.set()
    t.join(2)
    assert breaker.guarded(conn, "m", lambda: "next") == "next"    # the slot came back
    assert classify(breaker.LimiterSaturated("x")) == "throttled"


@pytest.mark.real_breaker
def test_a_saturated_limiter_is_not_a_provider_failure(monkeypatch):
    monkeypatch.setattr(breaker, "LIMIT_WAIT_S", 0.0)
    conn = _limited(rpm_limit=1)
    assert breaker.guarded(conn, "m", lambda: 1) == 1
    for _ in range(6):
        with pytest.raises(breaker.LimiterSaturated, match="rpm_limit 1"):
            breaker.guarded(conn, "m", lambda: 2)
    assert all(r.consecutive_failures == 0 for r in breaker.snapshot())


def test_rpm_waits_for_the_oldest_call_to_leave_the_window(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(breaker.time, "monotonic", lambda: clock["t"])
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s
    monkeypatch.setattr(breaker, "_sleep", sleep)
    monkeypatch.setattr(breaker, "LIMIT_WAIT_S", 60.0)
    conn = _limited(rpm_limit=2)
    for _ in range(2):
        breaker.guarded(conn, "m", lambda: None)
    clock["t"] += 15
    breaker.guarded(conn, "m", lambda: None)                         # waits until the first call is 60 s old
    assert slept == [45.0]


def test_a_changed_limit_rebuilds_the_limiter():
    first = breaker._limiter_for(_limited(max_concurrency=2))
    assert breaker._limiter_for(_limited(max_concurrency=2)) is first
    assert breaker._limiter_for(_limited(max_concurrency=3)) is not first


# ── review fixes: stale half-open copy, request errors, alias circuit skips ──────────────────────────────────────

def test_admit_lets_the_call_through_when_another_task_already_closed_the_circuit(monkeypatch):
    """The local copy (up to READ_TTL_S old) is half-open, the row under the lock is closed: not a refusal."""
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    monkeypatch.setattr(breaker, "_now", lambda: T0)
    breaker._rows[("acc3", "m")] = replace(H, model_key="m", circuit_open_until=T0 - timedelta(seconds=1),
                                           open_streak=1, last_error_class="other")
    conn = FakeConn({"last_ok_at": T0})                              # closed in the DB (a probe elsewhere succeeded)
    monkeypatch.setattr(breaker, "_db", lambda fn: asyncio.run(fn(conn)))
    assert breaker.admit("acc3", "m") is True
    assert "UPDATE" not in conn.log                                  # nothing claimed: it is not a probe
    assert state_of(breaker._get(("acc3", "m")), T0) == CLOSED       # the local copy caught up


@pytest.mark.parametrize("row,expected", [
    ({"circuit_open_until": T0 + timedelta(seconds=60)}, False),                 # a failed probe reopened it
    ({"circuit_open_until": T0 - timedelta(seconds=1), "probe_until": T0 + timedelta(seconds=60)}, False),  # held
    ({"circuit_open_until": T0 - timedelta(seconds=1)}, True),                   # half-open, free: this is the probe
])
def test_admit_from_a_stale_half_open_copy_follows_the_locked_row(monkeypatch, row, expected):
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    monkeypatch.setattr(breaker, "_now", lambda: T0)
    breaker._rows[("acc3", "m")] = replace(H, model_key="m", circuit_open_until=T0 - timedelta(seconds=1))
    conn = FakeConn(row)
    monkeypatch.setattr(breaker, "_db", lambda fn: asyncio.run(fn(conn)))
    assert breaker.admit("acc3", "m") is expected


BAD_REQUESTS = [
    RuntimeError("An error occurred (ValidationException) when calling the Converse operation: Input is too long "
                 "for requested model."),
    RuntimeError("BadRequestError: Error code: 400 - {'error': {'code': 'context_length_exceeded'}}"),
]


@pytest.mark.parametrize("exc", BAD_REQUESTS)
def test_request_errors_are_their_own_class(exc):
    assert breaker.failure_class(exc) == breaker.REQUEST


def test_an_anthropic_400_is_a_request_error_unless_it_is_a_credit_refusal():
    from shared.llm_client.adapters.anthropic_messages import AnthropicAPIError
    bad = AnthropicAPIError("prompt is too long: 250000 tokens > 200000 maximum", "other", 400,
                            "invalid_request_error")
    assert breaker.failure_class(bad) == breaker.REQUEST
    credit = AnthropicAPIError("Your credit balance is too low", "quota_exhausted", 400, "invalid_request_error")
    assert breaker.failure_class(credit) == "quota_exhausted"


@pytest.mark.parametrize("text", [
    "An error occurred (ValidationException) when calling the Converse operation: The provided model identifier is "
    "invalid.",
    "An error occurred (ValidationException): Invocation of model ID x with on-demand throughput isn't supported. "
    "Retry your request with the ID or ARN of an inference profile.",
])
def test_a_validation_error_about_the_deployment_still_counts(text):
    assert breaker.failure_class(RuntimeError(text)) == "other"


@pytest.mark.real_breaker
def test_many_bad_requests_leave_the_circuit_closed(monkeypatch):
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    conn = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="task_role", label="x",
                              data_scope="aws_only", enabled=True, region="us-west-1")
    for _ in range(8):
        with pytest.raises(RuntimeError, match="too long"):
            breaker.guarded(conn, "m", MagicMock(side_effect=BAD_REQUESTS[0]))
    assert breaker.snapshot() == [] and not breaker.is_open("acc3", "m")
    assert breaker.admit("acc3", "m") is True


@pytest.mark.real_breaker
def test_a_probe_that_meets_a_bad_request_frees_its_lease_without_doubling(monkeypatch):
    monkeypatch.delenv(breaker.SWITCH_ENV, raising=False)
    clock = {"now": T0}
    monkeypatch.setattr(breaker, "_now", lambda: clock["now"])
    conn = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="task_role", label="x",
                              data_scope="aws_only", enabled=True, region="us-west-1")
    breaker.record_failure("acc3", "m", ERROR_002)
    (opened,) = breaker.snapshot()
    clock["now"] = opened.circuit_open_until + timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="too long"):
        breaker.guarded(conn, "m", MagicMock(side_effect=BAD_REQUESTS[0]))     # this call took the probe lease
    (row,) = breaker.snapshot()
    assert row.probe_until is None and row.open_streak == 1 and row.circuit_open_until == opened.circuit_open_until
    assert breaker.admit("acc3", "m") is True                       # the next call is the probe at once


@pytest.mark.real_breaker
def test_an_alias_whose_targets_are_all_behind_an_access_denied_circuit_raises_llm_access_denied(world, monkeypatch):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "on")
    world.reset()
    for key in ("sonnet", "haiku"):                                  # sonnet -> [sonnet, haiku], both only on acc3
        breaker.record_failure("acc3", key, ERROR_002)
    with patch.object(LLMClient, "_legacy_chain", side_effect=AssertionError("legacy chain used")):
        with pytest.raises(LLMAccessDenied, match="All LLM providers failed") as info:
            world.generate(model_tier="sonnet")
    assert world.calls == []                                         # no provider call: the circuits said no
    assert isinstance(info.value.__cause__, breaker.CircuitOpen) and "circuit open on acc3" in str(info.value.__cause__)
