"""AA-450 (endpoint shape) + AA-466 (202 Accepted + poll) — api/routers/v1_content_writing.py.
Same convention test_aa449_v1_angle_gate.py uses: endpoint functions called directly,
service.py patched (already unit-tested separately in test_aa450_content_writing_service.py) —
this file checks HTTP status-code mapping + that the background task is actually launched with
a strong ref (AA-466 — the GC-safety pattern api/routers/v1_tours.py's trigger_rewrite() uses)."""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import v1_content_writing
from services.acp_angle_gate.service import RequestNotFoundError
from services.acp_content_writing import service

TENANT_ID = str(uuid.uuid4())
REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()


def _make_request():
    request = MagicMock()
    request.app.state.pool = MagicMock()
    return request


def _started(status="processing"):
    return {
        "piece": {"piece_id": str(PIECE_ID), "status": status, "content_text": "",
                   "angle_gate_request_id": str(REQUEST_ID)},
        "context": {"atom_text": "x"},
    }


class TestWrite:
    @pytest.mark.asyncio
    async def test_success_returns_202_processing_placeholder(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(v1_content_writing.service, "start_write",
                           new=AsyncMock(return_value=_started())), \
             patch.object(v1_content_writing.service, "run_write_background",
                           new=AsyncMock(return_value=None)) as mock_bg, \
             patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
            result = await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
            await asyncio.sleep(0)  # let the scheduled background task actually run

        # AA-613 — tenant-safe 202 body: ready_state instead of raw status, no gate/held fields.
        assert result["ready_state"] == "in_progress"
        assert result["piece_id"] == str(PIECE_ID)
        mock_bg.assert_called_once()

    @pytest.mark.asyncio
    async def test_background_task_launched_with_strong_ref(self):
        """AA-466 — the task must be added to the module-level _background_tasks set (and
        removed again on completion via add_done_callback), the same GC-safety guard
        api/routers/v1_tours.py::trigger_rewrite() already uses. A bare create_task() with no
        ref can be garbage-collected mid-flight."""
        body = v1_content_writing.WriteBody(cta=None)
        assert len(v1_content_writing._background_tasks) == 0
        with patch.object(v1_content_writing.service, "start_write",
                           new=AsyncMock(return_value=_started())), \
             patch.object(v1_content_writing.service, "run_write_background",
                           new=AsyncMock(return_value=None)), \
             patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
            await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
            assert len(v1_content_writing._background_tasks) == 1  # added before the task ran
            # task completion -> add_done_callback fires via call_soon, needs 2 ticks to observe
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert len(v1_content_writing._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_request_not_found_404(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(
            v1_content_writing.service, "start_write",
            new=AsyncMock(side_effect=RequestNotFoundError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_not_ready_409(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(
            v1_content_writing.service, "start_write",
            new=AsyncMock(side_effect=service.RequestNotReadyError("angle not chosen yet")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_cta_422(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(
            v1_content_writing.service, "start_write",
            new=AsyncMock(side_effect=service.MissingCTAError("no cta")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_generic_error_500(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(
            v1_content_writing.service, "start_write",
            new=AsyncMock(side_effect=service.ContentWritingError("unexpected")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_cta_override_forwarded(self):
        body = v1_content_writing.WriteBody(cta="Read the guide")
        with patch.object(v1_content_writing.service, "start_write",
                           new=AsyncMock(return_value=_started())) as mock_start, \
             patch.object(v1_content_writing.service, "run_write_background",
                           new=AsyncMock(return_value=None)), \
             patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
            await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
            await asyncio.sleep(0)
        assert mock_start.call_args.kwargs["cta_override"] == "Read the guide"


class TestWriteAuditLog:
    """AA-559 — write() logs the tenant's "start write" action right after start_write()
    succeeds, before the background write/check loop is kicked off."""

    @pytest.mark.asyncio
    async def test_write_started_row_logged_with_expected_fields(self):
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(v1_content_writing.service, "start_write",
                           new=AsyncMock(return_value=_started())), \
             patch.object(v1_content_writing.service, "run_write_background",
                           new=AsyncMock(return_value=None)), \
             patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()) as mock_audit:
            await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
            await asyncio.sleep(0)

        mock_audit.assert_awaited_once()
        _pool, kwargs = mock_audit.call_args.args[0], mock_audit.call_args.kwargs
        assert kwargs["tenant_id"] == TENANT_ID
        assert kwargs["actor"] == f"tenant:{TENANT_ID}"
        assert kwargs["action"] == "write.started"
        assert kwargs["resource_type"] == "angle_gate_request"
        assert kwargs["resource_id"] == str(REQUEST_ID)
        assert kwargs["details"] == {"piece_id": str(PIECE_ID)}

    @pytest.mark.asyncio
    async def test_not_logged_when_start_write_raises(self):
        """A failed start_write() (404/409/422/500) must not produce a "write started" row —
        nothing actually started."""
        body = v1_content_writing.WriteBody(cta=None)
        with patch.object(
            v1_content_writing.service, "start_write",
            new=AsyncMock(side_effect=RequestNotFoundError("nope")),
        ), patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()) as mock_audit:
            with pytest.raises(HTTPException):
                await v1_content_writing.write(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        mock_audit.assert_not_awaited()


class TestGetPiece:
    """UNCHANGED by AA-466 — fetch_piece() and this endpoint didn't need to change; kept here to
    confirm poll callers still get the right shape/status mapping at any of the 4 status values."""

    @pytest.mark.asyncio
    async def test_success(self):
        # AA-613 — fetch_piece returns the tenant-safe shape (ready_state, no raw status/gate).
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "ready", "content_text": "final"}),
        ):
            result = await v1_content_writing.get_piece(PIECE_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert result["ready_state"] == "ready"

    @pytest.mark.asyncio
    async def test_processing_state_returned_while_polling(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "in_progress", "content_text": None}),
        ):
            result = await v1_content_writing.get_piece(PIECE_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert result["ready_state"] == "in_progress"

    @pytest.mark.asyncio
    async def test_not_found_404(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(side_effect=service.ContentWritingError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.get_piece(PIECE_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404


class TestUpdatePiece:
    """AA-569 — PATCH /v1/content-writing/pieces/{piece_id}, the My Content hand-edit."""

    @pytest.mark.asyncio
    async def test_success(self):
        body = v1_content_writing.UpdateContentBody(content_text="Edited text")
        with patch.object(
            v1_content_writing.service, "update_piece_content_text",
            new=AsyncMock(return_value={"status": "approved", "content_text": "Edited text"}),
        ) as mock_update:
            result = await v1_content_writing.update_piece(PIECE_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert result["content_text"] == "Edited text"
        mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_content_422(self):
        body = v1_content_writing.UpdateContentBody(content_text="   ")
        with pytest.raises(HTTPException) as exc:
            await v1_content_writing.update_piece(PIECE_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_not_found_or_not_editable_404(self):
        body = v1_content_writing.UpdateContentBody(content_text="x")
        with patch.object(
            v1_content_writing.service, "update_piece_content_text",
            new=AsyncMock(side_effect=service.ContentWritingError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.update_piece(PIECE_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404


class TestExportPiece:
    """AA-569 — GET /v1/content-writing/pieces/{piece_id}/export?format=text|html."""

    @pytest.mark.asyncio
    async def test_text_export_any_channel(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "ready", "channel": "linkedin", "content_text": "Hello"}),
        ):
            result = await v1_content_writing.export_piece(
                PIECE_ID, _make_request(), tenant={"sub": TENANT_ID}, format="text",
            )
        assert result.media_type == "text/plain"
        assert result.body == b"Hello"

    @pytest.mark.asyncio
    async def test_html_export_blog_channel(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "ready", "channel": "blog", "content_text": "## Title\n\nBody"}),
        ):
            result = await v1_content_writing.export_piece(
                PIECE_ID, _make_request(), tenant={"sub": TENANT_ID}, format="html",
            )
        assert result.media_type == "text/html"
        assert b"<h2>Title</h2>" in result.body

    @pytest.mark.asyncio
    async def test_html_export_non_blog_channel_400(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "ready", "channel": "instagram", "content_text": "x"}),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.export_piece(
                    PIECE_ID, _make_request(), tenant={"sub": TENANT_ID}, format="html",
                )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_not_yet_written_409(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(return_value={"ready_state": "in_progress", "channel": "blog", "content_text": ""}),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.export_piece(
                    PIECE_ID, _make_request(), tenant={"sub": TENANT_ID}, format="text",
                )
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_not_found_404(self):
        with patch.object(
            v1_content_writing.service, "fetch_piece",
            new=AsyncMock(side_effect=service.ContentWritingError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.export_piece(
                    PIECE_ID, _make_request(), tenant={"sub": TENANT_ID}, format="text",
                )
        assert exc.value.status_code == 404


class TestGetLatestPiece:
    """AA-522 — GET .../requests/{request_id}/latest-piece, resume support for the T8/T9 wizard's
    write step. service.fetch_latest_piece_for_request() itself is unit-tested in
    test_aa450_content_writing_service.py::TestFetchLatestPieceForRequest — this only checks the
    router wraps it as {"piece": ...}."""

    @pytest.mark.asyncio
    async def test_returns_piece_when_present(self):
        with patch.object(
            v1_content_writing.service, "fetch_latest_piece_for_request",
            new=AsyncMock(return_value={"status": "approved", "piece_id": str(PIECE_ID)}),
        ):
            result = await v1_content_writing.get_latest_piece(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert result == {"piece": {"status": "approved", "piece_id": str(PIECE_ID)}}

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_written_yet(self):
        with patch.object(
            v1_content_writing.service, "fetch_latest_piece_for_request",
            new=AsyncMock(return_value=None),
        ):
            result = await v1_content_writing.get_latest_piece(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert result == {"piece": None}


class TestListReviews:
    """AA-501 — GET /v1/content-writing/reviews, the /portal/t10-review list."""

    @pytest.mark.asyncio
    async def test_returns_data_and_total(self):
        items = [{"request_id": str(REQUEST_ID), "ready_state": "ready"}]
        with patch.object(v1_content_writing.service, "fetch_review_list", new=AsyncMock(return_value=items)):
            result = await v1_content_writing.list_reviews(_make_request(), tenant={"sub": TENANT_ID})
        assert result == {"data": items, "total": 1}

    @pytest.mark.asyncio
    async def test_empty_list(self):
        with patch.object(v1_content_writing.service, "fetch_review_list", new=AsyncMock(return_value=[])):
            result = await v1_content_writing.list_reviews(_make_request(), tenant={"sub": TENANT_ID})
        assert result == {"data": [], "total": 0}


class TestGetReview:
    """AA-501 — GET .../requests/{request_id}/review, the new tenant-facing pre-T11 screen
    endpoint. service.fetch_review() itself is unit-tested in
    test_aa450_content_writing_service.py::TestFetchReview — this only checks the router's
    error-mapping (both possible not-found exceptions -> 404)."""

    @pytest.mark.asyncio
    async def test_success(self):
        review = {"request_id": str(REQUEST_ID), "ready_state": "ready", "content_text": "final"}
        with patch.object(v1_content_writing.service, "fetch_review", new=AsyncMock(return_value=review)):
            result = await v1_content_writing.get_review(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert result["ready_state"] == "ready"

    @pytest.mark.asyncio
    async def test_request_not_found_404(self):
        with patch.object(
            v1_content_writing.service, "fetch_review",
            new=AsyncMock(side_effect=RequestNotFoundError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.get_review(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_no_piece_written_yet_404(self):
        with patch.object(
            v1_content_writing.service, "fetch_review",
            new=AsyncMock(side_effect=service.ContentWritingError("nothing written yet")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_content_writing.get_review(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404
