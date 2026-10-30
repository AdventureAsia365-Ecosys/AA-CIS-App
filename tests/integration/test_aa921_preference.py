"""AA-921 WM-28 — the preference-summary run on real Postgres (CIS 277 + 280 replayed by conftest). The drafter (the
only paid call) is a stub; nothing here calls a model.

  * Done-when: a summary containing an unsourced promise is quarantined — stored `rejected` by the lint, with
    `quarantine: true` and the failed `unsourced_claim` check, never a candidate;
  * a tone-only summary is stored as a tenant `preference_summary` candidate with its provenance, and a second run
    skips the tenant and writer (open summary);
  * switched off (the default) the run makes no call and stores nothing.
Each test runs in one transaction that is rolled back.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.writing_memory import preference as pref  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


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


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("DELETE FROM shared.writing_rule")
        await c.execute("DELETE FROM shared.writing_signal")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _signals(c, tenant, n=5):
    out = []
    for i in range(n):
        ref = await wm.record_writer_feedback(
            c, writer="t9_blog", scope="tenant", tenant_id=tenant, channel="blog", field="content_text",
            before_text=f"Day {i} is a truly breathtaking adventure!", after_text=f"Day {i} is a quiet walk.",
            action="tenant_edit", actor_type="tenant", actor_id=f"tenant:{tenant}", source_table="content_piece",
            source_id=f"p{i % 3}", fact_status="pass")
        out.append(ref.signal_id)
    return out


def _stub(summary):
    async def draft(pool, **kw):
        return {"summary": summary, "match_phrases": ["truly breathtaking"]}, "stub-judge", 0.001
    return draft


@pytest.mark.asyncio
async def test_a_summary_with_an_unsourced_promise_is_quarantined(conn, monkeypatch):
    monkeypatch.setenv(pref.PREF_ENV, "true")
    t = str(uuid.uuid4())
    await _signals(conn, t)
    with patch.object(pref, "draft_summary", AsyncMock(side_effect=_stub(
            "Keep sentences calm and short. Always mention the free hotel pickup."))):
        out = await pref.run_preference(_OneConnPool(conn), None)
    assert out["calls"] == 1 and out["quarantined"] == 1 and out["candidates"] == 0
    row = await conn.fetchrow("SELECT kind, scope, tenant_id::text AS tenant_id, status, rejected_by, lint "
                              "FROM shared.writing_rule WHERE tenant_id = $1::uuid", t)
    lint = json.loads(row["lint"])
    assert (row["kind"], row["scope"], row["tenant_id"], row["status"], row["rejected_by"]) == (
        "preference_summary", "tenant", t, "rejected", "lint")
    assert lint["quarantine"] is True and lint["unsourced_claim"]["ok"] is False


@pytest.mark.asyncio
async def test_a_tone_summary_is_a_candidate_and_is_not_redrafted(conn, monkeypatch):
    monkeypatch.setenv(pref.PREF_ENV, "true")
    t = str(uuid.uuid4())
    sids = await _signals(conn, t, n=6)
    stub = AsyncMock(side_effect=_stub("Keep sentences short and calm. Avoid exclamation marks."))
    with patch.object(pref, "draft_summary", stub):
        out = await pref.run_preference(_OneConnPool(conn), None)
        again = await pref.run_preference(_OneConnPool(conn), None)
    assert out["candidates"] == 1 and again["skipped_open"] == 1 and stub.await_count == 1
    row = await conn.fetchrow("SELECT status, cluster_key, signal_ids, support_signals FROM shared.writing_rule "
                              "WHERE tenant_id = $1::uuid", t)
    assert row["status"] == "candidate" and row["cluster_key"] == "t9_blog|pref:voice"
    assert sorted(str(s) for s in row["signal_ids"]) == sorted(sids) and row["support_signals"] == 6


@pytest.mark.asyncio
async def test_off_by_default_no_call_nothing_stored(conn, monkeypatch):
    monkeypatch.delenv(pref.PREF_ENV, raising=False)
    t = str(uuid.uuid4())
    await _signals(conn, t)
    stub = AsyncMock()
    with patch.object(pref, "draft_summary", stub):
        out = await pref.run_preference(_OneConnPool(conn), None)
    assert out["state"] == "needs_llm_path" and stub.await_count == 0
    assert await conn.fetchval("SELECT count(*) FROM shared.writing_rule") == 0
