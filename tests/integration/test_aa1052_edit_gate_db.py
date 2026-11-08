"""AA-1052 W6 — the runtime edit gate on real Postgres (services/quality/edit_gate.py).

  * the open-wave read is the deploy gate's check (shared.rerun_wave.status = 'open'): an open wave refuses a gated
    edit (409 at the router), a closed / complete one does not;
  * the route policy router refuses a hold switch change of a gated stage without evidence (422), refuses it during
    an open wave (409), and a break-glass save lands in the audit row (details.evidence) with the admin bell, in the
    same transaction as the policy row; an alert-threshold-only edit passes without evidence.

Invented stage names (t2_zz1052_*) and wave names only; every row this test writes is removed at the end.
"""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402  (audit_log is append-only, CIS 248)
from api.routers import admin_llm_route as route_api  # noqa: E402
from services.quality import edit_gate  # noqa: E402
from shared.llm_client import route_watch as rw  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "t2_zz1052_writer"
PLAIN = "zz1052_helper"
REASON = "incident drill: the fallback hold must change now"
MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
(POLICY_MIGRATION,) = MIGRATIONS.glob("[0-9][0-9][0-9]_llm_route_policy.sql")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with p.acquire() as c:
        await c.execute(POLICY_MIGRATION.read_text())          # re-runnable
        await _clean(c)
    rw.reset()
    yield p
    async with p.acquire() as c:
        await _clean(c)
        await delete_audit_rows(c, "resource_type = 'llm_route_policy' AND resource_id = ANY($1::text[])",
                                [STAGE, PLAIN])
    rw.reset()
    await p.close()


async def _clean(c):
    await c.execute("DELETE FROM shared.llm_route_policy WHERE stage = ANY($1::text[])", [STAGE, PLAIN])
    await c.execute("DELETE FROM shared.rerun_wave WHERE name LIKE 'zz1052 %'")
    await c.execute("DELETE FROM shared.notifications WHERE event_type = 'platform.llm_route.break_glass' "
                    "AND entity_id = ANY($1::text[])", [STAGE, PLAIN])


async def _wave(c, status):
    return await c.fetchval(
        "INSERT INTO shared.rerun_wave (name, expected_tour_ids, status) VALUES ($1, ARRAY[$2::uuid], $3) "
        "RETURNING id::text", f"zz1052 {status} {uuid.uuid4().hex[:6]}", uuid.uuid4(), status)


def _request(pool):
    return MagicMock(app=MagicMock(state=MagicMock(pool=pool)))


async def _ours(pool):
    """The open waves this test made (other tests' waves in a shared DB are not ours to judge)."""
    return [w for w in await edit_gate.read_open_waves(pool) if (w["name"] or "").startswith("zz1052 ")]


@pytest.mark.asyncio
async def test_only_an_open_wave_freezes_a_gated_edit(pool):
    async with pool.acquire() as c:
        await _wave(c, "closed")
        await _wave(c, "complete")
    assert await _ours(pool) == []
    async with pool.acquire() as c:
        wid = await _wave(c, "open")
    assert [w["id"] for w in await _ours(pool)] == [wid]
    with pytest.raises(edit_gate.EditFrozen) as exc:
        await edit_gate.refuse_if_frozen("the route of stage t2_x", ["t2_x"], pool=pool)
    assert wid in str(exc.value)
    await edit_gate.refuse_if_frozen("nothing gated", [], pool=pool)            # no gated stage: no read, no refusal


@pytest.mark.asyncio
async def test_the_route_policy_gate_end_to_end(pool):
    req = _request(pool)
    # an alert threshold only: no evidence, no bell
    await route_api.put_route_policy(route_api.RoutePolicyBody(fallback_alert_pct=35), req, stage=STAGE,
                                     x_admin_user_id="zz1052")
    # the hold switch without evidence: 422, the row keeps its hold
    with pytest.raises(HTTPException) as exc:
        await route_api.put_route_policy(route_api.RoutePolicyBody(fallback_alert_pct=35, hold_fallback_writes=True),
                                         req, stage=STAGE, x_admin_user_id="zz1052")
    assert exc.value.status_code == 422
    # break glass: saved, the evidence in the audit row, the bell in the same transaction
    await route_api.put_route_policy(route_api.RoutePolicyBody(fallback_alert_pct=35, hold_fallback_writes=True,
                                                               break_glass_reason=REASON),
                                     req, stage=STAGE, x_admin_user_id="zz1052")
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT hold_fallback_writes FROM shared.llm_route_policy WHERE stage = $1",
                                STAGE) is True
        audits = await c.fetch(
            "SELECT details -> 'evidence' ->> 'break_glass_reason' AS reason FROM acp_shared.audit_log "
            "WHERE resource_type = 'llm_route_policy' AND resource_id = $1 ORDER BY created_at, id", STAGE)
        bells = await c.fetchval(
            "SELECT count(*) AS n FROM shared.notifications WHERE event_type = 'platform.llm_route.break_glass' "
            "AND entity_id = $1", STAGE)
    assert [a["reason"] for a in audits] == [None, REASON] and bells == 1
    # a plain stage's hold switch is free
    await route_api.put_route_policy(route_api.RoutePolicyBody(hold_fallback_writes=True), req, stage=PLAIN,
                                     x_admin_user_id="zz1052")
    # an open wave refuses the reset of the gated hold even with a reason
    async with pool.acquire() as c:
        await _wave(c, "open")
    with pytest.raises(HTTPException) as exc:
        await route_api.delete_route_policy(req, stage=STAGE, x_admin_user_id="zz1052", eval_run_id=None,
                                            break_glass_reason=REASON)
    assert exc.value.status_code == 409
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) AS n FROM shared.llm_route_policy WHERE stage = $1", STAGE) == 1
