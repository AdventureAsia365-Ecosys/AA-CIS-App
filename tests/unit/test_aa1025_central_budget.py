"""AA-1025 A7 — the central LLM budgets (shared.spend_budget provider 'llm', CIS migration 340) on the gateway's call
path: scopes, caps, the pre-call refusal with its 0 USD ledger row, alert-only rows, fail open, the bell.

No database: the rows, the spend and the job kind come from the module's loaders (replaced here). Invented stages,
jobs and amounts only.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from shared import cost_guard
from shared.llm_client import call_log, central_budget as cb
from shared.llm_client.call_log import bind_eval_stage, bind_job
from shared.llm_client.client import estimate_call_usd
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World

JOB = "3f1e2d4c-5b6a-4798-8a1b-2c3d4e5f6a7b"


def _row(scope, *, run=None, day=None, month=None, hard=True, pct=80):
    return cb.CentralBudget(scope=scope, per_run_usd=run, per_day_usd=day, per_month_usd=month, hard_stop=hard,
                            alert_pct=pct)


@pytest.fixture
def state(monkeypatch):
    """rows {scope: CentralBudget}, spend {scope: Spend}, kinds {job id: kind}; the loaders count their reads and the
    stop bells."""
    rows: dict = {}
    spend: dict = {}
    kinds: dict = {}
    reads = {"rows": 0, "spend": [], "kind": 0, "stops": []}

    def load_rows():
        reads["rows"] += 1
        return dict(rows)

    def load_spend(row, job_id):
        reads["spend"].append((row.scope, job_id))
        return spend.get(row.scope, cb.Spend())

    def load_kind(job_id):
        reads["kind"] += 1
        return kinds.get(job_id)

    monkeypatch.setattr(cb, "loader_rows", load_rows)
    monkeypatch.setattr(cb, "loader_spend", load_spend)
    monkeypatch.setattr(cb, "loader_job_kind", load_kind)
    monkeypatch.setattr(cb, "alert_stop", lambda row, sp, text: reads["stops"].append((row.scope, text)))
    cb.invalidate()
    return rows, spend, kinds, reads


# ── pure rules ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_row_of_reads_a_jsonb_row_with_or_without_the_month_column():
    r = cb.row_of({"provider": "llm", "scope": "stage:t9_write", "per_day_usd": "0.01", "hard_stop": True,
                   "alert_pct": 90})
    assert r == _row("stage:t9_write", day=0.01, pct=90)
    assert cb.row_of({"scope": "eval", "per_month_usd": 25, "hard_stop": False}).per_month_usd == 25.0
    assert cb.row_of({"scope": "global"}) is None
    assert cb.row_of({"scope": "job:x"}).capped is False


@pytest.mark.parametrize("scope,ok", [("app:cis", True), ("stage:a3_place_dedupe", True), ("job:place_dedupe", True),
                                      ("eval", True), ("adhoc", True), ("global", False), ("stage:", False),
                                      ("tenant:x", False), ("app:CIS", False)])
def test_the_scope_rule_is_the_migration_check(scope, ok):
    assert bool(cb.SCOPE_RE.match(scope)) is ok


def test_scopes_of_a_call():
    assert cb.scopes_for("t9_write", app="cis") == ["app:cis", "stage:t9_write"]
    assert cb.scopes_for("a3_place_days", app="cis", job_kind="place_extract") == [
        "app:cis", "stage:a3_place_days", "job:place_extract"]
    assert cb.scopes_for("t9_write", app="cis", eval_stage="writing_memory_eval") == [
        "app:cis", "stage:t9_write", "eval"]
    assert cb.scopes_for("writing_memory_eval", app="cis")[-1] == "eval"
    assert cb.scopes_for("adhoc_aa1026", app="cis")[-1] == "adhoc"
    assert cb.scopes_for("s1_generate", app="cis", eval_stage="adhoc_aa900")[-2:] == ["eval", "adhoc"]
    assert cb.scopes_for(None, app="cis") == ["app:cis"]


def test_the_eval_stages_are_the_writing_memory_labels():
    from services.writing_memory import config as cfg
    assert set(cb.EVAL_STAGES) == {*cfg.EVAL_LOG_STAGES, cfg.SHADOW_STAGE}
    assert cb.EVAL_STAGES[0] == cfg.EVAL_STAGE


def test_breaches_and_used_pct():
    row = _row("job:k", run=1.0, day=5.0, month=50.0)
    assert cb.breaches(row, cb.Spend(day_usd=4.9, month_usd=10.0, run_usd=0.5), 0.05) == []
    found = cb.breaches(row, cb.Spend(day_usd=4.99, month_usd=49.99, run_usd=0.99), 0.05)
    assert [b.axis for b in found] == ["per_run", "per_day", "per_month"]
    assert cb.breaches(row, cb.Spend(day_usd=0.0), 9.0) == [cb.Breach("per_day", 5.0, 0.0)]  # unread axes skipped
    assert cb.used_pct(row, cb.Spend(day_usd=4.0, month_usd=10.0, run_usd=0.1)) == pytest.approx(80.0)
    assert cb.used_pct(_row("adhoc", day=0.0), cb.Spend(day_usd=0.001)) == 100.0
    assert cb.used_pct(_row("adhoc"), cb.Spend()) is None


def test_utc_windows():
    day, month = cb.utc_windows(datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc))
    assert (day.isoformat(), month.isoformat()) == ("2026-11-02T00:00:00+00:00", "2026-11-01T00:00:00+00:00")


def test_the_estimate_is_the_worst_case_on_the_first_model():
    req = LLMRequest(system_prompt="s" * 400, user_prompt="u" * 3600, max_tokens=4096, stage="t9_write")
    est = estimate_call_usd(req, "sonnet-5-5")
    assert est > 0.01                       # the live check's cap: a T9 write is refused before any attempt
    assert estimate_call_usd(req, "gpt-6-luna@acc3") > 0
    assert estimate_call_usd(req, None) == 0.0


# ── the check ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_no_llm_row_reads_nothing_more(state):
    _, _, _, reads = state
    cb.check("t9_write", 1.0)
    cb.check("t9_write", 1.0)
    assert reads["rows"] == 1 and reads["spend"] == [] and reads["kind"] == 0
    assert cb.active() is False


def test_a_hard_stage_cap_refuses_the_call(state):
    rows, _, _, reads = state
    rows["stage:t9_write"] = _row("stage:t9_write", day=0.01)
    with pytest.raises(cb.CentralBudgetExceeded) as e:
        cb.check("t9_write", 0.04)
    assert isinstance(e.value, cost_guard.BudgetExceeded) and isinstance(e.value, RuntimeError)
    assert (e.value.scope, e.value.axis, e.value.provider) == ("stage:t9_write", "per_day", "llm")
    assert "llm/stage:t9_write" in str(e.value) and "was not sent" in str(e.value)
    assert reads["stops"] and reads["stops"][0][0] == "stage:t9_write"
    cb.check("t2_generate", 0.04)                                             # another stage is not in that scope


def test_an_alert_only_cap_never_refuses(state):
    rows, spend, _, _ = state
    rows["app:cis"] = _row("app:cis", day=1.0, hard=False)
    spend["app:cis"] = cb.Spend(day_usd=5.0)
    cb.check("s1_generate", 0.5)


def test_the_month_cap_of_adhoc_and_eval(state):
    rows, spend, _, _ = state
    rows["adhoc"] = _row("adhoc", month=2.0)
    rows["eval"] = _row("eval", month=25.0)
    spend["adhoc"] = cb.Spend(day_usd=0.1, month_usd=1.99)
    spend["eval"] = cb.Spend(day_usd=0.0, month_usd=25.0)
    with pytest.raises(cb.CentralBudgetExceeded, match="llm/adhoc"):
        cb.check("adhoc_aa1099", 0.02)
    cb.check("s1_generate", 1.0)                                              # neither scope applies
    with bind_eval_stage("writing_memory_eval", "run-1"):
        with pytest.raises(cb.CentralBudgetExceeded, match="llm/eval"):
            cb.check("t9_write", 0.001)


def test_a_job_kind_cap_reads_the_bound_job_kind_and_its_run(state):
    rows, spend, kinds, reads = state
    rows["job:place_extract"] = _row("job:place_extract", run=0.5)
    kinds[JOB] = "place_extract"
    spend["job:place_extract"] = cb.Spend(day_usd=3.0, run_usd=0.49)
    cb.check("a3_place_days", 0.005)                                          # no job bound: no job scope
    assert reads["kind"] == 0
    with bind_job(JOB):
        with pytest.raises(cb.CentralBudgetExceeded) as e:
            cb.check("a3_place_days", 0.02)
    assert e.value.axis == "per_run" and reads["spend"][-1] == ("job:place_extract", JOB)
    with bind_job(JOB):                                                       # the kind is cached per job
        with pytest.raises(cb.CentralBudgetExceeded):
            cb.check("a3_place_days", 0.02)
    assert reads["kind"] == 1


def test_unreadable_rows_or_spend_fail_open(state, monkeypatch):
    rows, _, _, _ = state

    def boom(*a, **k):
        raise OSError("db down")
    monkeypatch.setattr(cb, "loader_rows", boom)
    cb.check("t9_write", 100.0)
    rows["stage:t9_write"] = _row("stage:t9_write", day=0.0)
    cb.invalidate()
    monkeypatch.setattr(cb, "loader_rows", lambda: dict(rows))
    monkeypatch.setattr(cb, "loader_spend", boom)
    cb.check("t9_write", 100.0)


def test_the_switch_turns_it_off(state, monkeypatch):
    rows, _, _, reads = state
    rows["stage:t9_write"] = _row("stage:t9_write", day=0.0)
    monkeypatch.setenv(cb.SWITCH_ENV, "off")
    cb.check("t9_write", 1.0)
    assert reads["rows"] == 0 and cb.active() is False


def test_the_spend_is_cached(state):
    rows, _, _, reads = state
    rows["app:cis"] = _row("app:cis", day=100.0, hard=False)
    for _ in range(3):
        cb.check("s1_generate", 0.01)
    assert reads["spend"] == [("app:cis", None)]


# ── the gateway ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _stage(monkeypatch, stage, model="gpt-6-luna"):
    cfg = StageConfig(stage=stage, role="writer", provider="claude", model_id=model, account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)


def test_the_gateway_refuses_before_any_attempt_and_logs_a_budget_row(world, monkeypatch, state):
    rows_, _, _, _ = state
    rows_["stage:t9_write"] = _row("stage:t9_write", day=0.01)
    logged: list = []

    async def record_calls(rows):
        logged.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    _stage(monkeypatch, "t9_write")
    with pytest.raises(cb.CentralBudgetExceeded):
        world.generate(stage="t9_write")
    assert world.calls == []
    call_log.wait_for_pending_writes()
    assert len(logged) == 1
    row = logged[0]
    assert (row["status"], row["error_class"], row["cost_usd"], row["stage"]) == ("refused", "budget", 0.0,
                                                                                    "t9_write")
    assert "CentralBudgetExceeded" in row["quality_signal"]["error"]


def test_the_gateway_calls_as_before_under_an_alert_only_cap(world, monkeypatch, state):
    rows_, spend, _, _ = state
    rows_["stage:t9_write"] = _row("stage:t9_write", day=0.01, hard=False)
    spend["stage:t9_write"] = cb.Spend(day_usd=3.0)
    _stage(monkeypatch, "t9_write")
    resp = world.generate(stage="t9_write")
    assert resp.content == "from acc3" and len(world.calls) == 1


def test_the_gateway_reads_no_estimate_without_a_cap(world, monkeypatch, state):
    called = []
    monkeypatch.setattr("shared.llm_client.client.estimate_call_usd", lambda *a: called.append(a) or 0.0)
    _stage(monkeypatch, "t9_write")
    world.generate(stage="t9_write")
    assert called == []


# ── the bell ───────────────────────────────────────────────────────────────────────────────────────────────────────

class Conn:
    def __init__(self, recent=False):
        self.recent = recent
        self.executed: list = []

    async def fetchval(self, sql, *args):
        return self.recent

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def test_one_bell_per_scope_and_day(monkeypatch):
    sent = []

    async def email(conn, **kw):
        sent.append(kw)
    from shared import ops_alerts
    monkeypatch.setattr(ops_alerts, "enqueue_spend_budget_email", email)
    summary = cb.summary(_row("stage:t9_write", day=0.01), cb.Spend(day_usd=0.02))
    conn = Conn()
    assert asyncio.run(cost_guard.maybe_alert_central(conn, summary, alert_pct=80, stopped=True, message="m")) is True
    assert conn.executed[0][1][2] == "llm:stage:t9_write"
    assert sent and sent[0]["stopped"] is True and sent[0]["summary"]["provider"] == "llm"
    assert asyncio.run(cost_guard.maybe_alert_central(Conn(recent=True), summary, alert_pct=80, stopped=False,
                                                      message="m")) is False


class SpendConn:
    def __init__(self):
        self.calls: list = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {"day": 0.25, "win": 3.5}

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 0.1


def test_read_spend_uses_the_scope_query_and_the_month_window_only_with_a_month_cap():
    now = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    conn = SpendConn()
    s = asyncio.run(cb.read_spend(conn, _row("stage:t9_write", day=1.0), now=now))
    assert s == cb.Spend(day_usd=0.25, month_usd=None, run_usd=None)
    sql, args = conn.calls[-1]
    assert "l.stage = $3" in sql and args[0] == args[1] and args[2] == "t9_write"
    conn = SpendConn()
    s = asyncio.run(cb.read_spend(conn, _row("eval", month=25.0), now=now))
    assert s.month_usd == 3.5 and conn.calls[0][1][0].day == 1 and conn.calls[0][1][2] == list(cb.EVAL_STAGES)
    conn = SpendConn()
    s = asyncio.run(cb.read_spend(conn, _row("job:place_extract", run=1.0), job_id=JOB, now=now))
    assert s.run_usd == 0.1 and "JOIN shared.job" in conn.calls[0][0] and conn.calls[1][1] == (JOB,)
    conn = SpendConn()
    asyncio.run(cb.read_spend(conn, _row("adhoc", day=1.0), now=now))
    assert "adhoc" in conn.calls[0][0] and len(conn.calls[0][1]) == 2
