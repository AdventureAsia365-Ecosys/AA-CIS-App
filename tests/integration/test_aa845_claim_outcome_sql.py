"""AA-845 — the outcome SQL against real Postgres (decision_log from migration 181, applied by conftest).

record_outcome(keep_sources=) must not overwrite a reviewer's per-sentence "unsupported", clear_outcome
must only clear that source's label, and grounding.latest_verdicts must return the newest row per
(question, subject). Rows are seeded under a unique subject prefix and removed afterwards.
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
from services.content_generation import grounding as gr  # noqa: E402
from shared.llm_client import decide as d  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
Q = "a1_claim_supported"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    prefix = f"claim:aa845{uuid.uuid4().hex[:6]}:"
    yield p, prefix
    await p.execute("DELETE FROM shared.decision_log WHERE subject_key LIKE $1", prefix + "%")
    await p.close()


async def _seed(p, subject, *, zone="grey", prob=0.3, outcome=None, age_min=0, question=Q):
    await p.execute(
        """INSERT INTO shared.decision_log (stage, question_key, subject_key, mode, zone, probability, outcome,
                                            created_at)
           VALUES ('s1_grounding', $1, $2, 'enforce', $3, $4, $5, now() - make_interval(mins => $6))""",
        question, subject, zone, prob, outcome, age_min)


async def _outcomes(p, subject):
    rows = await p.fetch("SELECT outcome FROM shared.decision_log WHERE subject_key = $1 ORDER BY id", subject)
    return [json.loads(r["outcome"]) if r["outcome"] else None for r in rows]


@pytest.mark.asyncio
async def test_approve_does_not_overwrite_a_reviewer_unsupported(pool):
    p, prefix = pool
    marked, plain = prefix + "marked", prefix + "plain"
    await _seed(p, marked)
    await _seed(p, marked, age_min=5)
    await _seed(p, plain)

    assert await gr.record_rejected_outcome(p, marked, ref="rq-1", by="u-1") == 2
    for s in (marked, plain):
        await d.record_outcome(p, Q, s, truth=True, source=gr.REVIEW_APPROVE_SOURCE,
                               keep_sources=(gr.REVIEW_UNSUPPORTED_SOURCE,))
    assert all(o["truth"] is False and o["source"] == gr.REVIEW_UNSUPPORTED_SOURCE and o["by"] == "u-1"
               for o in await _outcomes(p, marked))
    assert [o["truth"] for o in await _outcomes(p, plain)] == [True]

    # A mark after an approve wins (explicit per-sentence act).
    assert await gr.record_rejected_outcome(p, plain, ref="rq-2") == 1
    assert (await _outcomes(p, plain))[0]["truth"] is False


@pytest.mark.asyncio
async def test_clear_only_removes_the_reviewer_label(pool):
    p, prefix = pool
    marked, approved = prefix + "m", prefix + "a"
    await _seed(p, marked)
    await _seed(p, approved, outcome=json.dumps({"truth": True, "source": "review_approve"}))
    await gr.record_rejected_outcome(p, marked)

    assert await gr.clear_rejected_outcome(p, marked) == 1
    assert await gr.clear_rejected_outcome(p, approved) == 0
    assert await _outcomes(p, marked) == [None]
    assert (await _outcomes(p, approved))[0]["source"] == "review_approve"


@pytest.mark.asyncio
async def test_latest_verdicts_returns_the_newest_row_per_question(pool):
    p, prefix = pool
    s = prefix + "v"
    await _seed(p, s, zone="grey", prob=0.4, age_min=10)
    await _seed(p, s, zone="reject", prob=0.1, age_min=1)
    await _seed(p, s, prob=0.7, question="a1_promise_unsupported")
    out = await gr.latest_verdicts(p, [s, prefix + "missing"], (Q, "a1_promise_unsupported"))
    assert out[(Q, s)]["zone"] == "reject" and abs(out[(Q, s)]["probability"] - 0.1) < 1e-9
    assert abs(out[("a1_promise_unsupported", s)]["probability"] - 0.7) < 1e-9
    assert (Q, prefix + "missing") not in out


@pytest.mark.asyncio
async def test_review_and_sample_queries_run_on_the_real_schema(pool):
    """The two content reads (review claims panel, panel sample) against the migrated tables: no
    rows for a random review id / country, but the SQL must parse and type-check (lessons: mocks do
    not catch parameter types)."""
    from api.routers.admin_decisions import _CLAIM_SAMPLE_TOURS_SQL, _MASTER_TENANT_ID
    from api.routers.admin_pipeline import _GROUNDING_OUTCOME_SQL

    p, _ = pool
    assert await p.fetchrow(_GROUNDING_OUTCOME_SQL, str(uuid.uuid4())) is None
    rows = await p.fetch(_CLAIM_SAMPLE_TOURS_SQL, _MASTER_TENANT_ID, "Bhutan", "aa845", 5)
    assert len(rows) <= 5
    await p.fetch(_CLAIM_SAMPLE_TOURS_SQL, _MASTER_TENANT_ID, None, "aa845", 5)
