"""AA-845 — Jev credit alerts: the low-credit check (Q845-2) and the e-mail half of the 402 exhausted alert (Q845-3).

Covers:
  - shared/jev_credit: estimate math, below-threshold rule, bell throttle (a real unread bell in 24 h blocks a new
    one; a test check skips the throttle and is marked), e-mail queued only with a new bell, failures never raise.
  - shared/llm_client/decide.maybe_alert_jev_credit: a new exhausted bell queues one e-mail; a throttled one does
    not; test=True skips the throttle; an e-mail failure keeps the bell's True.
  - shared/ops_alerts: enqueue_jev_credit_email keys (one per template, real/test, UTC day), not configured -> None,
    render_jev_credit for both templates and the test marker; the job renders both templates.
  - POST /admin/decisions/jev-credit/check and the new GET fields; the staff route policy.
  - api/core/ops_metrics_sampler: the Jev check runs from the loop (first tick, then every N s) and never raises.
  - the 402 sweep SQL also matches what decide() writes ('credit_exhausted', '... (breaker open)'), and only the
    real 402s after the latest top-up and the latest successful canary raise the bell (S246 review B1).
  - the low bell's throttle ignores the shared read flag; test bells go to admins only and are not "last alert".
Mock-only: no DB, no SES, no TypeSafe call."""
import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from api.core import ops_metrics_sampler as sampler
from api.core.staff_authz import POLICY, SPEND_WRITE
from api.routers import admin_decisions as routes
from api.routers import admin_llm_ops
from services.jobs import ops_alert_email_job
from shared import jev_credit as jc
from shared import ops_alerts
from shared.llm_client import decide as d

_NOW = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
MAIL_ENV = {"SES_FROM_ADDRESS": "no-reply@example.com", "ALERT_EMAIL_TO": "ops@example.com"}


class _Conn:
    """A fake asyncpg connection/pool: serves the top-up total, the Jev spend and the throttle check by SQL
    text; records notification inserts; transaction()/acquire() are async context managers."""

    def __init__(self, *, total=20.0, first_on=date(2026, 10, 4), spent=18.0, recent=None,
                 fail_insert=False, fail_estimate=False):
        self.total, self.first_on, self.spent, self.recent = total, first_on, spent, recent
        self.fail_insert, self.fail_estimate = fail_insert, fail_estimate
        self.bells = []
        self.locks = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False

        return _Tx()

    async def fetchrow(self, sql, *args):
        if self.fail_estimate:
            raise RuntimeError("db down")
        if "FROM shared.jev_credit_topup" in sql:
            return {"total": self.total, "first_on": self.first_on}
        if "coalesce(sum(cost_usd)" in sql:
            return {"spent": self.spent}
        return None

    async def fetchval(self, sql, *args):
        assert "coalesce(payload->>'test', 'false') <> 'true'" in sql   # test bells never throttle a real one
        return self.recent

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            self.locks.append(args[0])
            return "SELECT 1"
        if "INSERT INTO shared.notifications" in sql:
            if self.fail_insert:
                raise RuntimeError("insert failed")
            self.bells.append({"event": args[1], "payload": json.loads(args[2]), "roles": args[3]})
            return "INSERT 0 1"
        return "OK"


# ── pure rules ─────────────────────────────────────────────────────────────────────────────────

def test_estimate_and_below_rules():
    assert jc.estimate_from(None, None) is None and jc.estimate_from(20.0, None) is None
    assert jc.estimate_from(20.0, 18.25) == 1.75
    assert jc.is_below(2.99, 3.0) and not jc.is_below(3.0, 3.0) and not jc.is_below(None, 3.0)
    assert jc.LOW_CREDIT_THRESHOLD_USD == 3.0     # Nghiệp S246 (AA-845 panel): warn below $3
    assert "[TEST]" in jc.low_message(1.0, 3.0, test=True) and "[TEST]" not in jc.low_message(1.0, 3.0)


