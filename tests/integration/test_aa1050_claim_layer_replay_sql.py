"""AA-1050 (S294 D6) — the claim-layer replay's SQL on real Postgres (conftest replays every migration): every
statement runs against the live schema (table and column names), and `run()` completes end to end in a read-only
session with the spaCy-backed steps faked. No row is written. The CI replay of the migrations has no
`acp_shared.content_piece.channel` (the live table does; the AA-1006 / AA-1039 tests add it the same way), so the
fixture adds it before the read-only checks.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.quality import claim_layer_replay as clr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture(autouse=True)
async def live_columns():
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_every_statement_runs_on_the_migrated_schema():
    conn = await asyncpg.connect(DSN, server_settings={"default_transaction_read_only": "on"})
    try:
        await conn.fetch(clr.PIECES_SQL, 30, 5)
        await conn.fetch(clr.LEDGER_SQL, list(clr.LEDGER_STAGES), 30)
        routes = await conn.fetch(clr.ROUTES_SQL, list(clr.LEDGER_STAGES))
        settings = await conn.fetch(clr.SETTINGS_SQL, clr.SETTING_PREFIX + "%")
        has_t2 = await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok")
        if has_t2:
            await conn.fetch(clr.SHADOW_SQL, 30)
        else:   # the CI replay has no T2 table: run() treats the missing table as no shadow rows
            with pytest.raises(asyncpg.UndefinedTableError):
                await conn.fetch(clr.SHADOW_SQL, 30)
    finally:
        await conn.close()
    # the seeded rows the inventory names: the t10_claims and t10_adjudicate stages, the seed-align / adjudicator modes
    assert {"t10_claims", "t10_adjudicate"} <= {r["stage"] for r in routes}
    keys = {r["key"] for r in settings}
    assert {"cis.quality_modes.t10_seed_align", "cis.quality_modes.t10_adjudicate"} <= keys


@pytest.mark.asyncio
async def test_run_end_to_end_read_only(monkeypatch):
    monkeypatch.setenv("REPLAY_DB_SSL", "disable")
    tools = clr.PieceTools(inputs=lambda p: {}, tier1=lambda t, i: None, triggers=lambda t, i, r: [],
                           expand=lambda led: list(led))
    result = await clr.run(30, 5, dsn=DSN, tools=tools)
    assert result["schema"] == clr.SCHEMA and result["cost_usd"] == 0
    assert set(result["layers"]) == set(clr.LAYERS)
    assert result["settings"]["cis.quality_modes.t10_adjudicate"]["value"] == "off"
    assert set(result["recommendation"]) == set(clr.LAYERS)


@pytest.mark.asyncio
async def test_pieces_are_every_shipped_write_session_not_one_per_request():
    """Each content_piece row is one write session holding its final attempt (attempt_number = T9's internal retries);
    a reopened request gets a new row (migration 125). Every approved or held row is read, newest first: an older
    attempt-2 row does not hide a newer attempt-1 row, and a newer processing / failed row hides no shipped one."""
    p = await asyncpg.connect(DSN)
    tid = uuid.uuid4()
    try:
        await p.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'Lantern Trails', $2, 3)",
            tid, f"aa1050-{tid.hex[:8]}")

        async def request():
            return await p.fetchval(
                "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                "VALUES ($1, 'a1050', 'blog', 'approved') RETURNING request_id", tid)

        async def piece(req, attempt, status, minutes_ago, writer_input='{"tour": "x"}'):
            return str(await p.fetchval(
                "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
                "status, gate_ledger, repair_log, channel, writer_input, created_at) "
                "VALUES ($1, $2, $3, 'Morning light over the rope bridge', $4, '[]'::jsonb, '[]'::jsonb, 'blog', "
                "$5::jsonb, now() - make_interval(mins => $6)) RETURNING piece_id",
                tid, req, attempt, status, writer_input, minutes_ago))

        rewritten, reopened = await request(), await request()
        first = await piece(rewritten, 2, "approved", 300)       # first session took a retry
        second = await piece(rewritten, 1, "held", 120)          # the rewrite passed first time
        shipped = await piece(reopened, 1, "approved", 240)
        await piece(reopened, 2, "failed", 30)                   # newest row of the request, never shipped
        await piece(reopened, 1, "processing", 10)
        await piece(rewritten, 1, "approved", 60, writer_input=None)   # no writer input: gate inputs cannot be rebuilt

        rows = await p.fetch(clr.PIECES_SQL, 30, clr.MAX_LIMIT)
        ours = {str(r["piece_id"]) for r in await p.fetch(
            "SELECT piece_id FROM acp_shared.content_piece WHERE tenant_id = $1", tid)}
        assert [r["piece_id"] for r in rows if r["piece_id"] in ours] == [second, shipped, first]
    finally:
        await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
        await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
        await p.close()
