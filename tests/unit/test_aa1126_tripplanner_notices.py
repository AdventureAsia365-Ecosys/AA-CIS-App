"""AA-1126 L2 / L4 (S284, offline): the relay of TripPlanner's AI notice events into CIS bell rows
(services/tripplanner_notices.py) and its worker tick. No database: a fake connection; invented ids only."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest

from services import tripplanner_notices as tn
from shared import tenant_ai_notices

AGENCY = "5f1a2b3c-0000-4000-8000-a11260000011"
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, rows=None, read_error=None, write_error=None):
        self.rows, self.read_error, self.write_error = rows or [], read_error, write_error
        self.writes: list = []

    async def fetch(self, sql, *args):
        assert sql == tn.PENDING_SQL
        if self.read_error:
            raise self.read_error
        return self.rows

    async def fetchval(self, sql, *args):
        if self.write_error:
            raise self.write_error
        self.writes.append((sql, args))
        return len(self.writes)

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _row(event, payload, i=1, tenant=AGENCY):
    return {"id": i, "tenant_id": tenant, "event": event, "payload": json.dumps(payload), "created_at": NOW}


@pytest.mark.parametrize("now,start", [
    (datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)),
    (datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc), datetime(2026, 12, 1, tzinfo=timezone.utc)),
    (datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)),
])
def test_the_window_starts_at_the_previous_utc_month(now, start):
    assert tn.window_start(now) == start


def test_the_pending_read_names_the_contract_view_and_the_cis_once_only_keys():
    assert "FROM tripplanner.v_ai_notices_v1 v" in tn.PENDING_SQL
    assert "'tripplanner:' || (v.payload ->> 'month')" in tn.PENDING_SQL       # tenant_ai_notices.allowance_entity
    assert tenant_ai_notices.allowance_entity("tripplanner", NOW) == "tripplanner:2026-11"
    assert "ON CONFLICT (tenant_id, event_type, entity_id) WHERE event_type = " in tn.DEGRADED_SQL
    assert "ARRAY['admin']" in tn.DEGRADED_SQL and "INSERT INTO tripplanner" not in tn.DEGRADED_SQL


@pytest.mark.asyncio
async def test_each_event_becomes_its_bell_row():
    conn = FakeConn([_row("ai.allowance_used", {"kind": "tripplanner", "month": "2026-11", "resets_at": "2026-12-01"}),
                     _row("ai.spend_degraded", {"check": "spend_unreadable", "stage": "tp_compose",
                                                "day": "2026-11-18"}, i=2)])
    out = await tn.relay(FakePool(conn), NOW)
    assert out == {"ready": True, "allowance": 1, "degraded": 1, "skipped": 0}
    (allow_sql, allow), (deg_sql, deg) = conn.writes
    assert allow_sql == tenant_ai_notices.TENANT_ALLOWANCE_NOTICE_SQL
    assert allow[:4] == (AGENCY, "tripplanner.ai_allowance_used", "ai_allowance", "tripplanner:2026-11")
    assert json.loads(allow[4]) == {"kind": "tripplanner", "month": "2026-11", "resets_at": "2026-12-01"}
    assert deg_sql == tn.DEGRADED_SQL and deg[1] == "spend_unreadable:2026-11-18"
    body = json.loads(deg[2])
    assert "today's AI spend (stage tp_compose)" in body["message"] and body["tenant_id"] == AGENCY


@pytest.mark.asyncio
async def test_bad_rows_and_failed_writes_are_skipped_never_raised():
    conn = FakeConn([_row("ai.allowance_used", {"month": "not-a-month"}),
                     _row("ai.spend_degraded", {"stage": "tp_compose"}, i=2)])
    assert await tn.relay(FakePool(conn), NOW) == {"ready": True, "allowance": 0, "degraded": 0, "skipped": 2}
    failing = FakeConn([_row("ai.spend_degraded", {"check": "tenant_sessions", "day": "2026-11-18"})],
                       write_error=RuntimeError("synthetic"))
    assert (await tn.relay(FakePool(failing), NOW))["skipped"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncpg.exceptions.UndefinedTableError,
                                   asyncpg.exceptions.InsufficientPrivilegeError,
                                   asyncpg.exceptions.InvalidSchemaNameError])
async def test_before_tp_034_nothing_is_relayed(error):
    out = await tn.relay(FakePool(FakeConn(read_error=error("synthetic"))), NOW)
    assert out["ready"] is False and out["allowance"] == out["degraded"] == 0


@pytest.mark.asyncio
async def test_relay_safe_never_raises():
    class Broken:
        def acquire(self):
            raise ConnectionError("synthetic")
    assert (await tn.relay_safe(Broken(), NOW)) == {"ready": False, "error": "ConnectionError"}


def test_every_tripplanner_check_has_words():
    # AA-TripPlanner-Web backend/shared/spend_bell.py CHECKS (written out: another repo)
    for check in ("spend_unreadable", "tenant_budget_unreadable", "monthly_spend_unreadable", "tenant_sessions"):
        assert check in tn.CHECK_WORDS and check not in tn.degraded_message(check, None)


@pytest.mark.asyncio
async def test_the_worker_ops_tick_runs_the_relay(monkeypatch):
    from shared.jobs import worker as worker_mod

    calls = []

    async def fake_relay_safe(pool, now=None):
        calls.append(pool)
        return {"ready": True, "allowance": 1, "degraded": 0, "skipped": 0}
    monkeypatch.setattr(tn, "relay_safe", fake_relay_safe)
    w = worker_mod.Worker(pool="zz-pool", worker_id="zz-worker")
    await w._tripplanner_notices()
    assert calls == ["zz-pool"]