# ── check_low_credit ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_below_threshold_writes_one_bell_and_queues_the_email():
    conn = _Conn(total=20.0, spent=18.0)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(return_value="job-1")) as eq:
        out = await jc.check_low_credit(conn, source="ops_sampler")
    assert out["estimated_left_usd"] == 2.0 and out["below_threshold"] is True
    assert out["alerted"] is True and out["email_job_id"] == "job-1" and out["test"] is False
    assert [b["event"] for b in conn.bells] == [jc.JEV_CREDIT_LOW_EVENT]
    assert conn.bells[0]["payload"]["test"] is False and conn.bells[0]["payload"]["estimated_left_usd"] == 2.0
    assert conn.bells[0]["roles"] == ["admin", "content"]
    assert conn.locks == [jc._LOW_ALERT_LOCK_KEY]
    kw = eq.await_args.kwargs
    assert kw["template"] == ops_alerts.TEMPLATE_JEV_CREDIT_LOW and kw["test"] is False
    assert kw["details"]["threshold_usd"] == 3.0 and kw["details"]["estimated_left_usd"] == 2.0


@pytest.mark.asyncio
async def test_above_threshold_and_no_topup_are_quiet():
    for conn in (_Conn(total=20.0, spent=11.81), _Conn(total=None, first_on=None, spent=0.0)):
        with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock()) as eq:
            out = await jc.check_low_credit(conn, source="ops_sampler")
        assert out["below_threshold"] is False and out["alerted"] is False and conn.bells == []
        eq.assert_not_awaited()
    assert out["estimated_left_usd"] is None and out["spent_since_first_topup_usd"] is None


@pytest.mark.asyncio
async def test_unread_bell_in_24h_throttles_the_bell_and_the_email():
    conn = _Conn(spent=19.5, recent=1)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock()) as eq:
        out = await jc.check_low_credit(conn, source="ops_sampler")
    assert out["below_threshold"] is True and out["alerted"] is False and conn.bells == []
    eq.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_threshold_is_a_marked_test_that_skips_the_throttle():
    conn = _Conn(spent=11.81, recent=1)          # a real unread bell exists and $8.19 is above $3
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(return_value="job-t")) as eq:
        out = await jc.check_low_credit(conn, source="admin:u1", threshold_usd=1000.0)
    assert out["test"] is True and out["threshold_usd"] == 1000.0 and out["alerted"] is True
    assert conn.bells[0]["payload"]["test"] is True and conn.bells[0]["payload"]["message"].startswith("[TEST]")
    assert conn.bells[0]["roles"] == ["admin"]              # a content user never sees a [TEST] bell
    assert eq.await_args.kwargs["test"] is True


@pytest.mark.asyncio
async def test_bell_or_email_failure_never_raises():
    conn = _Conn(spent=19.0, fail_insert=True)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock()) as eq:
        out = await jc.check_low_credit(conn, source="ops_sampler")
    assert out["below_threshold"] is True and out["alerted"] is False
    eq.assert_not_awaited()

    conn = _Conn(spent=19.0)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(side_effect=RuntimeError("queue"))):
        out = await jc.check_low_credit(conn, source="ops_sampler")
    assert out["alerted"] is True and out["email_job_id"] is None


# ── the exhausted (402) alert now also queues the e-mail ───────────────────────────────────────

@pytest.mark.asyncio
async def test_new_exhausted_bell_queues_one_email():
    conn = _Conn()
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(return_value="job-x")) as eq:
        assert await d.maybe_alert_jev_credit(conn, source="decide") is True
    assert [b["event"] for b in conn.bells] == [d.JEV_CREDIT_EXHAUSTED_EVENT]
    assert conn.bells[0]["payload"]["test"] is False and conn.bells[0]["roles"] == ["admin", "content"]
    kw = eq.await_args.kwargs
    assert kw["template"] == ops_alerts.TEMPLATE_JEV_CREDIT_EXHAUSTED and kw["test"] is False
    assert kw["details"]["source"] == "decide"


@pytest.mark.asyncio
async def test_throttled_exhausted_alert_sends_no_email():
    conn = _Conn(recent=1)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock()) as eq:
        assert await d.maybe_alert_jev_credit(conn, source="402_sweep") is False
    eq.assert_not_awaited()
    assert conn.bells == []


