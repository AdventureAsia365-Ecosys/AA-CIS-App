"""S271 W0 (root ADR 0020, AA-900 l16) — the Review Queue approve gate and the T2 source read, on real Postgres.

conftest replays every migration. Each test runs in one transaction that is rolled back.

  * an unedited version with a surviving hard code: 409 approve_blocked, the review stays pending, the version hitl;
  * a tour held only by the elephant-riding policy (the code AND a ride in the stored text or the raw source, read by
    the gate query): approved (policy_keep), exported;
  * the same code with no ride anywhere (the LLM audit's other fact checks): 409 manual_check;
  * an override reason: approved, the reason and the blocker codes are on review_queue.reviewer_notes;
  * the gate query reads the LATEST quality_scores row of the version;
  * t2_rewrite_job._PT_SQL (with the raw src_* columns the elephant detector reads) prepares on the real schema.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
ELEPHANT = "FACT_CHECK_MANUAL_CHECK"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _review(conn, *, failure_codes=(), audit="pass", audit_codes=(), grounding=True, older_clean=False,
                  aa_itineraries=None, src_itineraries=None):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries) VALUES ($1, $2, $3, 'Varnel Hills 4D', 'Vietnam', '4 days', $4)",
        tour_id, MASTER, BATCH_ID, src_itineraries)
    meta = {"writer_version": "v2", **({"grounding": {"found": 0, "violations": []}} if grounding else {})}
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_itineraries, "
        "status, metadata) VALUES ($1, $2, 1, 'Varnel Hills', $4, 'hitl', $3::jsonb) RETURNING id",
        tour_id, MASTER, json.dumps(meta), aa_itineraries)
    if older_clean:   # an older, clean score row: the gate must read the newest one
        await conn.execute(
            "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
            "failure_codes, brand_audit_status, brand_audit_codes, evaluated_at) "
            "VALUES ($1, $2, $3, 9.5, '[]', 'pass', '[]', now() - interval '1 day')", gc, tour_id, MASTER)
    await conn.execute(
        "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
        "failure_codes, brand_audit_status, brand_audit_codes) VALUES ($1, $2, $3, 9.5, $4::jsonb, $5, $6::jsonb)",
        gc, tour_id, MASTER, json.dumps(list(failure_codes)), audit, json.dumps(list(audit_codes)))
    rq = await conn.fetchval(
        "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status, "
        "score_overall) VALUES ($1, $2, $3, 'pending'::review_status_enum, 9.5) RETURNING id", tour_id, gc, MASTER)
    return gc, rq


async def _state(conn, gc, rq):
    return (await conn.fetchval("SELECT review_status::text FROM silver_aa_internal.review_queue WHERE id = $1", rq),
            await conn.fetchval("SELECT status::text FROM silver_aa_internal.generated_content WHERE id = $1", gc))


@pytest.mark.asyncio
@pytest.mark.parametrize("older_clean", [False, True])
async def test_an_unedited_version_with_a_hard_code_is_409_and_changes_nothing(conn, older_clean):
    from api.routers import v1_pipeline
    gc, rq = await _review(conn, failure_codes=["FORBIDDEN_WORD"], older_clean=older_clean)
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        with pytest.raises(HTTPException) as exc:
            await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert exc.value.status_code == 409 and exc.value.detail.startswith("approve_blocked")
    assert "FORBIDDEN_WORD" in exc.value.detail
    export.assert_not_called()
    assert await _state(conn, gc, rq) == ("pending", "hitl")


@pytest.mark.asyncio
async def test_a_version_never_grounded_is_409(conn):
    from api.routers import v1_pipeline
    gc, rq = await _review(conn, grounding=False)
    with pytest.raises(HTTPException) as exc:
        await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert exc.value.status_code == 409 and "grounding did not run" in exc.value.detail
    assert await _state(conn, gc, rq) == ("pending", "hitl")


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["version", "source"])
async def test_a_policy_keep_tour_is_approved(conn, where):
    from api.routers import v1_pipeline
    ride = "Day 1 — Varnel\nDay 2 — Morrow\nAn optional elephant ride by the lake."
    gc, rq = await _review(conn, audit="manual_check", audit_codes=[ELEPHANT],
                           **({"aa_itineraries": ride} if where == "version" else {"src_itineraries": ride}))
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        out = await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert out["status"] == "approved" and out["policy_keep"] is True and out["exported"] is True
    export.assert_awaited_once_with(str(gc), contract_exit="approve")
    assert await _state(conn, gc, rq) == ("approved", "approved")


@pytest.mark.asyncio
async def test_the_shared_code_without_a_ride_is_409(conn):
    """FACT_CHECK_MANUAL_CHECK alone (e.g. the LLM audit's rare-wildlife / location check) is not the policy."""
    from api.routers import v1_pipeline
    gc, rq = await _review(conn, audit="manual_check", audit_codes=[ELEPHANT],
                           aa_itineraries="Day 1 — Varnel\nDay 2 — snow leopards by Morrow lake")
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        with pytest.raises(HTTPException) as exc:
            await v1_pipeline.approve_review(str(rq), _req(conn), None)
    assert exc.value.status_code == 409 and "manual check" in exc.value.detail and ELEPHANT in exc.value.detail
    export.assert_not_called()
    assert await _state(conn, gc, rq) == ("pending", "hitl")


@pytest.mark.asyncio
async def test_an_override_reason_is_recorded_on_the_review(conn):
    from api.routers import v1_pipeline
    gc, rq = await _review(conn, failure_codes=["FORBIDDEN_WORD"])
    body = v1_pipeline.ReviewApproveBody(override_reason="Word checked with the supplier, it is a place name")
    with patch("services.export.handler.process_export", new=AsyncMock()):
        out = await v1_pipeline.approve_review(str(rq), _req(conn), None, body)
    assert out["status"] == "approved" and out["override_blockers"] == ["hard_codes"]
    notes = await conn.fetchval("SELECT reviewer_notes FROM silver_aa_internal.review_queue WHERE id = $1", rq)
    assert "it is a place name" in notes and "hard_codes" in notes


@pytest.mark.asyncio
async def test_the_t2_source_query_prepares_on_the_real_schema(conn):
    from services.jobs import t2_rewrite_job as job
    stmt = await conn.prepare(job._PT_SQL)
    cols = [a.name for a in stmt.get_attributes()]
    for col in ("src_itineraries", "src_highlights", "inclusions", "activities", "price_raw", "aa_itineraries"):
        assert col in cols
