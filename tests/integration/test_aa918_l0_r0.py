"""AA-918 — R0 and the L0 lexicon fast path on real Postgres (CIS 277 + 280 replayed by conftest). Nothing here calls a
model: R0 is $0 by construction (the F9 verdicts are the stored ones).

  * R0 over a stored T9 piece and two tenant edits: the result lands on the rule (`lint.r0`, counts and ids only),
    the baseline agrees with the stored ledger, the phrase drops with no new hard stop, recurrence meets k;
  * the job handler runs R0 for a list of rules and reports a missing one;
  * activation through the route: refused (409 r0_missing / 403 no_actor) until R0 ran, then active with the admin's id;
    an edit after R0 would make it stale;
  * the writer's read: tenant A's active rule reaches tenant A's T9 social write (the pack and the writer_input record),
    never tenant B's, never a blog write when the rule is for social;
  * rollback (one click): the next write no longer gets the rule; a rolled-back rule cannot be rolled back twice.
Each test runs in one transaction that is rolled back.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.writing_memory import l0  # noqa: E402
from services.writing_memory import replay as rp  # noqa: E402
from services.writing_memory import rules as rules_mod  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402
from tests.unit import aa791_t9_fault_set as fs  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN = "5b0d2c4e-0000-4000-8000-000000000918"
WI = {"seed": fs.SEED, "route_moments": [], "facts": "", "scope": fs.SCOPE, "brand_pack": dict(fs.PACK),
      "brand_rubric": "rubric", "angle": {"name": "First night"}, "goal": "awareness", "channel": "facebook",
      "keyword": None, "cta": fs.CTA, "trip": fs.TRIP, "destination": "Mongolia"}
TEXT = fs.CLEAN.replace("a quiet first night", "a truly special first night")


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)

    async def fetchrow(self, *a):
        return await self.conn.fetchrow(*a)

    async def execute(self, *a):
        return await self.conn.execute(*a)


def _request(c):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(c))))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("DELETE FROM shared.writing_rule")
        await c.execute("DELETE FROM shared.writing_signal")
        # the live table has `channel` (migration 124 does not replay here, as in test_aa766); rolled back with the rest
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(c) -> str:
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'L0', $2, 3)",
                    tid, f"aa918-{tid.hex[:8]}")
    return str(tid)


async def _piece(c, tenant: str, text: str = TEXT, channel: str = "facebook") -> str:
    pr = rp.PieceReplay(WI, channel)
    ledger = pr.run(text, pack=pr.pack, f9=rp._stored_f9([]), stock_blocking=True)
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'a1', $2, 'approved') RETURNING request_id", tenant, channel)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, status, "
        "gate_ledger, repair_log, channel, writer_input) VALUES ($1::uuid, $2, 1, $3, 'approved', $4::jsonb, "
        "'[]'::jsonb, $5, $6::jsonb) RETURNING piece_id", tenant, req, text, json.dumps(ledger), channel,
        json.dumps(WI)))


async def _edit(c, tenant: str, piece: str):
    await wm.record_writer_feedback(
        c, writer="t9_social", scope="tenant", tenant_id=tenant, channel="facebook", field="content_text",
        before_text=TEXT, after_text=fs.CLEAN, reason_code="ai_phrasing", action="tenant_edit", actor_type="tenant",
        actor_id=f"tenant:{tenant}", source_table="content_piece", source_id=piece, fact_status="pass")


async def _candidate(c, tenant: str, phrase: str = "truly special", replacement=None) -> str:
    return await rules_mod.insert_candidate(
        c, kind="lexicon", scope="tenant", tenant_id=tenant, writer="t9_social", cluster_key="t9_social|phrase:x",
        rule_text=phrase, replacement=replacement, match_phrases=[phrase], applies_to=["facebook"],
        lint={"ok": True, "basis": "tenant"})


@pytest.mark.asyncio
async def test_r0_activate_write_rollback(conn):
    from fastapi import HTTPException

    from api.routers import admin_writing_memory as r
    from services.jobs.writing_memory_job import run_r0_job
    a, b = await _tenant(conn), await _tenant(conn)
    p1, p2 = await _piece(conn, a), await _piece(conn, a, text=fs.CLEAN)
    await _piece(conn, b)                                  # tenant B's text never feeds tenant A's rule
    await _edit(conn, a, p1)
    await _edit(conn, a, p2)
    rid = await _candidate(conn, a)
    req = _request(conn)

    # activation before R0: refused with its code; no admin id: refused
    with pytest.raises(HTTPException) as e:
        await r.activate_rule(rid, req, x_admin_user_id=ADMIN)
    assert e.value.status_code == 409 and e.value.detail["code"] == "r0_missing"
    with pytest.raises(HTTPException) as e:
        await r.activate_rule(rid, req, x_admin_user_id=None)
    assert e.value.status_code == 403 and e.value.detail["code"] == "no_actor"

    # R0 through the job handler: the result lands on the rule
    progress = []

    async def _progress(**kw):
        progress.append(kw)
    ctx = SimpleNamespace(pool=_OneConnPool(conn), payload={"rule_ids": [rid, str(uuid.uuid4())]}, job_id=None,
                          progress=_progress)
    out = await run_r0_job(ctx)
    assert [x["rule_id"] for x in out["rules"]] == [rid] and len(out["missing"]) == 1 and out["cost_usd"] == 0.0
    rule = rules_mod.serialise(await rules_mod.get_rule(conn, rid))
    r0 = rule["lint"]["r0"]
    assert r0["rule_sha"] == rp.rule_sha(rule) and rule["lint"]["basis"] == "tenant"     # the lint is kept
    assert r0["pieces"]["pieces"] == 2 and r0["phrase"] == {"before": 2, "after": 0}     # tenant A only
    assert r0["new_hard_stops"] == {} and r0["l0_ok"] and r0["r2_eligible"]
    assert r0["signals"]["explained_edits"] == 2 and r0["baseline"]["agreement"] == 1.0
    assert "truly special" not in json.dumps(r0)                                        # counts and ids only
    assert progress[-1] == {"done": 2, "total": 2}

    # activation: one click, the admin's id as approver
    active = await r.activate_rule(rid, req, x_admin_user_id=ADMIN)
    assert active["status"] == "active" and active["approved_by"] == ADMIN and active["approved_at"]

    # the writer's read: tenant A's social write gets it, tenant B's and a blog write do not
    pool = _OneConnPool(conn)
    pack, rec = await l0.l0_for_write(pool, tenant_id=uuid.UUID(a), channel="facebook", pack=dict(fs.PACK),
                                      cta=fs.CTA, trip_name=fs.TRIP)
    assert "truly special" in pack["forbidden_words"] and [e["rule_id"] for e in rec["applied"]] == [rid]
    other, rec_b = await l0.l0_for_write(pool, tenant_id=uuid.UUID(b), channel="facebook", pack=dict(fs.PACK),
                                         cta=fs.CTA, trip_name=fs.TRIP)
    assert rec_b is None and "forbidden_words" not in other
    _, rec_blog = await l0.l0_for_write(pool, tenant_id=uuid.UUID(a), channel="blog", pack=dict(fs.PACK),
                                        cta=fs.CTA, trip_name=fs.TRIP)
    assert rec_blog is None                                   # writer t9_blog: the rule is t9_social
    _, rec_li = await l0.l0_for_write(pool, tenant_id=uuid.UUID(a), channel="linkedin", pack=dict(fs.PACK),
                                      cta=fs.CTA, trip_name=fs.TRIP)
    assert rec_li["applied"] == [] and "facebook" in rec_li["skipped"][0]["reason"]      # applies_to

    # rollback: the next write no longer gets it; a second rollback is a 409
    rolled = await r.rollback_rule(rid, r.RollbackBody(reason="rollback drill"), req, x_admin_user_id=ADMIN)
    assert rolled["status"] == "rejected" and rolled["rejected_reason"] == "rollback: rollback drill"
    assert rolled["approved_by"] == ADMIN                                     # who approved it stays on the row
    pack2, rec2 = await l0.l0_for_write(pool, tenant_id=uuid.UUID(a), channel="facebook", pack=dict(fs.PACK),
                                        cta=fs.CTA, trip_name=fs.TRIP)
    assert rec2 is None and "forbidden_words" not in pack2
    with pytest.raises(HTTPException) as e:
        await r.rollback_rule(rid, r.RollbackBody(reason="again"), req, x_admin_user_id=ADMIN)
    assert e.value.status_code == 409 and e.value.detail["code"] == "not_active"


@pytest.mark.asyncio
async def test_activation_refuses_a_failed_or_stale_r0(conn):
    from fastapi import HTTPException

    from api.routers import admin_writing_memory as r
    a = await _tenant(conn)
    p1 = await _piece(conn, a)
    await _edit(conn, a, p1)
    req = _request(conn)
    # a replacement that brings in a platform-truth word: R0 finds the new hard stop, activation is refused
    rid = await _candidate(conn, a, replacement="cheap")
    rule = rules_mod.serialise(await rules_mod.get_rule(conn, rid))
    res = await rp.run_r0(_OneConnPool(conn), rule)
    assert await rules_mod.store_r0(conn, rid, res)
    assert res["new_hard_stops"] and not res["l0_ok"]
    with pytest.raises(HTTPException) as e:
        await r.activate_rule(rid, req, x_admin_user_id=ADMIN)
    assert e.value.detail["code"] == "r0_failed" and "F2_platform_truth" in e.value.detail["detail"]

    # an edit after a passing R0 makes it stale (the edit replaces the lint, R0 included)
    await conn.execute("UPDATE shared.writing_rule SET status = 'rejected', rejected_by = 'test' WHERE rule_id = $1",
                       uuid.UUID(rid))
    rid2 = await _candidate(conn, a)
    rule2 = rules_mod.serialise(await rules_mod.get_rule(conn, rid2))
    await rules_mod.store_r0(conn, rid2, await rp.run_r0(_OneConnPool(conn), rule2))
    await conn.execute("UPDATE shared.writing_rule SET rule_text = 'truly special indeed' WHERE rule_id = $1",
                       uuid.UUID(rid2))
    with pytest.raises(HTTPException) as e:
        await r.activate_rule(rid2, req, x_admin_user_id=ADMIN)
    assert e.value.detail["code"] == "r0_stale"


@pytest.mark.asyncio
async def test_r0_baseline_route_queues_the_job(conn):
    from api.routers import admin_writing_memory as r
    await conn.execute("DELETE FROM shared.job WHERE kind = 'writing_memory_r0'")
    queued = await r.run(r.RunBody(kind="r0_baseline"), _request(conn), None)
    assert queued["created"] and queued["kind"] == "writing_memory_r0"
    payload = await conn.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", queued["job_id"])
    assert json.loads(payload)["baseline"] is True