@pytest.mark.asyncio
async def test_test_exhausted_alert_skips_the_throttle_and_is_marked():
    conn = _Conn(recent=1)
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(return_value="job-t")) as eq:
        assert await d.maybe_alert_jev_credit(conn, source="admin:u1", test=True) is True
    assert conn.bells[0]["payload"]["test"] is True and conn.bells[0]["payload"]["message"].startswith("[TEST]")
    assert conn.bells[0]["roles"] == ["admin"]
    assert eq.await_args.kwargs["test"] is True


@pytest.mark.asyncio
async def test_exhausted_email_failure_keeps_the_bell_result():
    conn = _Conn()
    with patch.object(ops_alerts, "enqueue_jev_credit_email", AsyncMock(side_effect=RuntimeError("queue"))):
        assert await d.maybe_alert_jev_credit(conn, source="canary") is True
    assert len(conn.bells) == 1


# ── ops_alerts: enqueue + render ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_not_configured_returns_none(monkeypatch):
    monkeypatch.delenv("SES_FROM_ADDRESS", raising=False)
    with patch("shared.jobs.queue.enqueue", AsyncMock()) as q:
        assert await ops_alerts.enqueue_jev_credit_email(
            object(), template=ops_alerts.TEMPLATE_JEV_CREDIT_LOW, details={}) is None
    q.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_keys_one_per_template_real_or_test_and_day(monkeypatch):
    for k, v in MAIL_ENV.items():
        monkeypatch.setenv(k, v)
    with patch("shared.jobs.queue.enqueue", AsyncMock(side_effect=[("j1", True), ("j1", False), ("j2", True)])) as q:
        assert await ops_alerts.enqueue_jev_credit_email(
            object(), template=ops_alerts.TEMPLATE_JEV_CREDIT_LOW, details={"estimated_left_usd": 2.0},
            now=_NOW) == "j1"
        assert await ops_alerts.enqueue_jev_credit_email(
            object(), template=ops_alerts.TEMPLATE_JEV_CREDIT_LOW, details={}, now=_NOW) is None
        assert await ops_alerts.enqueue_jev_credit_email(
            object(), template=ops_alerts.TEMPLATE_JEV_CREDIT_EXHAUSTED, details={}, test=True, now=_NOW) == "j2"
    keys = [c.kwargs["idempotency_key"] for c in q.await_args_list]
    assert keys == ["ops_alert_email:jev_credit_low:real:2026-10-21", "ops_alert_email:jev_credit_low:real:2026-10-21",
                    "ops_alert_email:jev_credit_exhausted:test:2026-10-21"]
    first = q.await_args_list[0]
    assert first.args[1] == ops_alerts.KIND and first.args[2]["template"] == "jev_credit_low"
    assert first.args[2]["estimated_left_usd"] == 2.0 and first.args[2]["test"] is False
    with pytest.raises(ValueError):
        await ops_alerts.enqueue_jev_credit_email(object(), template="spend_budget", details={})


def test_render_low_and_exhausted_and_test_marker():
    low = ops_alerts.render_jev_credit(
        {"template": "jev_credit_low", "period": "2026-10-21", "estimated_left_usd": 2.4, "threshold_usd": 3.0,
         "total_topped_up_usd": 20.0, "first_topup_on": "2026-10-04", "spent_since_first_topup_usd": 17.6,
         "source": "ops_sampler", "message": "m", "test": False},
        to=("ops@example.com",), base_url="https://admin.example", environment="dev")
    assert low.subject == "[AA-CIS dev] Jev credit low: about $2.40 left (alert below $3.00)"
    assert "https://admin.example/admin/ai-gateway?tab=providers&view=jev-credit" in low.text and "TEST" not in low.text
    assert "<script" not in low.html

    ex = ops_alerts.render_jev_credit({"template": "jev_credit_exhausted", "period": "2026-10-21",
                                       "source": "decide", "test": True},
                                      to=("ops@example.com",), base_url="https://admin.example", environment="dev")
    assert ex.subject == "[TEST] [AA-CIS dev] Jev credit exhausted (TypeSafe 402)"
    assert "TEST send" in ex.text and "402 billing_error" in ex.text


def test_the_job_renders_both_jev_templates():
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_JEV_CREDIT_LOW] is ops_alerts.render_jev_credit
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_JEV_CREDIT_EXHAUSTED] is ops_alerts.render_jev_credit


