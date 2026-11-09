"""AA-1037 R4 slice 1 — the tenant_billing_tick without a database: the account move per Booking billing status
(``services.tenant_account.billing.decide``), the fail-soft read, the Dev guard and the per-tenant failure count on a
scripted pool, the job kind and its hourly schedule, the sampler part. On Postgres:
tests/integration/test_aa1037_tenant_billing_tick.py. Names are invented."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from services.notifications import EventType
from services.tenant_account import billing
from shared.jobs import registry

T1, T2, T3 = ("00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2",
              "00000000-0000-4000-8000-0000000000b3")


@pytest.mark.parametrize(("view", "account", "reason", "content", "expected"), [
    ("past_due", "active", None, "active", ("suspended", "billing", EventType.TENANT_BILLING_SUSPENDED)),
    ("past_due", "trial", None, "trialing", ("suspended", "billing", EventType.TENANT_BILLING_SUSPENDED)),
    ("past_due", "suspended", "billing", "active", None),
    ("past_due", "offboarding", None, "canceled", None),
    ("warn", "active", None, "active", ("past_due", None, EventType.TENANT_BILLING_OVERDUE)),
    ("warn", "past_due", None, "past_due", ("past_due", None, EventType.TENANT_BILLING_OVERDUE)),
    ("warn", "trial", None, "trialing", None),
    ("ok", "suspended", "billing", "active", ("active", None, EventType.TENANT_BILLING_RESTORED)),
    ("ok", "suspended", "billing", "past_due", ("past_due", None, EventType.TENANT_BILLING_RESTORED)),
    ("ok", "suspended", "compliance", "active", None),
    ("ok", "past_due", None, "active", ("active", None, None)),
    ("ok", "past_due", None, "past_due", None),
    ("ok", "active", None, "active", None),
])
def test_decide(view, account, reason, content, expected):
    assert billing.decide(view, account, reason, content) == expected


class Conn:
    """Scripted connection: answers by SQL fragment, records executes."""

    def __init__(self, status_rows=None, tenants=None, error=None, fail_tenant=None):
        self.status_rows, self.tenants, self.error, self.fail_tenant = status_rows or [], tenants or {}, error, \
            fail_tenant
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        if "v_tenant_billing_status" in sql:
            if self.error:
                raise self.error
            return self.status_rows
        if "FROM shared.tenants WHERE tenant_id = ANY" in sql:
            return [{"tenant_id": k, **v} for k, v in self.tenants.items()]
        return []

    async def fetchrow(self, sql, *args):
        if args[0] == self.fail_tenant:
            raise RuntimeError("boom")
        t = self.tenants.get(args[0])
        return None if t is None else {"account_status": t["account_status"], "suspend_reason": None,
                                       "kind": t["kind"]}

    async def fetchval(self, sql, *args):
        if "product = 'content'" in sql:
            return "active"
        return False                                       # never notified today

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    @asynccontextmanager
    async def transaction(self):
        yield self


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _row(tid, status, days=0, paid_through=None):
    return {"tenant_id": tid, "status": status, "oldest_overdue_days": days, "overdue_invoices": 1 if days else 0,
            "overdue_amount": '{"USD": 50}' if days else {}, "paid_through": paid_through, "warn_after_days": 1,
            "suspend_after_days": 30}


@pytest.mark.asyncio
async def test_unreadable_view_skips():
    for err in (asyncpg.UndefinedTableError("x"), asyncpg.InsufficientPrivilegeError("x")):
        out = await billing.tick(Pool(Conn(error=err)), job_id="j")
        assert out["outcome"] == "skipped"


@pytest.mark.asyncio
async def test_tick_moves_and_counts(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    conn = Conn(
        status_rows=[_row(T1, "past_due", 40), _row(T2, "warn", 3, date(2026, 11, 30)), _row(T3, "ok"),
                     _row("00000000-0000-4000-8000-0000000000ff", "ok")],
        tenants={T1: {"account_status": "active", "kind": "demo"}, T2: {"account_status": "active", "kind": "demo"},
                 T3: {"account_status": "active", "kind": "agency"}},
    )
    out = await billing.tick(Pool(conn), job_id="j7")
    assert (out["suspended"], out["warned"], out["skipped_real"], out["not_in_cis"], out["notified"]) == (1, 1, 1, 1, 2)
    assert out["paid_through_moved"] == 1
    moves = [args for sql, args in conn.executed if sql.startswith("UPDATE shared.tenants")]
    assert moves == [(T1, "suspended", "billing"), (T2, "past_due", None)]
    actors = {args[0] for sql, args in conn.executed if "set_config('aa.actor'" in sql}
    assert actors == {"job:tenant_billing_tick:j7"}
    notices = [args for sql, args in conn.executed if "INSERT INTO shared.notifications" in sql]
    assert [n[2] for n in notices] == ["tenant.billing.suspended", "tenant.billing.overdue"]
    assert notices[0][6] == ["tenant", "admin", "finance"]


@pytest.mark.asyncio
async def test_one_failing_tenant_never_holds_the_others(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    conn = Conn(status_rows=[_row(T1, "past_due", 40), _row(T2, "past_due", 40)],
                tenants={T1: {"account_status": "active", "kind": "agency"},
                         T2: {"account_status": "active", "kind": "agency"}}, fail_tenant=T1)
    out = await billing.tick(Pool(conn), job_id="j8")
    assert (out["failed"], out["suspended"]) == (1, 1)
    assert (await billing.tick(Pool(Conn()), job_id="j9"))["tenants"] == 0


def test_kind_registered():
    from services import jobs  # noqa: F401  (registers every kind)
    from services.jobs import tenant_billing_tick_job as job

    k = registry.get_kind(job.KIND)
    assert k is not None and k.concurrency == 1 and k.max_attempts == 2 and not k.deploy_blocking


@pytest.mark.asyncio
async def test_enqueue_hourly_and_switch(monkeypatch):
    from services.jobs import tenant_billing_tick_job as job

    now = datetime(2026, 11, 5, 9, 12, tzinfo=timezone.utc)
    assert job.schedule_key(now) == "tenant_billing_tick:2026-11-05T09"
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    assert await job.enqueue_hourly(pool, now=now) == ("job-1", True, "tenant_billing_tick:2026-11-05T09")
    pool.fetchval = AsyncMock(return_value=True)
    assert await job.enqueue_hourly(pool, now=now) == (None, False, "tenant_billing_tick:2026-11-05T09")
    monkeypatch.setenv("TENANT_BILLING_TICK", "off")
    assert job.schedule_enabled() is False
    monkeypatch.delenv("TENANT_BILLING_TICK")
    assert job.schedule_enabled() is True


@pytest.mark.asyncio
async def test_run_sets_the_result(monkeypatch):
    from services.jobs import tenant_billing_tick_job as job

    monkeypatch.setattr(billing, "tick", AsyncMock(return_value={"outcome": "ok", "warned": 1}))
    ctx = MagicMock()
    ctx.job_id, ctx.payload, ctx.pool = "j", {"trigger": "schedule"}, object()
    out = await job.run(ctx)
    assert out == {"kind": "tenant_billing_tick", "trigger": "schedule", "outcome": "ok", "warned": 1}
    ctx.set_result.assert_called_once_with(out)


@pytest.mark.asyncio
async def test_sampler_part(monkeypatch):
    from api.core import ops_metrics_sampler as s
    from services.jobs import tenant_billing_tick_job as job

    monkeypatch.setattr(job, "enqueue_hourly", AsyncMock(return_value=("job-2", True, "k")))
    out: dict = {}
    await s._tenant_billing_tick_part(object(), {job.KIND: 1}, datetime.now(timezone.utc), out)
    assert out["tenant_billing_tick"] == {"job_id": "job-2", "key": "k"}
    out = {}
    await s._tenant_billing_tick_part(object(), {}, datetime.now(timezone.utc), out)
    assert out == {}
