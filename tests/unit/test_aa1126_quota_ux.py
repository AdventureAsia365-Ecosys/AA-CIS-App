"""AA-1126 l16 (S283) — the Content studio side of a used-up AI allowance (L1) and the budget read error under the prod
spend rules (L4).

L1: a request that would start new AI writing while the tenant's plan allowance is used up answers 402
``ai_quota_exhausted`` (code, plain message, the day it resets); a held version carries ``job_stop_code``; the agency's
own bell gets one notice a month with an Upgrade link; the hourly tick re-queues the held jobs once the tenant no
longer needs approval. L4: with ``cis.spend.environment`` = prod a budget that cannot be read lets the call go on with a
warning and one admin bell; dev keeps the fail-closed read. No database: fakes and the module loaders. Invented
tenants only."""
import asyncio
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from api.core import ai_allowance
from api.routers import v1_notifications
from services.tenant_account import lifecycle
from shared import cost_guard
from shared.llm_client import spend_environment, tenant_budget
from shared.llm_client.call_log import bind_tenant

TENANT = "5c0f2a14-8d3b-4e6a-9f21-0a1b2c3d4e5f"
NOV = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)


def _budget(spent=10.0, monthly=10.0, approval=100, hard=True):
    return cost_guard.TenantAIBudget(tenant_id=TENANT, monthly_usd=monthly, month_spent_usd=spent, alert_pct=80,
                                     approval_pct=approval, hard_stop=hard, month_calls=4)


def run(coro):
    return asyncio.run(coro)


# ── the refusal and the stop code ────────────────────────────────────────────────────────────────────────────────────

def test_the_refusal_names_the_code_the_reason_and_the_reset_day():
    body = tenant_budget.quota_refusal(NOV)
    assert body == {"code": "ai_quota_exhausted", "message": "Your plan's AI writing allowance for this month is used up.",
                    "resets_at": "2026-12-01"}
    assert tenant_budget.allowance_resets_at(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)) == date(2027, 1, 1)


def test_the_stop_code_is_set_only_for_a_job_held_by_the_tenant_budget():
    sql = tenant_budget.job_stop_code_sql("j")
    assert "j.status = 'stopped_budget'" in sql and f"j.error LIKE '{tenant_budget.APPROVAL_REASON}%'" in sql
    assert f"THEN '{tenant_budget.QUOTA_STOP_CODE}'" in sql
    assert "status = 'stopped_budget' AND error LIKE" in tenant_budget.job_stop_code_sql()


def test_the_on_hold_message_gives_the_real_reason_and_says_no_retry_is_needed():
    msg = tenant_budget.ON_HOLD_MESSAGE
    assert "allowance" in msg and "used up" in msg and "No need to try again" in msg


# ── api/core/ai_allowance ────────────────────────────────────────────────────────────────────────────────────────────

class _Pool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _check(monkeypatch, result=None, error=None):
    async def check(conn, tid):
        if error:
            raise error
        return result
    monkeypatch.setattr(cost_guard, "tenant_budget_check", check)


@pytest.mark.parametrize("budget, used", [(None, False), ("under", False), ("over", True), ("over_soft", False)])
def test_allowance_used_only_when_the_budget_holds_new_work(monkeypatch, budget, used):
    value = {None: None, "under": _budget(spent=2.0), "over": _budget(spent=10.0),
             "over_soft": _budget(spent=10.0, hard=False)}[budget]
    _check(monkeypatch, value)
    assert run(ai_allowance.allowance_used(object(), TENANT)) is used


def test_a_read_error_never_blocks_the_request(monkeypatch):
    _check(monkeypatch, error=RuntimeError("zz db down"))
    assert run(ai_allowance.allowance_used(object(), TENANT)) is False


def test_the_switch_off_skips_the_check(monkeypatch):
    _check(monkeypatch, _budget(spent=10.0))
    monkeypatch.setenv(tenant_budget.SWITCH_ENV, "off")
    assert run(ai_allowance.allowance_used(object(), TENANT)) is False


def test_refuse_answers_402_with_the_quota_code(monkeypatch):
    _check(monkeypatch, _budget(spent=12.0))
    with pytest.raises(HTTPException) as info:
        run(ai_allowance.refuse_if_allowance_used(_Pool(), TENANT, action="rewrite"))
    assert info.value.status_code == 402
    assert info.value.detail["code"] == "ai_quota_exhausted" and info.value.detail["resets_at"]
    _check(monkeypatch, _budget(spent=1.0))
    assert run(ai_allowance.refuse_if_allowance_used(_Pool(), TENANT, action="rewrite")) is None


# ── L4: the gateway check under the prod spend rules ─────────────────────────────────────────────────────────────────

@pytest.fixture
def unreadable(monkeypatch):
    """A budgeted tenant whose spend counter cannot be read; the bell writes are counted."""
    bells: list = []

    def boom(tid):
        raise RuntimeError("zz db down")
    monkeypatch.setattr(tenant_budget, "loader_tenants", lambda: frozenset({TENANT}))
    monkeypatch.setattr(tenant_budget, "loader_state", boom)
    monkeypatch.setattr(tenant_budget, "alerter_unreadable", bells.append)
    tenant_budget.invalidate()
    yield bells
    tenant_budget.invalidate()