# ── route + policy ─────────────────────────────────────────────────────────────────────────────

def _req(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


def test_check_route_policy_is_spend_write():
    # S246 review: the response carries money (top-ups, estimate) like GET jev-credit (spend.read), and the route
    # belongs to the Jev credit ledger (spend.write), so spend.write, not ops.write.
    assert POLICY[("POST", "/admin/decisions/jev-credit/check")] == SPEND_WRITE


def test_check_route_body_bounds():
    assert routes.JevCreditCheck().threshold_usd is None
    with pytest.raises(ValidationError):
        routes.JevCreditCheck(threshold_usd=0)
    with pytest.raises(ValidationError):
        routes.JevCreditCheck(threshold_usd=5000)


@pytest.mark.asyncio
async def test_check_route_real_and_test(monkeypatch):
    monkeypatch.setattr(routes, "verify_admin_secret", lambda _s: None)
    pool = _Conn(spent=11.81)
    with patch.object(jc, "check_low_credit", AsyncMock(return_value={"below_threshold": False, "alerted": False,
                                                                       "test": False})) as ck, \
         patch.object(d, "maybe_alert_jev_credit", AsyncMock(return_value=True)) as ex:
        out = await routes.check_jev_credit(_req(pool), None, x_admin_secret="x", x_admin_user_id="u1")
        assert out["exhausted_test_alerted"] is False
        assert ck.await_args.kwargs == {"source": "admin:u1", "threshold_usd": None}
        ex.assert_not_awaited()
        out = await routes.check_jev_credit(
            _req(pool), routes.JevCreditCheck(threshold_usd=1000, test_exhausted_alert=True),
            x_admin_secret="x", x_admin_user_id="u1")
    assert ck.await_args.kwargs["threshold_usd"] == 1000 and out["exhausted_test_alerted"] is True
    assert ex.await_args.kwargs == {"source": "admin:u1", "test": True}


def test_get_reports_the_low_threshold_state(monkeypatch):
    monkeypatch.setattr(routes, "verify_admin_secret", lambda _s: None)

    class _P(_Conn):
        async def fetch(self, sql, *args):
            return [{"id": 1, "topped_up_on": date(2026, 10, 4), "amount_usd": 20.0, "note": None,
                     "created_by": "admin:x", "created_at": _NOW}]

        async def fetchrow(self, sql, *args):
            if "max(created_at) AS last_at" in sql:
                assert "coalesce(payload->>'test', 'false') <> 'true'" in sql   # a test bell is not an alert
                return {"last_at": _NOW if args[0] == jc.JEV_CREDIT_LOW_EVENT else None}
            return await super().fetchrow(sql, *args)

    out = asyncio.run(routes.jev_credit(_req(_P(spent=18.5)), x_admin_secret="x"))
    assert out["low_threshold_usd"] == 3.0 and out["below_low_threshold"] is True
    assert out["last_low_alert_at"] == _NOW.isoformat() and out["last_exhausted_alert_at"] is None


# ── sampler hook ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jev_tick_never_raises():
    with capture_logs() as logs, patch.object(jc, "check_low_credit", AsyncMock(side_effect=RuntimeError("x"))):
        assert await sampler.jev_credit_tick(object()) is None
    assert [e["event"] for e in logs] == ["jev_credit_check_error"]

    async def _hang(*a, **kw):
        await asyncio.sleep(3600)

    with capture_logs() as logs, patch.object(jc, "check_low_credit", _hang):
        assert await asyncio.wait_for(sampler.jev_credit_tick(object(), part_timeout_s=0.02), 2) is None
    assert logs[0]["error"].startswith("TimeoutError")


@pytest.mark.asyncio
async def test_loop_runs_the_jev_check_on_the_first_tick_then_on_its_own_interval():
    app = SimpleNamespace(state=SimpleNamespace(pool=object()))
    ticks = []

    async def _sample(*a, **kw):
        ticks.append(1)
        return {"sampled_at": "t"}

    with patch.object(sampler, "sample_once", _sample), \
         patch.object(sampler, "jev_credit_tick", AsyncMock(return_value={})) as jt:
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(
            app, 0.005, 0, client_factory=lambda: object(), jev_check_every_s=3600))
        for _ in range(200):
            if len(ticks) >= 5:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(ticks) >= 5 and jt.await_count == 1


