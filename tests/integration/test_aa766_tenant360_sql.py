"""AA-766 (S236) — the Tenant 360 cost / quality / markets SQL against real Postgres.

The CI DB is built from the migrations, but a few objects these queries read are created only by
migrations the test DB skips or cannot apply (001: gold_aa_internal.tenant_tour_versions; 203:
acp_contract.v_active_tour_atoms; content_piece.channel). Like test_aa797_tenant_admin.py, the
fixture creates minimal stand-ins with IF NOT EXISTS using the live column names from
docs/architecture/db-schema-reference.md, and drops only what it created. shared.v_tenant_monthly_usage
is not exercised here (its live shape comes from migration 223, which the CI DB cannot apply
without tenant_tour_versions) — the cost route's margin math is covered by the unit tests.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_tenant360 as t360  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    made = []
    if not await p.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL"):
        await p.execute("CREATE SCHEMA IF NOT EXISTS gold_aa_internal")
        await p.execute("CREATE TABLE gold_aa_internal.tenant_tour_versions ("
                        "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, "
                        "published_tour_id uuid NOT NULL, edit_source text NOT NULL, "
                        "created_at timestamptz NOT NULL DEFAULT now())")
        made.append("TABLE gold_aa_internal.tenant_tour_versions")
    if not await p.fetchval("SELECT to_regclass('acp_contract.v_active_tour_atoms') IS NOT NULL"):
        await p.execute("CREATE VIEW acp_contract.v_active_tour_atoms AS "
                        "SELECT atom_id, tour_id, owner_scope FROM acp_contract.tour_atoms")
        made.append("VIEW acp_contract.v_active_tour_atoms")
    await p.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text")

    tid = uuid.uuid4()
    await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'T360', $2, 3)",
                    tid, f"aa766-{tid.hex[:8]}")
    yield p, tid
    await p.execute("DELETE FROM shared.llm_call_log WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
    await p.execute("DELETE FROM gold_aa_internal.tenant_tour_versions WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM shared.unmapped_market_requests WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM shared.tenant_seo_config WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    for obj in made:
        await p.execute(f"DROP {obj}")
    await p.close()


async def _piece(p, tid, *, channel, status, ledger, held_reason=None):
    req = await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, 'a1', $2, 'approved') RETURNING request_id", tid, channel)
    return await p.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, held_reason, gate_ledger, repair_log, channel) "
        "VALUES ($1, $2, 1, 'x', $3, $4, $5::jsonb, '[]'::jsonb, $6) RETURNING piece_id",
        tid, req, status, held_reason, json.dumps(ledger), channel)


async def _call(p, tid, stage, cost, piece=None, shadow=False, last_month=False):
    await p.execute(
        "INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, "
        "content_piece_id, created_at) VALUES ($1, $2, 'writer', 'm', $3, $4::jsonb, $5, "
        "CASE WHEN $6 THEN date_trunc('month', now()) - interval '1 day' ELSE now() END)",
        tid, stage, cost, json.dumps({"shadow": True} if shadow else {}), piece, last_month)


@pytest.mark.asyncio
async def test_cost_sql(ctx):
    p, tid = ctx
    blog = await _piece(p, tid, channel="blog", status="approved", ledger=[])
    fb = await _piece(p, tid, channel="facebook", status="approved", ledger=[])
    await _call(p, tid, "t9_write", 1.0, blog)
    await _call(p, tid, "t10_judge", 0.5, blog)
    await _call(p, tid, "t9_write", 0.25, fb)
    await _call(p, tid, "t2_generate", 2.0)
    await _call(p, tid, "t9_write", 9.0, blog, shadow=True)       # excluded: shadow
    await _call(p, tid, "t2_generate", 9.0, last_month=True)      # excluded: previous month

    stages = {r["stage"]: float(r["cost_usd"]) for r in await p.fetch(t360._COST_BY_STAGE_SQL, tid)}
    assert stages == {"t2_generate": 2.0, "t9_write": 1.25, "t10_judge": 0.5}
    chans = {r["channel"]: (r["pieces"], float(r["cost_usd"])) for r in await p.fetch(t360._COST_BY_CHANNEL_SQL, tid)}
    assert chans == {"blog": (1, 1.5), "facebook": (1, 0.25)}


@pytest.mark.asyncio
async def test_quality_sql(ctx):
    p, tid = ctx
    await _piece(p, tid, channel="blog", status="approved",
                 ledger=[{"gate": "F1", "passed": True}, {"gate": "F9_cta_fact", "passed": False, "blocking": False}])
    await _piece(p, tid, channel="blog", status="held", held_reason="F9_cta_fact",
                 ledger=[{"gate": "F1", "passed": True}, {"gate": "F9_cta_fact", "passed": False}])
    await _piece(p, tid, channel="blog", status="failed", ledger=[{"gate": "F1", "passed": False}])

    status = {r["status"]: r["n"] for r in await p.fetch(t360._PIECE_STATUS_SQL, tid, 30)}
    assert status == {"approved": 1, "held": 1, "failed": 1}
    gates = {r["gate"]: dict(r) for r in await p.fetch(t360._GATES_SQL, tid, 30)}
    assert gates["F1"]["evaluated"] == 2 and gates["F1"]["passed"] == 2      # failed piece not counted
    f9 = gates["F9_cta_fact"]
    assert (f9["failed_blocking"], f9["failed_warn"], f9["held_pieces"]) == (1, 1, 1)
    held = await p.fetch(t360._HELD_REASONS_SQL, tid, 30)
    assert [(r["held_reason"], r["n"]) for r in held] == [("F9_cta_fact", 1)]

    t1, t2 = uuid.uuid4(), uuid.uuid4()
    insert = ("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, edit_source) "
              "VALUES ($1, $2, $3)")
    if not await _ttv_minimal(p):   # a real table (FKs, NOT NULLs): out of scope for this test
        pytest.skip("tenant_tour_versions is the real table here; edit-rate SQL covered by the stand-in run")
    for pt, src in ((t1, "ai_generated"), (t2, "ai_generated"), (t1, "tenant_edit")):
        await p.execute(insert, tid, pt, src)
    e = await p.fetchrow(t360._EDIT_RATE_SQL, tid, 30)
    assert dict(e) == {"ai_versions": 2, "tenant_edits": 1, "tours_generated": 2, "tours_edited": 1}


async def _ttv_minimal(p) -> bool:
    """True when tenant_tour_versions is this file's stand-in (no version_number column)."""
    return not await p.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'gold_aa_internal' "
        "AND table_name = 'tenant_tour_versions' AND column_name = 'version_number')")


@pytest.mark.asyncio
async def test_markets_sql(ctx):
    p, tid = ctx
    await p.execute("INSERT INTO shared.tenant_seo_config (tenant_id, target_market) VALUES ($1, $2::jsonb)",
                    tid, json.dumps({"countries": ["UK", "AE"]}))
    await p.execute("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) VALUES ($1, 'AE')", tid)

    tm = await p.fetchval(t360._SEO_CONFIG_SQL, tid)
    codes, unsupported, _ = t360.seo_market_codes(tm)
    assert codes == ["UK"] and unsupported == ["AE"]
    rows = await p.fetch(t360._MARKET_COVERAGE_SQL, tid, False, codes, t360.FRESH_DAYS)
    # no adopted tours -> empty scope, one row per market, nothing researched
    assert [(r["market"], r["places_in_scope"], r["researched"], r["fresh"]) for r in rows] == [("UK", 0, 0, 0)]
    unmapped = await p.fetch(t360._UNMAPPED_SQL, tid)
    assert [r["country_code"] for r in unmapped] == ["AE"] and unmapped[0]["resolved_at"] is None