def test_dev_keeps_the_fail_closed_read(unreadable, monkeypatch):
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "dev")
    with bind_tenant(TENANT):
        with pytest.raises(tenant_budget.TenantBudgetUnavailable):
            tenant_budget.check("t9_write")
    assert unreadable == []


def test_prod_lets_the_call_go_on_with_one_bell(unreadable, monkeypatch):
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "prod")
    with bind_tenant(TENANT):
        assert tenant_budget.check("t9_write") is None
        assert tenant_budget.check("t2_generate") is None
    assert unreadable == [TENANT]                     # paced: one bell per UNREADABLE_BELL_EVERY_SECONDS


def test_prod_still_holds_a_tenant_whose_allowance_is_used_up(monkeypatch):
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "prod")
    monkeypatch.setattr(tenant_budget, "loader_tenants", lambda: frozenset({TENANT}))
    monkeypatch.setattr(tenant_budget, "loader_state", lambda tid: _budget(spent=11.0))
    tenant_budget.invalidate()
    with bind_tenant(TENANT):
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired):
            tenant_budget.check("t9_write")
    tenant_budget.invalidate()


def test_a_failing_bell_never_reaches_the_call(unreadable, monkeypatch):
    monkeypatch.setattr(spend_environment, "spend_environment", lambda: "prod")

    def bad_bell(tid):
        raise RuntimeError("zz bell down")
    monkeypatch.setattr(tenant_budget, "alerter_unreadable", bad_bell)
    with bind_tenant(TENANT):
        assert tenant_budget.check("t9_write") is None


# ── the notices ──────────────────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, returns=1, error=None, recent=False):
        self.returns, self.error, self.recent, self.calls = returns, error, recent, []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        if sql == cost_guard._RECENT_ALERT_SQL:
            return self.recent
        return self.returns

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def test_the_tenant_notice_is_once_a_month_and_never_raises():
    conn = _Conn(returns=7)
    assert run(cost_guard.notify_tenant_allowance_used(conn, TENANT, now=NOV)) is True
    sql, args = conn.calls[0]
    assert "ON CONFLICT (tenant_id, event_type, entity_id)" in sql
    assert args[:4] == (TENANT, "ai.allowance_used", "ai_allowance", "content:2026-11")
    assert '"resets_at": "2026-12-01"' in args[4]
    assert run(cost_guard.notify_tenant_allowance_used(_Conn(returns=None), TENANT, now=NOV)) is False
    assert run(cost_guard.notify_tenant_allowance_used(_Conn(error=RuntimeError("zz")), TENANT, now=NOV)) is False


def test_the_unreadable_bell_is_once_a_day_and_never_raises():
    conn = _Conn(recent=False)
    assert run(cost_guard.maybe_alert_tenant_unreadable(conn, TENANT)) is True
    assert run(cost_guard.maybe_alert_tenant_unreadable(_Conn(recent=True), TENANT)) is False
    assert run(cost_guard.maybe_alert_tenant_unreadable(_Conn(error=RuntimeError("zz")), TENANT)) is False


@pytest.mark.parametrize("event", ["ai.allowance_used", "tripplanner.ai_allowance_used"])
def test_the_bell_entry_links_to_the_plan_page(event):
    row = {"id": 1, "event_type": event, "entity_type": "ai_allowance", "entity_id": "content:2026-11",
           "payload": {"resets_at": "2026-12-01", "kind": "content"}, "is_read": False,
           "created_at": NOV}
    item = v1_notifications._item(row)
    assert item["upgrade"] is True and item["resets_at"] == "2026-12-01"
    msg = v1_notifications._message(event, row["payload"])
    assert "used up" in msg and "1 December 2026" in msg and "Upgrade" in msg
    assert "AA-" not in msg


def test_another_bell_entry_has_no_upgrade_link():
    row = {"id": 2, "event_type": "tour.reinstated", "entity_type": "tour", "entity_id": "zz",
           "payload": {}, "is_read": True, "created_at": NOV}
    item = v1_notifications._item(row)
    assert item["upgrade"] is False and item["resets_at"] is None


# ── the hourly tick re-queues the holds ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("now, since", [(datetime(2026, 11, 17, tzinfo=timezone.utc), datetime(2026, 10, 1)),
                                        (datetime(2027, 1, 3, tzinfo=timezone.utc), datetime(2026, 12, 1))])
def test_holds_since_the_previous_month(now, since):
    assert lifecycle.allowance_holds_since(now) == since.replace(tzinfo=timezone.utc)


def test_the_tick_requeues_through_the_tenant_budget_retry(monkeypatch):
    from shared.jobs import queue
    seen = {}

    async def retry(pool, prefix, *, since, dry_run, **kw):
        seen.update(prefix=prefix, since=since, dry_run=dry_run)
        return {"requeued": 3, "held_count": 1}
    monkeypatch.setattr(queue, "retry_by_reason", retry)
    assert run(lifecycle.resume_allowance_holds(object(), NOV)) == 3
    assert seen == {"prefix": queue.RETRY_REASON_TENANT_BUDGET, "since": datetime(2026, 10, 1, tzinfo=timezone.utc),
                    "dry_run": False}


def test_a_failing_requeue_never_stops_the_tick(monkeypatch):
    from shared.jobs import queue

    async def retry(*a, **kw):
        raise RuntimeError("zz queue down")
    monkeypatch.setattr(queue, "retry_by_reason", retry)
    assert run(lifecycle.resume_allowance_holds(object(), NOV)) == 0
