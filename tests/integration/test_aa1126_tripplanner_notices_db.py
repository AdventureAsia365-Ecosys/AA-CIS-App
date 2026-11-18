"""AA-1126 L2 / L4 (S284) — on a real database, as the CIS runtime role cis_app: the relay of TripPlanner's AI notice
events (services/tripplanner_notices.py) writes the agency's bell row and the admin bell row once each, from a
stand-in of TripPlanner's contract view tripplanner.v_ai_notices_v1 (tp-034 lives in AA-TripPlanner-Web; its own
replay test checks the real view). CIS 447's once-only indexes make a second relay, or a second worker, a no-op.
Everything runs in one transaction that is rolled back; invented tenants and values only."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services import tripplanner_notices as tn  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
AGENCY = "5f1a2b3c-0000-4000-8000-a11260000001"
OTHER = "5f1a2b3c-0000-4000-8000-a11260000002"
PLATFORM = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
# The stand-in: the tp-034 view's columns over a scratch table (the real view is TripPlanner's).
STANDIN = """
CREATE SCHEMA IF NOT EXISTS tripplanner;
CREATE TABLE tripplanner.zz_aa1126_outbox (
    id bigserial PRIMARY KEY, tenant_id uuid NOT NULL, event text NOT NULL, idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL, created_at timestamptz NOT NULL);
CREATE VIEW tripplanner.v_ai_notices_v1 AS
    SELECT id, tenant_id, event, idempotency_key AS notice_key, payload, created_at FROM tripplanner.zz_aa1126_outbox;
GRANT USAGE ON SCHEMA tripplanner TO cis_app;
GRANT SELECT ON tripplanner.v_ai_notices_v1 TO cis_app;
-- the live cis_app grants on its own table (the CI database starts cis_app without them), rolled back with the rest
GRANT USAGE ON SCHEMA shared TO cis_app;
GRANT SELECT, INSERT ON shared.notifications TO cis_app;
DO $$ BEGIN
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO cis_app', pg_get_serial_sequence('shared.notifications', 'id'));
END $$;
"""
EVENT = ("INSERT INTO tripplanner.zz_aa1126_outbox (tenant_id, event, idempotency_key, payload, created_at) "
         "VALUES ($1::uuid, $2, $3, $4::jsonb, $5::timestamptz)")


class _OnePool:
    """The relay's pool.acquire() over the test's one connection (its transaction is rolled back at the end)."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(STANDIN)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _bells(conn):
    rows = await conn.fetch(
        "SELECT tenant_id::text AS tenant_id, event_type, entity_type, entity_id, target_roles, payload::text AS p "
        "FROM shared.notifications WHERE event_type IN ('tripplanner.ai_allowance_used', "
        "'platform.tripplanner.spend_degraded') AND (tenant_id = ANY($1::uuid[]) OR payload->>'tenant_id' = "
        "ANY($2::text[])) ORDER BY event_type, entity_id", [AGENCY, OTHER], [AGENCY, OTHER])
    return [(r["tenant_id"], r["event_type"], r["entity_type"], r["entity_id"], list(r["target_roles"]),
             json.loads(r["p"])) for r in rows]


@pytest.mark.asyncio
async def test_the_relay_writes_each_bell_once_as_cis_app(conn):
    allowance = {"kind": "tripplanner", "month": "2026-11", "resets_at": "2026-12-01"}
    await conn.execute(EVENT, AGENCY, "ai.allowance_used", f"ai.allowance_used:{AGENCY}:2026-11",
                       json.dumps(allowance), NOW)
    await conn.execute(EVENT, OTHER, "ai.spend_degraded", "ai.spend_degraded:tenant_sessions:2026-11-18",
                       json.dumps({"check": "tenant_sessions", "stage": None, "day": "2026-11-18"}), NOW)
    await conn.execute(EVENT, AGENCY, "ai.allowance_used", f"ai.allowance_used:{AGENCY}:2026-09",      # outside window
                       json.dumps({**allowance, "month": "2026-09"}), datetime(2026, 9, 3, tzinfo=timezone.utc))
    await conn.execute("SET LOCAL ROLE cis_app")
    first = await tn.relay(_OnePool(conn), NOW)
    second = await tn.relay(_OnePool(conn), NOW)
    await conn.execute("RESET ROLE")

    assert first == {"ready": True, "allowance": 1, "degraded": 1, "skipped": 0}
    assert second == {"ready": True, "allowance": 0, "degraded": 0, "skipped": 0}   # nothing pending any more
    bells = await _bells(conn)
    assert [(b[0], b[1], b[2], b[3], b[4]) for b in bells] == [
        (PLATFORM, "platform.tripplanner.spend_degraded", "tripplanner_ai", "tenant_sessions:2026-11-18", ["admin"]),
        (AGENCY, "tripplanner.ai_allowance_used", "ai_allowance", "tripplanner:2026-11", ["tenant"])]
    assert "AI sessions quota" in bells[0][5]["message"] and bells[0][5]["tenant_id"] == OTHER
    assert bells[1][5] == allowance


@pytest.mark.asyncio
async def test_a_bell_already_written_is_not_written_again(conn):
    await conn.execute(EVENT, AGENCY, "ai.spend_degraded", "ai.spend_degraded:spend_unreadable:2026-11-18",
                       json.dumps({"check": "spend_unreadable", "stage": "tp_compose", "day": "2026-11-18"}), NOW)
    await conn.execute("SET LOCAL ROLE cis_app")
    async with conn.transaction():                      # another worker wrote it first, between the read and the write
        row = (await conn.fetch(tn.PENDING_SQL, tn.window_start(NOW), PLATFORM))[0]
        assert await tn._degraded(conn, row) is True
        assert await tn._degraded(conn, row) is False       # ON CONFLICT DO NOTHING
    assert await conn.fetch(tn.PENDING_SQL, tn.window_start(NOW), PLATFORM) == []         # nothing pending
    await conn.execute("RESET ROLE")


@pytest.mark.asyncio
async def test_no_view_or_no_grant_means_not_ready(conn):
    await conn.execute("REVOKE SELECT ON tripplanner.v_ai_notices_v1 FROM cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    async with conn.transaction():
        assert (await tn.relay(_OnePool(conn), NOW))["ready"] is False
    await conn.execute("RESET ROLE")
