"""AA-211 + AA-212 — export gate hardening + HITL review_queue re-wire.

Tests drive the REAL decision logic, not re-implemented copies (S67 lesson):
  - the gate calls api.routers.admin_pipeline._is_publishable (same fn the pipeline uses);
  - the enqueue calls api.routers.admin_pipeline._enqueue_review (same INSERT the pipeline runs);
  - approve/reject call the real api.routers.v1_pipeline endpoint coroutines.

No live DB / no AWS: asyncpg is mocked (matching tests/unit/test_s1_router.py), and the enqueue
idempotency is asserted against a tiny in-memory conn that emulates the NOT EXISTS guard.
"""
import json  # noqa: F401  (kept symmetric with router imports)

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from api.routers.admin_pipeline import _is_publishable, _enqueue_review


MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
TENANT = {"sub": MASTER_TENANT, "role": "admin"}
TOUR_ID = "22222222-2222-2222-2222-222222222222"
GCID = "11111111-1111-1111-1111-111111111111"
REVIEW_ID = "33333333-3333-3333-3333-333333333333"


# ── GATE: api.routers.admin_pipeline._is_publishable ──────────────────────────

@pytest.mark.parametrize("result, expected", [
    # clean pass, score >= 7 → publish
    ({"quality_score": 8.0, "brand_audit_status": "pass"}, True),
    # flagged but fix pass repaired it (Terra) → publish (must NOT block fixed)
    ({"quality_score": 8.0, "brand_audit_status": "flagged", "fix_pass_applied": True}, True),
    # flagged, fix pass NOT applied → block (enqueue)
    ({"quality_score": 8.0, "brand_audit_status": "flagged", "fix_pass_applied": False}, False),
    # flagged, fix_pass_applied absent → block (enqueue)
    ({"quality_score": 8.0, "brand_audit_status": "flagged"}, False),
    # manual_check despite score >= 7 → block (enqueue)
    ({"quality_score": 8.0, "brand_audit_status": "manual_check"}, False),
    # score < 7 (any audit) → block (enqueue)
    ({"quality_score": 5.0, "brand_audit_status": "pass"}, False),
    ({"quality_score": 6.99, "brand_audit_status": "flagged", "fix_pass_applied": True}, False),
    # no brand profile at all, score >= 7 → publish (legacy / no-profile still exports)
    ({"quality_score": 7.0}, True),
])
def test_publishable_gate(result, expected):
    assert _is_publishable(result) is expected


# ── ENQUEUE: api.routers.admin_pipeline._enqueue_review ───────────────────────

class FakeReviewQueueConn:
    """asyncpg-like conn that emulates the review_queue INSERT ... SELECT ... WHERE NOT EXISTS
    guard against an in-memory list — lets us assert idempotency (one pending row per
    generated_content_id) behaviourally without a live Postgres.

    Positional args mirror _enqueue_review's call:
        (tour_id, generated_content_id, tenant_id, failure_summary, score_overall)
    """

    def __init__(self):
        self.rows = []

    async def execute(self, sql, *args):
        if "INSERT INTO silver_aa_internal.review_queue" in sql:
            tour_id, gcid, tenant_id, failure_summary, score_overall = args
            already_pending = any(
                r["generated_content_id"] == gcid and r["review_status"] == "pending"
                for r in self.rows
            )
            if not already_pending:
                # SF columns are intentionally absent from the INSERT → NULL on the real row.
                self.rows.append({
                    "tour_id": tour_id,
                    "generated_content_id": gcid,
                    "tenant_id": tenant_id,
                    "failure_summary": failure_summary,
                    "score_overall": score_overall,
                    "review_status": "pending",
                    "step_fn_task_token": None,
                    "step_fn_execution_arn": None,
                })
                return "INSERT 0 1"
            return "INSERT 0 0"
        return "OK"


@pytest.mark.asyncio
async def test_enqueue_inserts_one_row_with_correct_columns():
    """Blocked tour → exactly one review_queue row, columns correct, SF cols NULL."""
    conn = FakeReviewQueueConn()
    result = {
        "quality_score": 5.2,
        "brand_audit_status": "flagged",
        "failure_codes": ["META_TOO_SHORT"],
        "brand_audit_codes": ["BRAND_TONE"],
    }
    await _enqueue_review(conn, TOUR_ID, GCID, result)

    assert len(conn.rows) == 1  # row actually present (catches a silent ::uuid cast fail)
    row = conn.rows[0]
    assert row["tour_id"] == TOUR_ID
    assert row["generated_content_id"] == GCID
    assert row["tenant_id"] == MASTER_TENANT
    assert row["review_status"] == "pending"
    assert row["step_fn_task_token"] is None
    assert row["step_fn_execution_arn"] is None
    assert row["score_overall"] == pytest.approx(5.2)
    # failure_summary surfaces the available signals
    assert "brand_audit=flagged" in row["failure_summary"]
    assert "META_TOO_SHORT" in row["failure_summary"]
    assert "BRAND_TONE" in row["failure_summary"]
    assert "low_quality" in row["failure_summary"]


@pytest.mark.asyncio
async def test_enqueue_rerun_does_not_double_insert():
    """Re-run on the same generated_content_id while a pending row exists → still one row."""
    conn = FakeReviewQueueConn()
    result = {"quality_score": 4.0, "brand_audit_status": "manual_check"}
    await _enqueue_review(conn, TOUR_ID, GCID, result)
    await _enqueue_review(conn, TOUR_ID, GCID, result)
    assert len(conn.rows) == 1


