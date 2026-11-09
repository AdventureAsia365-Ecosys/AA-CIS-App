"""AA-1050 (Quality W4, root ADR 0020 D7) — the claim shadow's store path on real Postgres (CIS 391, conftest replays
every migration), as cis_app: claim_shadow.shadow_object writes a row, then serves the next object of the same key
from the cache with no call; the ledger read runs on the real table; the admin summary reads the rows back.

Invented values only; every row has tenant TENANT and is rolled back.
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

from services.quality import claim_shadow as cs  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "00000000-0000-4000-8000-0000000a1072"
BODY = "Arrive in Velmora and settle in at the Harbour Lodge. A private chef cooks a lobster dinner on the pier."
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included."


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    yield c
    await c.close()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _reply(*quotes):
    return SimpleNamespace(
        content=json.dumps({"claims": [{"quote": q, "class": "fact.inclusion", "why": "invented"} for q in quotes]}),
        model_used="gpt-6-luna-openai", connection_key="openai-direct", cost_usd=0.001, attempt_no=1,
        stop_reason="stop", route_position=0, provider="openai", input_tokens=600, output_tokens=120,
        satellite_account=None, fallback_used=False)


@pytest.mark.asyncio
async def test_shadow_object_writes_then_serves_the_cache_as_cis_app(conn, monkeypatch):
    """The store path as cis_app (grants given inside the rolled-back transaction: the CI replay skips CIS 244): the
    first object makes its calls and writes a row; a second object with the same text and source (another piece) is
    served from the cache: a cached row, 0 calls, cost 0. The ledger read runs on the real table."""
    calls = []

    def caller(inst, system, user, schema):
        calls.append(user)
        return _reply("A private chef cooks a lobster dinner on the pier.")

    async def no_log(*_a, **_k):
        return None

    monkeypatch.setattr(cs, "_log", no_log)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
        await conn.execute("GRANT SELECT, INSERT, DELETE ON shared.claim_shadow_verdict TO cis_app")
        await conn.execute("GRANT SELECT ON shared.llm_call_log TO cis_app")
        await conn.execute("SET LOCAL ROLE cis_app")

        def obj(piece):
            return cs.ShadowObject(surface="t9_blog", fields=(("content_text", BODY),), source_text=SOURCE,
                                   baseline="pass", baseline_status="approved", tenant_id=TENANT, piece_id=piece)

        first = await cs.shadow_object(_Pool(conn), obj(str(uuid.uuid4())), run_kind="replay", run_id="zz-aa1050-a",
                                       caller=caller, attempts=1)
        assert first["verdict"] == "unsupported" and first["cached"] is False and len(calls) == cs.VOTES
        again = await cs.shadow_object(_Pool(conn), obj(str(uuid.uuid4())), run_kind="replay", run_id="zz-aa1050-b",
                                       caller=caller, attempts=1)
        assert again["cached"] is True and again["calls"] == 0 and len(calls) == cs.VOTES
        rows = await conn.fetch("SELECT run_id, cached, calls, cost_usd::float8 AS cost, verdict, agreement "
                                "FROM shared.claim_shadow_verdict WHERE tenant_id = $1::uuid ORDER BY run_id", TENANT)
        assert [(r["run_id"], r["cached"], r["calls"]) for r in rows] == [
            ("zz-aa1050-a", False, cs.VOTES), ("zz-aa1050-b", True, 0)]
        assert rows[1]["cost"] == 0 and {r["agreement"] for r in rows} == {"disagree"}
        summary = await cs.summary(conn, days=1, run_kind="replay", sample=5)
        assert summary["by_surface"]["t9_blog"]["disagree"] >= 2 and summary["cached_share"] is not None
    finally:
        await tr.rollback()
