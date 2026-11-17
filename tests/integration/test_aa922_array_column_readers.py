"""AA-922 — every reader of a content_piece JSON-array column survives a row whose value is a JSON object.

The weekly writing-memory KPI job for week 2026-11-09 failed twice with "cannot extract elements from an object": a Dev
fixture wrote ``gate_ledger = '{}'`` (S282 fixed the fixture and the gate_ledger unnests). ``flags`` has the same array
contract and is nullable, and three reads still unnested it (or a concatenation with it) raw, or relied on ``AND`` to
check ``jsonb_typeof`` first (Postgres does not promise an order for ``AND``; ``CASE`` does). Each test mixes one
object-shaped and one array-shaped row. Real Postgres (conftest replays every migration); each test runs in one
transaction that is rolled back. Nothing calls a model; every name below is invented.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X287_PREDECESSORS, X287_STANDIN_MARK  # noqa: E402

from services.writing_memory import backfill  # noqa: E402
from services.writing_memory import kpi  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "aa922-test-admin-secret"
GATE = "F2_lexicon"
FAILED = json.dumps([{"gate": GATE, "passed": False, "blocking": True}])
WARN = json.dumps([{"gate": "F15_voice", "passed": False, "blocking": False}])
REPAIRED = json.dumps([{"attempt": 1, "gate": GATE, "action": "rewrite"}])
OBJ = "{}"
OBJ_FULL = json.dumps({"gate": GATE, "passed": False})


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
        # CIS 124 (pgvector) does not replay on the CI postgres:15 image: add the plain columns of it these reads
        # use (inside the rolled-back transaction)
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text, "
                        "ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"AA922 Copper Ridge {tid.hex[:8]}", f"zz922-{tid.hex[:8]}")
    return str(tid)


async def _piece(c, tid, *, ledger="[]", flags=None, repair_log="[]", status="approved"):
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'zz922-atom', 'facebook', 'approved') RETURNING request_id", tid)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, flags, repair_log, channel) VALUES ($1::uuid, $2, 1, 'Body.', $3, $4::jsonb, "
        "$5::jsonb, $6::jsonb, 'facebook') RETURNING piece_id",
        tid, req, status, ledger, flags, repair_log))


def _admin(monkeypatch, conn):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    req = MagicMock()
    req.app.state.pool = _OneConnPool(conn)
    return req


@pytest.mark.asyncio
async def test_weekly_kpi_counts_the_array_row_and_skips_the_object_row(conn):
    """The failed job's own read: one object-shaped and one array-shaped ledger in the same week."""
    t = await _tenant(conn)
    await _piece(conn, t, ledger=OBJ_FULL, flags=OBJ)
    await _piece(conn, t, ledger=FAILED, flags=WARN)
    out = await kpi.run_weekly(_OneConnPool(conn), week_start=kpi.week_of(datetime.now(timezone.utc).date()),
                               apply=False)
    social = out["kpis"]["writers"]["t9_social"]["tenants"][t]
    assert social["writes"] == 2 and social["flagged"] == 1


@pytest.mark.asyncio
async def test_content_quality_pieces_list_and_gate_filter_survive_object_rows(conn, monkeypatch):
    """GET /admin/content-quality/pieces: failed_gates / flag_gates per row, and the `gate` filter that unnests
    ledger || flags (an object || an object is an object: it raised before)."""
    from api.routers import admin_content_quality as acq

    req = _admin(monkeypatch, conn)
    monkeypatch.setattr(acq, "tenant_scope_filter", AsyncMock(return_value=None))
    monkeypatch.setattr(acq.seed_audit, "load_piece_seeds", AsyncMock(return_value={}))
    t = await _tenant(conn)
    both_obj = await _piece(conn, t, ledger=OBJ_FULL, flags=OBJ_FULL)
    flags_obj = await _piece(conn, t, ledger=FAILED, flags=OBJ)
    arrays = await _piece(conn, t, ledger=FAILED, flags=WARN)

    out = await acq.list_pieces(req, limit=50, offset=0, tenant_id=t, channel=None, status=None, gate=None, q=None,
                                label=None, sort="created_at", sort_dir="desc", x_admin_secret=SECRET)
    rows = {str(r["piece_id"]): r for r in out["data"]}
    assert out["total"] == 3
    assert list(rows[both_obj]["failed_gates"]) == [] and list(rows[both_obj]["flag_gates"]) == []
    assert list(rows[flags_obj]["failed_gates"]) == [GATE] and list(rows[flags_obj]["flag_gates"]) == []
    assert list(rows[arrays]["flag_gates"]) == ["F15_voice"]

    hit = await acq.list_pieces(req, limit=50, offset=0, tenant_id=t, channel=None, status=None, gate=GATE, q=None,
                                label=None, sort="created_at", sort_dir="desc", x_admin_secret=SECRET)
    assert {str(r["piece_id"]) for r in hit["data"]} == {flags_obj, arrays}


@pytest.mark.asyncio
async def test_gate_telemetry_warn_count_reads_only_array_flags(conn, monkeypatch):
    from api.routers import admin_a4

    req = _admin(monkeypatch, conn)
    t = await _tenant(conn)
    await _piece(conn, t, ledger=OBJ, flags=OBJ_FULL)
    await _piece(conn, t, ledger=FAILED, flags=WARN)
    await _piece(conn, t, ledger=FAILED, flags=None)
    data = await admin_a4.get_gate_telemetry(req, date_from=None, date_to=None, top_n=20, x_admin_secret=SECRET)
    body = data.get("data", data)
    rows = [r for r in body["by_tenant_channel"] if r["tenant_id"] == t]
    assert len(rows) == 1 and rows[0]["total"] == 3 and rows[0]["warn_count"] == 1


@pytest.mark.asyncio
async def test_backfill_reads_array_repair_logs_only(conn):
    t = await _tenant(conn)
    skipped = await _piece(conn, t, repair_log=OBJ_FULL)
    kept = await _piece(conn, t, repair_log=REPAIRED)
    rows = await conn.fetch(backfill._T9_SQL)
    ids = {r["piece_id"] for r in rows}
    assert kept in ids and skipped not in ids
