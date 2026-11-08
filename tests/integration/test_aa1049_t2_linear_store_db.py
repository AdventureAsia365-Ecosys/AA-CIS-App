"""AA-1049 W3 (T2 train) — the SQL of the opt-in T2 linear orchestrator against real Postgres: the resume checkpoint
(``save_checkpoint`` / ``load_checkpoint`` on ``generation_meta`` of the still-pending version) and the store step
(``save_version``: main's column set).

  - a checkpoint is written on a pending version and read back (best candidate + budget), also one with the budget
    only (calls counted before the first candidate existed);
  - a checkpoint never touches a version that is no longer pending (the save already ran);
  - the store step writes the text, status, score, QA columns, generation_meta and judge_breakdown;
  - a version with no checkpoint (main's generation_meta shape, or NULL) reads as "no checkpoint".

Same harness as test_aa859_t2_version_reads_pool.py: one transaction rolled back, a live-shape stand-in of
gold_aa_internal.tenant_tour_versions (columns of db-schema-reference.md, FK-free; the CI DB skips migration 001).
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.content_generation import t2_orchestrator as o  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

TTV_DDL = """
    CREATE TABLE gold_aa_internal.tenant_tour_versions (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         uuid NOT NULL,
        published_tour_id uuid NOT NULL,
        version_number    integer NOT NULL,
        parent_version_id uuid,
        rewritten_content jsonb NOT NULL DEFAULT '{}'::jsonb,
        status            text NOT NULL DEFAULT 'pending',
        quality_score     numeric(4,2),
        edit_source       text NOT NULL DEFAULT 'ai_generated',
        rewrite_language  text NOT NULL DEFAULT 'en-US',
        seo_mode          text NOT NULL DEFAULT 'standard',
        edited_at         timestamptz,
        edited_by_user    text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        qa_status         text NOT NULL DEFAULT 'pending',
        qa_repair_count   smallint NOT NULL DEFAULT 0,
        qa_checked_at     timestamptz,
        qa_auto_passed    boolean NOT NULL DEFAULT false,
        job_id            uuid,
        generation_meta   jsonb,
        judge_breakdown   jsonb,
        UNIQUE (tenant_id, published_tour_id, version_number)
    )
"""


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await c.execute(TTV_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _version(c, status="pending", meta=None):
    return str(await c.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, status, "
        "generation_meta) VALUES ($1, $2, 1, $3, $4::jsonb) RETURNING id",
        uuid.uuid4(), uuid.uuid4(), status, json.dumps(meta) if meta is not None else None))


_BEST = {"attempt": 1, "via": "sentence_repair", "generated": {"name": "Velmora", "summary": "Three slow days."},
         "prompt_version": "abcd1234", "model_used": "stub", "fallback_used": False, "score": 9.5}


@pytest.mark.asyncio
async def test_a_checkpoint_round_trips_on_a_pending_version(conn):
    vid = await _version(conn)
    pool = _Pool(conn)
    budget = o.VersionBudget(writer=1, repair=1, usd=0.02)
    await o.save_checkpoint(pool, vid, budget, {**_BEST, "codes": [], "result": {}})
    cp = await o.load_checkpoint(pool, vid)
    assert cp["best"]["generated"] == _BEST["generated"] and cp["best"]["score"] == 9.5
    assert o.VersionBudget.from_dict(cp["budget"]).writer == 1


@pytest.mark.asyncio
async def test_a_budget_only_checkpoint_round_trips(conn):
    """The budget is persisted before the first candidate exists (each billed call counted before it is sent): a
    checkpoint with no best candidate reads back with its budget, so a retry does not pay those calls again."""
    vid = await _version(conn)
    pool = _Pool(conn)
    await o.save_checkpoint(pool, vid, o.VersionBudget(writer=2, usd=0.01), None)
    cp = await o.load_checkpoint(pool, vid)
    assert "best" not in cp and o.VersionBudget.from_dict(cp["budget"]).writer == 2


@pytest.mark.asyncio
async def test_a_checkpoint_never_touches_a_saved_version(conn):
    vid = await _version(conn, status="ai_generated", meta={"writing_release": {"id": "r0"}})
    pool = _Pool(conn)
    await o.save_checkpoint(pool, vid, o.VersionBudget(), {**_BEST, "codes": [], "result": {}})
    row = await conn.fetchval("SELECT generation_meta FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid",
                              vid)
    assert json.loads(row) == {"writing_release": {"id": "r0"}}
    assert await o.load_checkpoint(pool, vid) is None          # main's generation_meta shape: no checkpoint


@pytest.mark.asyncio
async def test_no_checkpoint_reads_as_none(conn):
    assert await o.load_checkpoint(_Pool(conn), await _version(conn)) is None


@pytest.mark.asyncio
async def test_the_store_step_writes_mains_columns(conn):
    vid = await _version(conn)
    qa = {"passed": False, "attempts": 2}
    await o.save_version(_Pool(conn), vid, rewritten={"name": "Velmora", "status": "done"}, status="needs_review",
                         quality_score=9.5, qa=qa, generation_meta={"t2_orchestrator": {"version": "t2-linear-1"}},
                         judge_breakdown={"final_contract": {"verdict": "needs_review"}}, final_contract=None)
    row = await conn.fetchrow(
        "SELECT rewritten_content, status, quality_score::float AS score, qa_status, qa_repair_count, qa_auto_passed, "
        "qa_checked_at IS NOT NULL AS checked, generation_meta, judge_breakdown "
        "FROM gold_aa_internal.tenant_tour_versions WHERE id = $1::uuid", vid)
    assert json.loads(row["rewritten_content"])["name"] == "Velmora"
    assert (row["status"], row["score"], row["qa_status"], row["qa_repair_count"], row["qa_auto_passed"],
            row["checked"]) == ("needs_review", 9.5, "escalated", 2, True, True)
    assert json.loads(row["generation_meta"])["t2_orchestrator"]["version"] == "t2-linear-1"
    assert json.loads(row["judge_breakdown"])["final_contract"]["verdict"] == "needs_review"