# ── 402 sweep matches what decide() writes ─────────────────────────────────────────────────────

def test_402_sweep_matches_the_decide_error_texts():
    """S246 lesson: the sweep matched only '%402%' while decide() writes 'credit_exhausted', so it counted 0 of
    5,683 rows on 10/10. The real-Postgres check is tests/integration/test_aa845_jev_credit_sql.py."""
    import inspect

    sql = admin_llm_ops._JEV_402_SWEEP_SQL
    assert "error LIKE 'credit_exhausted%'" in sql and "error LIKE '%402%'" in sql
    src = inspect.getsource(d._decide)
    assert 'error="credit_exhausted"' in src and 'error="credit_exhausted (breaker open)"' in src


def test_low_bell_throttle_ignores_the_read_flag():
    """S246 review: is_read is one flag for every staff user, so an `is_read = FALSE` throttle let the 30-min check
    write a new low bell every 30 min after anyone opened the bell."""
    assert "is_read" not in jc.LOW_ALERT_RECENT_SQL
    assert "interval '24 hours'" in jc.LOW_ALERT_RECENT_SQL


def test_402_sweep_cuts_at_the_latest_topup_and_canary():
    sql = admin_llm_ops._JEV_402_SWEEP_SQL
    assert "max(created_at) FROM shared.jev_credit_topup" in sql and "$1::timestamptz" in sql
    assert "NOT LIKE '%(breaker open)%'" in sql and "AS n_open" in sql


class _SweepConn(_Conn):
    def __init__(self, *, n, n_open):
        super().__init__()
        self.n, self.n_open, self.sweep_args = n, n_open, None

    async def fetchrow(self, sql, *args):
        if "FROM shared.decision_log" in sql:
            self.sweep_args = args
            return {"n": self.n, "n_open": self.n_open, "last_seen": _NOW}
        return await super().fetchrow(sql, *args)


@pytest.mark.asyncio
async def test_sweep_alerts_only_on_open_402s(monkeypatch):
    """B1: rows from before a top-up (or before this run's successful canary) are reported, never alerted."""
    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    monkeypatch.setattr(admin_llm_ops, "_last_canary_ok_at", _NOW)
    for n, n_open, alerts in ((40, 0, 0), (40, 2, 1)):
        conn = _SweepConn(n=n, n_open=n_open)
        with patch.object(d, "maybe_alert_jev_credit", AsyncMock(return_value=True)) as al:
            out = await admin_llm_ops.sweep_jev_402(_req(conn), x_admin_secret="x")
        assert conn.sweep_args == (_NOW,)
        assert out["errors_24h"] == n and out["open_402s"] == n_open
        assert al.await_count == alerts and out["alerted"] is bool(alerts)


@pytest.mark.asyncio
async def test_successful_canary_sets_the_sweep_cut(monkeypatch):
    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    monkeypatch.setattr(admin_llm_ops, "_last_canary_ok_at", None)
    with patch.object(d, "_call_jev", AsyncMock(side_effect=d.JevBillingError("402"))), \
         patch.object(d, "_trip_jev_breaker"), patch.object(d, "jev_breaker_open", return_value=True), \
         patch.object(d, "maybe_alert_jev_credit", AsyncMock(return_value=True)):
        out = await admin_llm_ops.check_jev_canary(_req(_Conn()), x_admin_secret="x")
    assert out["billing_error"] is True and admin_llm_ops._last_canary_ok_at is None
    with patch.object(d, "_call_jev", AsyncMock(return_value={})), patch.object(d, "reset_jev_breaker"), \
         patch.object(d, "jev_breaker_open", return_value=False):
        out = await admin_llm_ops.check_jev_canary(_req(_Conn()), x_admin_secret="x")
    assert out["credit_ok"] is True
    assert admin_llm_ops._last_canary_ok_at is not None
    assert admin_llm_ops._last_canary_ok_at.isoformat() == out["checked_at"]
