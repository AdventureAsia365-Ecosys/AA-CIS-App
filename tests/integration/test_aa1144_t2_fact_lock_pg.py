"""AA-1144 (S285 decision D8) — PATCH /v1/tours/versions/{id} edit against real Postgres, as aa_app_user.

  - the tenant pool reads the AA Master of the tour (CIS 239 master read, replayed by conftest) for the fact base: a
    version without an itinerary is checked against the Master's days, so adding a day is a 422 fact_locked and nothing is inserted;
  - a wording edit inserts the merged content (the version's itinerary kept) with the member from the token as
    edited_by_user, and one acp_shared.audit_log row (tenant_tour_version.edited, hashes only) is written on the main
    pool after the tenant transaction; a refused edit writes tenant_tour_version.edit_refused.

Harness: tests/integration/test_aa859_t2_version_write.py (one connection, SET LOCAL ROLE aa_app_user for the tenant
pool, rolled back). Invented tour text only.
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _constants import TENANT_ID as AA_MASTER  # noqa: E402
from _ttv_live_standin import install_ttv_live_standin, seed_published_tour  # noqa: E402
from test_aa859_t2_version_write import DSN, TTV, TENANT_ROLE, _OneConnPool, _Redis  # noqa: E402
from api.routers import v1_tours  # noqa: E402

MASTER_ITINERARY = ("Day 1: Arrive in Kestrel Town.\nDay 2: Climb Brinmoor Ridge (5 hours).\n"
                    "Day 3: Descend to Orrin Lake.")
PARENT = {"name": "Lumora Valley Trek", "summary": "A three-day walk through Lumora Valley.",
          "highlights": ["Sunrise over Brinmoor Ridge"], "itineraries": "", "status": "done"}


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await install_ttv_live_standin(conn)   # the replayed 239 already grants the master read on published_tours
        pt = await seed_published_tour(conn, name="Lumora Valley Trek")
        await conn.execute("UPDATE gold_aa_internal.published_tours SET tenant_id = $2::uuid, aa_itineraries = $3 "
                           "WHERE id = $1", pt, AA_MASTER, MASTER_ITINERARY)
        tenant = uuid.uuid4()
        v1 = await conn.fetchval(
            f"INSERT INTO {TTV} (tenant_id, published_tour_id, version_number, rewritten_content, status) "
            "VALUES ($1, $2, 1, $3::jsonb, 'approved') RETURNING id", tenant, pt, json.dumps(PARENT))
        yield SimpleNamespace(conn=conn, pt=pt, tenant=tenant, v1=v1, mid=str(uuid.uuid4()))
    finally:
        await tr.rollback()
        await conn.close()


class _MainPool(_OneConnPool):
    """The main pool: write_audit_log(pool, ...) calls pool.execute (asyncpg Pool.execute acquires internally)."""

    async def execute(self, *args):
        return await self.conn.execute(*args)


def _request(conn):
    state = SimpleNamespace(pool=_MainPool(conn), tenant_pool=_OneConnPool(conn, TENANT_ROLE), redis=_Redis(True))
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _edit(ctx, content):
    body = v1_tours.VersionActionRequest(action="edit", edited_content=content, edited_by="ignored")
    return await v1_tours.update_version(str(ctx.v1), body, _request(ctx.conn),
                                         tenant={"sub": str(ctx.tenant), "mid": ctx.mid})


async def _audit(ctx, action):
    return await ctx.conn.fetch("SELECT actor, resource_id, details FROM acp_shared.audit_log "
                                "WHERE action = $1 AND tenant_id = $2", action, str(ctx.tenant))


@pytest.mark.asyncio
async def test_a_new_day_against_the_master_is_refused_and_audited(ctx):
    with pytest.raises(HTTPException) as exc:
        await _edit(ctx, {"itineraries": MASTER_ITINERARY + "\nDay 4: Rest at Orrin Lake."})
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "fact_locked"
    assert {"field": "itineraries", "fact": "days", "value": "Day 4"} in exc.value.detail["locked"]
    assert await ctx.conn.fetchval(f"SELECT count(*) FROM {TTV} WHERE tenant_id = $1", ctx.tenant) == 1
    (row,) = await _audit(ctx, "tenant_tour_version.edit_refused")
    assert row["actor"] == f"tenant:{ctx.tenant}" and row["resource_id"] == str(ctx.v1)
    details = json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
    assert details["membership_id"] == ctx.mid and "days" in {x["fact"] for x in details["locked"]}


@pytest.mark.asyncio
async def test_a_wording_edit_is_saved_merged_with_the_member_and_an_audit_row(ctx):
    out = await _edit(ctx, {"summary": "Walk the quiet Lumora Valley over three days."})
    assert out["status"] == "edited" and out["version_number"] == 2
    row = await ctx.conn.fetchrow(f"SELECT rewritten_content, edited_by_user, parent_version_id, edit_source "
                                  f"FROM {TTV} WHERE id = $1::uuid", out["new_version_id"])
    content = json.loads(row["rewritten_content"])
    assert content == {**PARENT, "summary": "Walk the quiet Lumora Valley over three days."}
    assert row["edited_by_user"] == f"tenant:{ctx.tenant}:{ctx.mid}"
    assert row["parent_version_id"] == ctx.v1 and row["edit_source"] == "tenant_edit"
    (audit,) = await _audit(ctx, "tenant_tour_version.edited")
    details = json.loads(audit["details"]) if isinstance(audit["details"], str) else audit["details"]
    assert details["new_version_id"] == out["new_version_id"]
    assert [c["field"] for c in details["changes"]] == ["summary"]
    assert "Lumora" not in json.dumps(details)