# ── APPROVE / REJECT atomic-claim: real v1_pipeline endpoints ─────────────────

# S271 W0: the approve gate reads the stored version before the claim (v1_pipeline._APPROVE_GATE_SQL).
_GATE_CLEAN = {"generated_content_id": GCID, "human_edited": False, "fix_pass_applied": False,
               "metadata": {"grounding": {"found": 0, "violations": []}}, "score_overall": 8.0,
               "failure_codes": [], "brand_audit_status": "pass", "brand_audit_codes": []}


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    req.headers = {}
    return req


@pytest.mark.asyncio
async def test_approve_null_token_exports_once_then_409_on_second_call():
    """NULL token → direct export; second approve claims nothing → 409, export still once.

    This is the EventBridge-double-fire regression test: process_export (NOT idempotent against
    its re-fire) must run at most once across a double-click.
    """
    from api.routers import v1_pipeline

    conn = AsyncMock()
    # 1st approve: claim succeeds (pending row, NULL token). 2nd approve: claim returns nothing.
    conn.fetchrow = AsyncMock(side_effect=[
        _GATE_CLEAN,
        {"generated_content_id": GCID, "step_fn_task_token": None},
        None,    # 2nd approve, S271 W0 gate: the review is no longer pending
        None,    # 2nd approve: claim returns nothing (row already approved)
        None,    # AA-234: gate-disambiguation query → not gate-blocked → "already processed"
    ])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    req = _make_request(_make_pool(conn))

    with patch("services.export.handler.process_export", new=AsyncMock()) as mock_export:
        first = await v1_pipeline.approve_review(REVIEW_ID, req, TENANT)
        assert first["status"] == "approved"
        assert first["exported"] is True
        assert first["sf_notified"] is False
        mock_export.assert_awaited_once_with(str(GCID), contract_exit="approve")

        with pytest.raises(HTTPException) as exc:
            await v1_pipeline.approve_review(REVIEW_ID, req, TENANT)
        assert exc.value.status_code == 409
        # double-fire guard holds: export NOT called again
        assert mock_export.call_count == 1


@pytest.mark.asyncio
async def test_approve_with_token_uses_sfn_not_export():
    """Token set → send_task_success path; process_export NOT called; sf_notified True."""
    from api.routers import v1_pipeline

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_GATE_CLEAN, {
        "generated_content_id": GCID, "step_fn_task_token": "tok-123",
    }])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    req = _make_request(_make_pool(conn))

    sfn = MagicMock()
    with patch("services.export.handler.process_export", new=AsyncMock()) as mock_export, \
         patch.object(v1_pipeline, "_boto3") as mock_boto3:
        mock_boto3.client.return_value = sfn
        res = await v1_pipeline.approve_review(REVIEW_ID, req, TENANT)

    assert res["sf_notified"] is True
    assert res["exported"] is False
    mock_export.assert_not_called()
    sfn.send_task_success.assert_called_once()


@pytest.mark.asyncio
async def test_reject_claims_then_409_and_never_exports():
    """pending → rejected; second reject → 409; process_export never called."""
    from api.routers import v1_pipeline

    conn = AsyncMock()
    # AA-441: reject_review now also RETURNING generated_content_id (to reset
    # generated_content.status to 'rejected' — bug #5) and issues a second conn.execute() for
    # that UPDATE, alongside the pre-existing one the claim itself does.
    # AA-476: reject_review now also looks up tour_id (2nd fetchrow) to call
    # mark_tour_rejected() — see services/export/handler.py.
    conn.fetchrow = AsyncMock(side_effect=[
        {"step_fn_task_token": None, "generated_content_id": GCID},
        {"tour_id": TOUR_ID},
        None,
    ])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    req = _make_request(_make_pool(conn))

    with patch("services.export.handler.process_export", new=AsyncMock()) as mock_export, \
         patch("services.export.handler.mark_tour_rejected", new=AsyncMock()) as mock_reject:
        first = await v1_pipeline.reject_review(REVIEW_ID, req, TENANT)
        assert first["status"] == "rejected"
        assert first["sf_notified"] is False
        mock_reject.assert_awaited_once_with(conn, TOUR_ID)

        with pytest.raises(HTTPException) as exc:
            await v1_pipeline.reject_review(REVIEW_ID, req, TENANT)
        assert exc.value.status_code == 409

    mock_export.assert_not_called()


@pytest.mark.asyncio
async def test_reject_with_token_sends_task_failure():
    """Token set → send_task_failure called; sf_notified True; no export."""
    from api.routers import v1_pipeline

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"step_fn_task_token": "tok-9", "generated_content_id": GCID},
        {"tour_id": TOUR_ID},
    ])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    req = _make_request(_make_pool(conn))

    sfn = MagicMock()
    with patch.object(v1_pipeline, "_boto3") as mock_boto3, \
         patch("services.export.handler.mark_tour_rejected", new=AsyncMock()) as mock_reject:
        mock_boto3.client.return_value = sfn
        res = await v1_pipeline.reject_review(REVIEW_ID, req, TENANT)

    assert res["sf_notified"] is True
    sfn.send_task_failure.assert_called_once()
    mock_reject.assert_awaited_once_with(conn, TOUR_ID)
