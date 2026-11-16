"""S282 Sentry triage (AA-CIS-API-1F) — one object-shaped ``gate_ledger`` row must not fail a whole read.

``acp_shared.content_piece.gate_ledger`` is a JSON array by contract, but no CHECK enforces it, and a Dev fixture wrote
``'{}'::jsonb``. ``jsonb_array_elements`` on that row raised "cannot extract elements from an object", so the weekly
writing-memory KPI job failed (both attempts) and the platform-stats gate query errored too. Every unnest now goes
through ``shared.repository.jsonb_sql.json_array``. Real Postgres (conftest replays every migration); each test runs in
one transaction that is rolled back. Nothing calls a model.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X287_PREDECESSORS, X287_STANDIN_MARK  # noqa: E402

from services.writing_memory import disagreements as dis  # noqa: E402
from services.writing_memory import kpi  # noqa: E402
from shared.repository.jsonb_sql import json_array  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
FAILED = json.dumps([{"gate": "F2_lexicon", "passed": False, "blocking": True}])
SECRET = "s282-test-admin-secret"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X287_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X287_STANDIN_MARK)
        # CIS 124 (pgvector) does not replay on the CI postgres:15 image: add the two plain columns of it the
        # platform-stats read uses (inside the rolled-back transaction)
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text, "
                        "ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"S282 Lantern Trails {tid.hex[:8]}", f"zz282-{tid.hex[:8]}")
    return str(tid)


async def _piece(c, tid, ledger, *, writer_input=None, created=None):
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'zz282-atom', 'facebook', 'approved') RETURNING request_id", tid)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, writer_input, created_at) VALUES ($1::uuid, $2, 1, 'Body.', 'approved', "
        "$3::jsonb, '[]'::jsonb, $4::jsonb, coalesce($5, now())) RETURNING piece_id",
        tid, req, ledger, writer_input, created))


@pytest.mark.asyncio
async def test_json_array_unnests_an_array_and_skips_an_object(conn):
    n = await conn.fetchval(
        "SELECT count(*) AS n FROM (VALUES ('{}'::jsonb), ('[{\"gate\": \"F1\"}, {\"gate\": \"F2\"}]'::jsonb), "
        f"('\"x\"'::jsonb)) v(gate_ledger), jsonb_array_elements({json_array('v.gate_ledger')}) g")
    assert n == 2


@pytest.mark.asyncio
async def test_weekly_kpi_survives_an_object_shaped_ledger(conn):
    """The Dev fixture's shape: approved, gate_ledger '{}', no writer_input (so it counts in the t9_social pass),
    in this week and in the 28-day look-back."""
    t = await _tenant(conn)
    now = datetime.now(timezone.utc)
    await _piece(conn, t, "{}")
    await _piece(conn, t, "{}", created=now - timedelta(days=10))
    await _piece(conn, t, FAILED)
    labelled = await _piece(conn, t, "{}")
    await conn.execute("INSERT INTO acp_shared.content_piece_label (piece_id, labelled_by, label, defects) "
                       "VALUES ($1::uuid, 'pytest', 'reject', '{}'::jsonb)", labelled)
    week = kpi.week_of(now.date())
    out = await kpi.run_weekly(_OneConnPool(conn), week_start=week, apply=False)
    social = out["kpis"]["writers"]["t9_social"]["tenants"][t]
    assert social["writes"] == 3 and social["flagged"] == 1
    # the reject label on a piece whose ledger is not an array: no failed blocking gate -> a disagreement
    queued = await dis.fill_queue(conn, week, apply=False)
    assert queued["found"] >= 1 and queued["dry_run"]


@pytest.mark.asyncio
async def test_platform_stats_survives_an_object_shaped_ledger(conn, monkeypatch):
    from api.routers import admin_a4

    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    t = await _tenant(conn)
    await _piece(conn, t, "{}")
    await _piece(conn, t, FAILED)
    req = MagicMock()
    req.app.state.pool = _OneConnPool(conn)
    data = await admin_a4.get_platform_stats(req, top_n=15, x_admin_secret=SECRET)
    gates = {g["gate"]: g for g in data["data"]["top_gate_failures"]}
    assert "F2_lexicon" in gates
