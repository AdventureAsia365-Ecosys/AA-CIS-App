"""AA-450 (write/T10 loop) + AA-466 (202 Accepted + poll split) — services/acp_content_writing/
service.py. Mocked asyncpg pool + every collaborator module, same convention
test_aa449_angle_gate_service.py already uses.

AA-466 split the single write_and_check() into:
  - start_write()          — fast pre-flight (no LLM), inserts a 'processing' placeholder row.
  - run_write_background() — the write/rewrite + T10-check loop, UNCHANGED body, now updating
                              the placeholder in place instead of inserting a final row.
Covers Phase 1's confirmed architecture (max 2 attempts, non-repairable = immediate hold),
STEP0's CTA-fallback decision, and AA-466's new failed/processing states."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import service

TENANT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()
OPTION_ID = uuid.uuid4()

GOAL = {"key": "promotion", "name": "Promotion", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
ANGLE = {"idx": 0, "name": "A", "why_it_works": "wa", "formula_fit": "AIDA",
         "best_final_style": "warm", "recommended": True, "chosen": True}
CHANNEL_STYLE = {"key": "facebook", "tone": "casual"}


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _request(status="approved", cta="Book a consultation", angles=None, channel="facebook",
             route_segment_ids=None, trip_id=None, dfs_paa_snapshot=None):
    return {
        "request_id": str(REQUEST_ID), "tenant_id": str(TENANT_ID), "atom_id": "atom_abc123",
        "trip_id": trip_id, "channel": channel, "goal": "promotion", "cta": cta,
        "status": status, "created_at": "2026-08-24T00:00:00", "updated_at": "2026-08-24T00:00:00",
        "angles": angles if angles is not None else [ANGLE],
        "route_segment_ids": route_segment_ids,  # AA-513
        "dfs_paa_snapshot": dfs_paa_snapshot,  # AA-514
    }


def _placeholder_row(**over):
    base = {
        "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "", "status": "processing",
        "held_reason": None, "gate_ledger": [], "repair_log": [],
        "created_at": datetime.now(timezone.utc),
    }
    base.update(over)
    return base


def _finalized_row(**over):
    base = {
        "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "final piece text", "status": "approved",
        "held_reason": None, "gate_ledger": [], "repair_log": [],
        "created_at": datetime.now(timezone.utc),
    }
    base.update(over)
    return base


def _context(**over):
    base = {
        "atom_text": "atom text", "goal": GOAL, "channel_style": CHANNEL_STYLE,
        "brand_audience": {}, "chosen": ANGLE, "cta": "Book a consultation",
        "destination": None, "trip_name": None, "brand_rubric_text": "rubric",
        "channel": "facebook", "atom_id": "atom_abc123",
    }
    base.update(over)
    return base


def _passing_outcome():
    ledger = [{"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True}]
    return {"passed": True, "gate_ledger": ledger, "first_failure": None, "flags": []}


def _failing_outcome(gate="F2_banned_patterns", repairable=True):
    failure = {"gate": gate, "passed": False, "violations": [f"{gate} violation"], "repairable": repairable}
    # AA-519 Việc 5 — no test in this file exercises a non-blocking failure, so `flags` stays [].
    return {"passed": False, "gate_ledger": [failure], "first_failure": failure, "flags": []}


@pytest.mark.asyncio
class TestStartWrite:
    """Fast pre-flight only — no LLM call, ends with a 'processing' placeholder insert."""

    async def test_happy_path_returns_piece_and_context(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL), \
             patch.object(service, "write_content") as mock_write, \
             patch.object(service, "run_quality_gates") as mock_gates:
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        assert result["piece"]["status"] == "processing"
        assert result["piece"]["piece_id"] == str(PIECE_ID)
        assert result["context"]["atom_text"] == "atom text"
        assert result["context"]["cta"] == "Book a consultation"
        assert result["context"]["goal"] == GOAL
        mock_write.assert_not_called()   # pre-flight never touches the LLM
        mock_gates.assert_not_called()

    async def test_request_not_approved_raises(self):
        pool = _make_pool(AsyncMock())
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(status="pending_choice"))):
            with pytest.raises(service.RequestNotReadyError):
                await service.start_write(TENANT_ID, REQUEST_ID, pool)

    async def test_missing_cta_raises_without_override(self):
        pool = _make_pool(AsyncMock())
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(cta=None))):
            with pytest.raises(service.MissingCTAError):
                await service.start_write(TENANT_ID, REQUEST_ID, pool)

    async def test_no_channel_raises(self):
        """AA-469 Việc 4 (flow-order fix) — channel is now set at a separate step 8
        (angle_gate_service.set_channel()), AFTER angle choice, not at request creation. A
        request that's 'approved' but never had a channel set must not reach the LLM call."""
        pool = _make_pool(AsyncMock())
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(channel=None))):
            with pytest.raises(service.RequestNotReadyError):
                await service.start_write(TENANT_ID, REQUEST_ID, pool)

    async def test_cta_override_used_when_stored_cta_is_null(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(cta=None))), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool, cta_override="Read the guide")

        assert result["context"]["cta"] == "Read the guide"

    async def test_angle_gate_option_id_passed_through_to_insert(self):
        """AA-497 — the placeholder INSERT now carries which angle_gate_option this piece was
        written from (migration 124's column, populated for the first time here) so a later
        reopen()+re-choice on the SAME request doesn't retroactively change what an OLD piece's
        angle attribution shows (api/routers/v1_publish.py's own AA-497 fix reads this back)."""
        option_id = uuid.uuid4()
        angle_with_option = {**ANGLE, "option_id": option_id}
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(angles=[angle_with_option]))), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            await service.start_write(TENANT_ID, REQUEST_ID, pool)

        insert_call = conn.fetchrow.call_args_list[1]
        insert_sql, *params = insert_call.args
        assert "angle_gate_option_id" in insert_sql
        assert option_id in params

    async def test_channel_passed_through_to_insert(self):
        """AA-469 Việc 4 — content_piece.channel (migration 124's column, unpopulated until this
        session) is now written on every placeholder INSERT, using the channel set on the request
        (angle_gate_service.set_channel(), step 8) — closes the gap migration 124's own header
        flagged."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(channel="tiktok"))), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            await service.start_write(TENANT_ID, REQUEST_ID, pool)

        insert_call = conn.fetchrow.call_args_list[1]
        insert_sql, *params = insert_call.args
        assert "channel" in insert_sql
        assert "tiktok" in params

    async def test_missing_option_id_on_legacy_angle_does_not_crash(self):
        """A `chosen` dict without 'option_id' (shouldn't happen post-AA-497, but defensive) must
        insert NULL, not raise a KeyError."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        assert result["piece"]["status"] == "processing"
        insert_call = conn.fetchrow.call_args_list[1]
        _, *params = insert_call.args
        assert None in params

    async def test_route_pick_builds_route_segments_context_joined_atom_text(self):
        """AA-513 — a Route/Blog pick (route_segment_ids set) must resolve EVERY Segment's own
        (atom_id, text) pair into context["route_segments"], while atom_text (fed to the quality
        GATES) stays the plain joined text — same as before this build, deliberately not
        labeled (see docs/claude_audit/AA-513-step0-investigation.md §1's digit-pollution risk)."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_placeholder_row()]  # only the INSERT — route path skips _fetch_atom_text
        pool = _make_pool(conn)
        segments = [("atom_seg1", "Cross the bamboo bridge at dawn"), ("atom_seg2", "Kayak the bay at sunset")]

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(route_segment_ids=["seg1", "seg2"], trip_id="trip1"))), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL), \
             patch.object(service, "_fetch_route_segments", new=AsyncMock(return_value=segments)) as mock_fetch:
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        mock_fetch.assert_called_once_with(TENANT_ID, "trip1", ["seg1", "seg2"], pool)
        assert result["context"]["route_segments"] == segments
        assert result["context"]["atom_text"] == "Cross the bamboo bridge at dawn\n\nKayak the bay at sunset"

    async def test_keyword_resolved_from_dfs_paa_snapshot_first_related_keyword(self):
        """AA-514 — reuses T8's own already-snapshotted signal (AA-501, migration 127), no new
        DFS call."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)
        snapshot = {"relevance": "HIGH", "people_also_ask": [], "related_keywords": ["laos temples", "vientiane"]}

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_request(dfs_paa_snapshot=snapshot))), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        assert result["context"]["keyword"] == "laos temples"

    async def test_no_snapshot_keyword_is_none(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        assert result["context"]["keyword"] is None

    async def test_non_route_request_has_none_route_segments(self):
        """Regression — a request with no route_segment_ids (every existing Segment pick or
        atom-picker request) must NOT populate context["route_segments"], and must still fetch
        the single-atom text exactly as before this build."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"text": "atom text"}, _placeholder_row()]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=_request())), \
             patch.object(service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
             patch.object(service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.start_write(TENANT_ID, REQUEST_ID, pool)

        assert result["context"]["route_segments"] is None
        assert result["context"]["atom_text"] == "atom text"


@pytest.mark.asyncio
class TestInsertPlaceholderPieceMarksSubjectUsed:
    """AA-554 mục H.1 — a content_piece being created is the one signal that marks its Slate
    proposal (if any) 'used'. Fires inside _insert_placeholder_piece(), the single INSERT call
    site shared by start_write() (primary write) and the buffer retry (AA-485,
    _maybe_buffer_retry_held_piece())."""

    async def test_marks_linked_subject_used_from_picked(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _placeholder_row()
        pool = _make_pool(conn)

        await service._insert_placeholder_piece(
            pool, tenant_id=TENANT_ID, request_id=REQUEST_ID, angle_gate_option_id=OPTION_ID,
            channel="facebook",
        )

        # AA-559 added a 2nd conn.execute() call (the audit_log INSERT, asserted separately
        # below) — the subject-state UPDATE is now call #1, not the only call.
        assert conn.execute.await_count == 2
        query, *params = conn.execute.call_args_list[0][0]
        assert "UPDATE acp_shared.subject SET state = 'used'" in query
        assert "state = 'picked'" in query
        assert params == [REQUEST_ID]

    async def test_update_resolves_subject_via_angle_gate_request_subquery(self):
        """Never overwrites a Subject already 'cut', still 'proposed', or belonging to a request
        with no Subject at all (the pre-Slate atom-picker path) — the UPDATE's own `state =
        'picked'` guard and the subquery's NULL-for-no-Subject behavior both do this without any
        Python-side branching."""
        conn = AsyncMock()
        conn.fetchrow.return_value = _placeholder_row()
        pool = _make_pool(conn)

        await service._insert_placeholder_piece(pool, tenant_id=TENANT_ID, request_id=REQUEST_ID)

        query, *_params = conn.execute.call_args_list[0][0]
        assert "SELECT subject_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid" in query

    async def test_audit_log_row_written_after_subject_update(self):
        """AA-559 — services/acp_shared/audit_log.py's write_audit_log(), called as the 2nd
        conn.execute() (after the subject-state UPDATE), same connection/transaction."""
        conn = AsyncMock()
        conn.fetchrow.return_value = _placeholder_row()
        pool = _make_pool(conn)

        await service._insert_placeholder_piece(
            pool, tenant_id=TENANT_ID, request_id=REQUEST_ID, angle_gate_option_id=OPTION_ID,
            channel="facebook",
        )

        query, *params = conn.execute.call_args_list[1][0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert params[0] == str(TENANT_ID)
        assert params[1] == f"tenant:{TENANT_ID}"
        assert params[2] == "content_piece.created"
        assert params[3] == "content_piece"
        assert params[4] == str(PIECE_ID)
        details = json.loads(params[5])
        assert details == {"request_id": str(REQUEST_ID), "channel": "facebook"}


@pytest.mark.asyncio
class TestRunWriteBackground:
    """The write/rewrite + T10-check loop — body unchanged from pre-AA-466, now updating the
    placeholder in place. _finalize_piece() is patched directly so these tests don't need to
    fake the UPDATE's conn/pool machinery — same isolation level start_write()'s tests use for
    the INSERT side."""

    async def test_happy_path_first_attempt_approved(self):
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)) as mock_write, \
             patch.object(service, "rewrite_with_feedback") as mock_rewrite, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_write.assert_called_once()
        mock_rewrite.assert_not_called()
        assert mock_finalize.call_args.kwargs["status"] == "approved"
        assert mock_finalize.call_args.kwargs["attempt_number"] == 1
        assert mock_finalize.call_args.kwargs["held_reason"] is None

    async def test_seo_meta_from_write_content_reaches_gates_and_finalize(self):
        """AA-514 — write_content()'s 3rd return value (seo_meta) must reach BOTH
        run_quality_gates() (so gate_seo_surface() has something to check) and _finalize_piece()
        (so it actually gets persisted), not be silently dropped."""
        seo_meta = {"seo_title": "A Title", "meta_description": "d" * 130 + ".", "slug": "a-slug"}
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, seo_meta, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_gates.call_args.kwargs["seo_title"] == "A Title"
        assert mock_gates.call_args.kwargs["meta_description"] == seo_meta["meta_description"]
        assert mock_gates.call_args.kwargs["slug"] == "a-slug"
        assert mock_finalize.call_args.kwargs["seo_title"] == "A Title"
        assert mock_finalize.call_args.kwargs["meta_description"] == seo_meta["meta_description"]
        assert mock_finalize.call_args.kwargs["slug"] == "a-slug"

    async def test_keyword_passed_through_to_write_content_and_gates(self):
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(
                REQUEST_ID, PIECE_ID, _context(keyword="laos temples"), pool=MagicMock(),
            )

        assert mock_write.call_args.kwargs["keyword"] == "laos temples"
        assert mock_gates.call_args.kwargs["keyword"] == "laos temples"

    async def test_missing_keyword_key_defaults_to_none(self):
        assert "keyword" not in _context()  # sanity: base fixture omits the key
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_write.call_args.kwargs["keyword"] is None

    async def test_route_segments_passed_through_to_write_content(self):
        """AA-513 — context["route_segments"] (when present) must reach write_content(), not be
        silently dropped between start_write() and the actual LLM call."""
        segments = [("atom_seg1", "text1"), ("atom_seg2", "text2")]
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(
                REQUEST_ID, PIECE_ID, _context(route_segments=segments), pool=MagicMock(),
            )

        assert mock_write.call_args.kwargs["route_segments"] == segments

    async def test_missing_route_segments_key_defaults_to_none(self):
        """A context dict built without the "route_segments" key at all (every pre-AA-513
        caller/test — the base `_context()` fixture itself has no such key) must not crash —
        dict.get(), not context["route_segments"]."""
        assert "route_segments" not in _context()  # sanity: base fixture really omits the key
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)) as mock_write, \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_write.call_args.kwargs["route_segments"] is None

    async def test_retry_once_then_approved(self):
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)) as mock_write, \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)) as mock_rewrite, \
             patch.object(service, "run_quality_gates",
                           side_effect=[_failing_outcome(), _passing_outcome()]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(attempt_number=2))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_write.assert_called_once()
        mock_rewrite.assert_called_once()
        assert mock_rewrite.call_args.kwargs["revision_feedback"] == ["F2_banned_patterns violation"]
        assert mock_fin.call_args.kwargs["status"] == "approved"
        assert mock_fin.call_args.kwargs["attempt_number"] == 2

    async def test_retry_exhausted_still_failing_holds_at_max_two_attempts(self):
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)) as mock_rewrite, \
             patch.object(service, "run_quality_gates",
                           side_effect=[_failing_outcome(), _failing_outcome()]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_rewrite.assert_called_once()  # exactly 2 attempts total, never a 3rd
        assert mock_fin.call_args.kwargs["status"] == "held"
        assert mock_fin.call_args.kwargs["held_reason"] == "F2_banned_patterns: F2_banned_patterns violation"

    async def test_non_repairable_failure_holds_immediately_no_rewrite(self):
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)) as mock_write, \
             patch.object(service, "rewrite_with_feedback") as mock_rewrite, \
             patch.object(service, "run_quality_gates",
                           return_value=_failing_outcome(gate="F6_cta_present", repairable=False)), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_write.assert_called_once()
        mock_rewrite.assert_not_called()  # non-repairable — never wastes a second attempt
        assert mock_fin.call_args.kwargs["status"] == "held"
        assert mock_fin.call_args.kwargs["attempt_number"] == 1

    async def test_exception_in_write_content_marks_failed(self):
        """AA-466 — a real system error (not a quality-gate hold) inside the background task
        must land as status='failed', with held_reason carrying the error, never silently lost
        and never conflated with a real 'held' business outcome."""
        with patch.object(service, "write_content", side_effect=RuntimeError("Bedrock throttled")), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="failed"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["status"] == "failed"
        assert "Bedrock throttled" in mock_fin.call_args.kwargs["held_reason"]
        assert mock_fin.call_args.kwargs["content_text"] == ""

    async def test_exception_during_gate_check_marks_failed_not_held(self):
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=ValueError("gate crashed")), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="failed"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["status"] == "failed"
        assert "gate crashed" in mock_fin.call_args.kwargs["held_reason"]

    async def test_finalize_write_failure_itself_is_swallowed_not_raised(self):
        """If even the failed-status UPDATE can't be written (DB blip), run_write_background()
        must not raise out of the background task — there's no caller left to catch it."""
        with patch.object(service, "write_content", side_effect=RuntimeError("boom")), \
             patch.object(service, "_finalize_piece", new=AsyncMock(side_effect=RuntimeError("db down"))):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())  # no raise


@pytest.mark.asyncio
class TestGateRegressionGuard:
    """AA-528 — a full-text rewrite (attempt 2) can fix the gate it was told about while breaking
    a DIFFERENT one that was already passing. Modeled directly on the two real held pieces AA-525
    Phần 11.B.2 found (wanderlux-travel, before this fix): 8b2562a1 (a genuinely NEW gate failure
    appeared on attempt 2 — case this guard reverts) and 319bd3d8 (the SAME gate failed on both
    attempts, just with different violation text — case this guard deliberately leaves alone,
    since reverting wouldn't change the outcome and the issue's own proposal only defines
    "regression" as a previously-PASSING gate turning to FAIL)."""

    def _outcome(self, *gates):
        """gates: (name, passed, blocking) triples. Builds a run_quality_gates()-shaped dict."""
        ledger = [{"gate": g, "passed": p, "violations": [] if p else [f"{g} violation"],
                   "repairable": True, "blocking": b} for g, p, b in gates]
        first_failure = next((g for g in ledger if not g["passed"] and g["blocking"]), None)
        return {"passed": first_failure is None, "gate_ledger": ledger,
                "first_failure": first_failure, "flags": []}

    async def test_new_blocking_gate_failure_reverts_to_attempt_one(self):
        """Case 8b2562a1: attempt 1 fails only F4_extreme_length. Attempt 2 (rewrite) STILL fails
        F4_extreme_length AND newly fails F1_grounding — strictly worse (2 blocking failures vs
        1, one of them brand new) — revert to attempt 1's own output/ledger."""
        attempt1_outcome = self._outcome(("F4_extreme_length", False, True))
        attempt2_outcome = self._outcome(("F4_extreme_length", False, True), ("F1_grounding", False, True))
        with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=[attempt1_outcome, attempt2_outcome]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["status"] == "held"
        assert mock_fin.call_args.kwargs["content_text"] == "attempt 1 text"  # NOT "attempt 2 text"
        assert mock_fin.call_args.kwargs["held_reason"] == "F4_extreme_length: F4_extreme_length violation"
        gate_names = {g["gate"] for g in mock_fin.call_args.kwargs["gate_ledger"]}
        assert gate_names == {"F4_extreme_length"}  # attempt 2's F1_grounding never persisted
        # audit trail: repair_log keeps BOTH attempts' own failures (attempt 2's real, worse
        # outcome is not hidden) plus a final entry recording the revert decision
        targets = [r["gate_targeted"] for r in mock_fin.call_args.kwargs["repair_log"]]
        assert targets == ["F4_extreme_length", "F4_extreme_length", "gate_regression_guard"]

    async def test_same_gate_different_violation_does_not_revert(self):
        """Case 319bd3d8: attempt 1 fails F1_grounding on one ungrounded number; attempt 2 fixes
        THAT number but fabricates a different one — F1_grounding fails again, same gate name,
        not a new one. Per the issue's own definition (a previously-PASSING gate turning to FAIL),
        this is not a "regression" — attempt 2's output is kept, same as before this fix."""
        attempt1_outcome = self._outcome(("F1_grounding", False, True))
        attempt2_outcome = self._outcome(("F1_grounding", False, True))
        with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=[attempt1_outcome, attempt2_outcome]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["content_text"] == "attempt 2 text"  # kept, not reverted
        targets = [r["gate_targeted"] for r in mock_fin.call_args.kwargs["repair_log"]]
        assert "gate_regression_guard" not in targets

    async def test_new_gate_failure_with_fewer_total_failures_does_not_revert(self):
        """A rewrite that fixes 2 gates and breaks 1 new one is a net improvement (1 < 2 total
        blocking failures) even though the 1 remaining failure is a gate that wasn't failing
        before — the guard only reverts when attempt 2 is NOT strictly better on total count."""
        attempt1_outcome = self._outcome(("F2_banned_patterns", False, True), ("F6_cta_present", False, True))
        attempt2_outcome = self._outcome(("F2_banned_patterns", True, True), ("F6_cta_present", True, True),
                                          ("F1_grounding", False, True))
        with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=[attempt1_outcome, attempt2_outcome]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["content_text"] == "attempt 2 text"  # kept, net improvement
        assert mock_fin.call_args.kwargs["held_reason"] == "F1_grounding: F1_grounding violation"

    async def test_regression_on_approved_second_attempt_never_triggered(self):
        """The guard lives inside the failure branch only — an attempt 2 that fully PASSES never
        reaches it, even though (in principle) it could have regressed a gate that isn't checked
        because run_quality_gates() short-circuits on CTA (AA-514) or simply doesn't re-surface a
        passing gate as a violation. Documents that this is deliberately out of scope: 'approved'
        already means every blocking gate passed on THIS attempt, which is the real invariant that
        matters for what ships."""
        attempt1_outcome = self._outcome(("F4_extreme_length", False, True))
        attempt2_outcome = self._outcome(("F4_extreme_length", True, True))
        with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=[attempt1_outcome, attempt2_outcome]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="approved"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["status"] == "approved"
        assert mock_fin.call_args.kwargs["content_text"] == "attempt 2 text"


@pytest.mark.asyncio
class TestDiscardedAttempts:
    """AA-570 — content_piece.discarded_attempts (migration 148): the attempt that did NOT
    become this row's final content_text, {attempt_number, content_text, gate_ledger}. At most
    one element, since MAX_ATTEMPTS=2."""

    def _outcome(self, *gates):
        ledger = [{"gate": g, "passed": p, "violations": [] if p else [f"{g} violation"],
                   "repairable": True, "blocking": b} for g, p, b in gates]
        first_failure = next((g for g in ledger if not g["passed"] and g["blocking"]), None)
        return {"passed": first_failure is None, "gate_ledger": ledger,
                "first_failure": first_failure, "flags": []}

    async def test_single_attempt_no_discard(self):
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row())) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["discarded_attempts"] == []

    async def test_retry_then_approved_discards_attempt_one(self):
        """The common case (AA-570's own real-data finding, 66.7% of pieces): attempt 1 fails,
        attempt 2 passes with no gate regression — attempt 1's own text/ledger is what's
        discarded."""
        attempt1_ledger = [{"gate": "F2_banned_patterns", "passed": False,
                             "violations": ["F2_banned_patterns violation"], "repairable": True}]
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates",
                           side_effect=[_failing_outcome(), _passing_outcome()]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(attempt_number=2))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        discarded = mock_fin.call_args.kwargs["discarded_attempts"]
        assert discarded == [{
            "attempt_number": 1, "content_text": "draft 1", "gate_ledger": attempt1_ledger,
        }]

    async def test_retry_both_fail_same_gate_discards_attempt_one(self):
        """No regression revert (same gate both attempts) — attempt 2's rewrite is what's kept
        as the final held content, so attempt 1 is still the one discarded."""
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates",
                           side_effect=[_failing_outcome(), _failing_outcome()]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        discarded = mock_fin.call_args.kwargs["discarded_attempts"]
        assert len(discarded) == 1
        assert discarded[0]["attempt_number"] == 1
        assert discarded[0]["content_text"] == "draft 1"

    async def test_non_repairable_immediate_hold_no_discard(self):
        """Only 1 attempt ever ran — nothing to discard."""
        with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates",
                           return_value=_failing_outcome(gate="F6_cta_present", repairable=False)), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["discarded_attempts"] == []

    async def test_gate_regression_revert_discards_attempt_two_not_attempt_one(self):
        """AA-528's revert case: attempt 1's content is what's KEPT (reverted back to), so
        attempt 2 — the just-generated rewrite that regressed — is the one reported discarded,
        not attempt 1."""
        attempt1_outcome = self._outcome(("F4_extreme_length", False, True))
        attempt2_outcome = self._outcome(("F4_extreme_length", False, True), ("F1_grounding", False, True))
        with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", side_effect=[attempt1_outcome, attempt2_outcome]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_fin.call_args.kwargs["content_text"] == "attempt 1 text"  # kept
        discarded = mock_fin.call_args.kwargs["discarded_attempts"]
        assert len(discarded) == 1
        assert discarded[0]["attempt_number"] == 2
        assert discarded[0]["content_text"] == "attempt 2 text"  # the regressed rewrite, discarded
        gate_names = {g["gate"] for g in discarded[0]["gate_ledger"]}
        assert gate_names == {"F4_extreme_length", "F1_grounding"}

    async def test_citation_tags_stripped_from_discarded_content_and_ledger(self):
        """The same AA-452 leak-prevention step that strips [R:atom_id] tags from the final
        content_text/gate_ledger must also cover the discarded attempt — it's persisted to the
        same table, no reason a tag would be safe there and not here."""
        attempt1_ledger = [{"gate": "F1_grounding", "passed": False,
                             "violations": ["sentence [R:atom_1] not grounded"], "repairable": True}]
        with patch.object(service, "write_content",
                           return_value=("draft with [R:atom_1] tag", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates",
                           side_effect=[
                               {"passed": False, "gate_ledger": attempt1_ledger,
                                "first_failure": attempt1_ledger[0], "flags": []},
                               _passing_outcome(),
                           ]), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(attempt_number=2))) as mock_fin:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        discarded = mock_fin.call_args.kwargs["discarded_attempts"][0]
        assert "[R:atom_1]" not in discarded["content_text"]
        assert "[R:atom_1]" not in discarded["gate_ledger"][0]["violations"][0]


@pytest.mark.asyncio
class TestFetchRouteSegments:
    """AA-513 — services/acp_content_writing/service.py::_fetch_route_segments() itself (AA-511
    Gap A's own `_fetch_route_text()` had zero direct test coverage before this build — added
    here as part of touching/renaming it, not backfilled elsewhere)."""

    async def test_resolves_every_segment_in_order(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            {"atom_id": "atom_seg1", "text": "text1"},
            {"atom_id": "atom_seg2", "text": "text2"},
        ]
        pool = _make_pool(conn)
        result = await service._fetch_route_segments(TENANT_ID, "trip1", ["seg1", "seg2"], pool)
        assert result == [("atom_seg1", "text1"), ("atom_seg2", "text2")]

    async def test_best_effort_skips_a_segment_rebuilt_away(self):
        """A Segment whose live representative atom is gone (rebuilt away since pick time) is
        silently skipped, not a hard failure — same "partial join degrades detail but doesn't
        fail" precedent route_detection.py::create_route_pick() already sets."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [None, {"atom_id": "atom_seg2", "text": "text2"}]
        pool = _make_pool(conn)
        result = await service._fetch_route_segments(TENANT_ID, "trip1", ["seg1_gone", "seg2"], pool)
        assert result == [("atom_seg2", "text2")]

    async def test_all_segments_gone_raises(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [None, None]
        pool = _make_pool(conn)
        with pytest.raises(service.ContentWritingError):
            await service._fetch_route_segments(TENANT_ID, "trip1", ["gone1", "gone2"], pool)

    async def test_missing_trip_id_raises(self):
        pool = _make_pool(AsyncMock())
        with pytest.raises(service.ContentWritingError):
            await service._fetch_route_segments(TENANT_ID, None, ["seg1"], pool)


@pytest.mark.asyncio
class TestFetchPiece:
    async def test_returns_tenant_safe_piece(self):
        # AA-613 — fetch_piece is a tenant endpoint: returns content + ready_state, NOT the
        # internal technical fields.
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row()
        pool = _make_pool(conn)
        result = await service.fetch_piece(TENANT_ID, uuid.uuid4(), pool)
        assert result["ready_state"] == "ready"  # approved -> ready
        assert "content_text" in result
        # leak-prevention: none of the internal/technical fields are exposed to the tenant
        for leaked in ("status", "held_reason", "gate_ledger", "repair_log",
                       "discarded_attempts", "attempt_number"):
            assert leaked not in result, f"{leaked} leaked to tenant response"

    async def test_held_piece_reads_as_ready_to_tenant(self):
        # AA-613 — a held (product-truth-unresolved) piece is still delivered to the tenant:
        # they see the content (ready_state=ready), only its publish is gated.
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row(status="held", held_reason="F1_grounding: ...")
        pool = _make_pool(conn)
        result = await service.fetch_piece(TENANT_ID, uuid.uuid4(), pool)
        assert result["ready_state"] == "ready"
        assert result["content_text"]  # content is shown
        assert "held_reason" not in result

    async def test_not_found_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)
        with pytest.raises(service.ContentWritingError):
            await service.fetch_piece(TENANT_ID, uuid.uuid4(), pool)

    async def test_seo_fields_read_back(self):
        """AA-514 — a blog piece's persisted seo_title/meta_description/slug round-trip through
        fetch_piece(), not silently dropped on read."""
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row(
            seo_title="A Title", meta_description="A description.", slug="a-slug",
        )
        pool = _make_pool(conn)
        result = await service.fetch_piece(TENANT_ID, uuid.uuid4(), pool)
        assert result["seo_title"] == "A Title"
        assert result["meta_description"] == "A description."
        assert result["slug"] == "a-slug"

    async def test_missing_seo_columns_default_to_none(self):
        """A row shape without the 3 new columns at all (defensive, shouldn't happen against the
        real SELECT list post-migration-136) must not KeyError."""
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row()  # no seo_title/meta_description/slug keys
        pool = _make_pool(conn)
        result = await service.fetch_piece(TENANT_ID, uuid.uuid4(), pool)
        assert result["seo_title"] is None
        assert result["meta_description"] is None
        assert result["slug"] is None


@pytest.mark.asyncio
class TestUpdatePieceContentText:
    """AA-569 — the My Content hand-edit. Ownership + editable-status both enforced in the
    UPDATE's WHERE clause (piece_id + tenant_id + status IN ('approved','held')) — a mocked conn
    can't actually exercise that filter, so these tests cover the two outcomes it produces
    (a row comes back, or none does) rather than the SQL text itself."""

    async def test_success_returns_updated_piece(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row(content_text="Edited by tenant")
        pool = _make_pool(conn)

        result = await service.update_piece_content_text(TENANT_ID, PIECE_ID, "Edited by tenant", pool)

        assert result["content_text"] == "Edited by tenant"
        query, *params = conn.fetchrow.call_args_list[0][0]
        assert "UPDATE acp_shared.content_piece" in query
        assert "status IN ('approved', 'held')" in query
        assert params == [PIECE_ID, TENANT_ID, "Edited by tenant"]

    async def test_audit_log_row_written(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row()
        pool = _make_pool(conn)

        await service.update_piece_content_text(TENANT_ID, PIECE_ID, "x", pool)

        query, *params = conn.execute.call_args_list[0][0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert params[2] == "content_piece.edited"
        assert params[4] == str(PIECE_ID)

    async def test_not_found_or_not_owned_or_not_editable_raises(self):
        """One WHERE clause covers 3 real cases (wrong piece_id, wrong tenant, or a
        processing/failed piece) — all indistinguishable from the caller's side, all correctly
        surfaced as 404 by the router either way."""
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)

        with pytest.raises(service.ContentWritingError):
            await service.update_piece_content_text(TENANT_ID, PIECE_ID, "x", pool)


@pytest.mark.asyncio
class TestFetchLatestPieceForRequest:
    """AA-522 — resume support for the T8/T9 wizard's write step (see AngleGateTab.tsx's own
    header comment for the bug this closes). Scoped to the CURRENTLY chosen angle option, same
    option-id-first / chosen=true-fallback convention TestFetchReview below already covers for
    fetch_review()."""

    async def test_no_piece_yet_returns_none(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"option_id": OPTION_ID}, None]
        pool = _make_pool(conn)
        result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, pool)
        assert result is None

    async def test_returns_piece_for_current_option(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"option_id": OPTION_ID}, _finalized_row()]
        pool = _make_pool(conn)
        result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, pool)
        assert result["ready_state"] == "ready"  # AA-613 tenant-safe shape
        # The second fetchrow (the piece lookup) must be scoped to the option just resolved.
        piece_query, *params = conn.fetchrow.call_args_list[1][0]
        assert params == [REQUEST_ID, TENANT_ID, OPTION_ID]

    async def test_returns_processing_placeholder_for_resume_polling(self):
        """A write that was already in flight (202 placeholder, not yet finished) must come back
        as-is — the FE resumes polling it rather than re-firing a second write."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [{"option_id": OPTION_ID}, _placeholder_row()]
        pool = _make_pool(conn)
        result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, pool)
        assert result["ready_state"] == "in_progress"  # AA-613 tenant-safe shape

    async def test_no_chosen_option_still_queries_with_none(self):
        """Defensive — shouldn't happen via the real API (this is only called once status is
        'approved', which requires a chosen angle), but must not raise if the chosen-option
        lookup somehow comes back empty."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [None, None]
        pool = _make_pool(conn)
        result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, pool)
        assert result is None
        piece_query, *params = conn.fetchrow.call_args_list[1][0]
        assert params == [REQUEST_ID, TENANT_ID, None]


def _review_request(**over):
    base = {
        "request_id": str(REQUEST_ID), "tenant_id": str(TENANT_ID), "atom_id": "atom_abc123",
        "trip_id": None, "channel": "facebook", "goal": "promotion", "cta": "Book now",
        "status": "approved",
        "dfs_paa_snapshot": {"relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"]},
        "created_at": "2026-08-24T00:00:00", "updated_at": "2026-08-24T00:00:00",
        "angles": [{**ANGLE, "option_id": OPTION_ID}],
    }
    base.update(over)
    return base


def _review_piece_row(**over):
    base = {
        "piece_id": PIECE_ID, "status": "approved", "content_text": "final piece text",
        "channel": "facebook", "angle_gate_option_id": OPTION_ID,
        "created_at": datetime.now(timezone.utc),
        # AA-519 Việc 4/5 — now selected by _LATEST_PIECE_FOR_REQUEST_QUERY.
        "route_hub_name": None, "route_segment_count": None, "flags": None,
    }
    base.update(over)
    return base


_ATOM_CONTEXT_ROW = {
    "text": "Cross the bamboo bridge", "activity_type": "adventure",
    "emotional_hook": "awe", "season_note": "dry season best",
}


@pytest.mark.asyncio
class TestFetchReview:
    """AA-501 — deliberately narrower than fetch_piece(): assembles full write context (atom/
    tour/goal/angle/DFS-PAA/channel) but never returns gate_ledger/repair_log/held_reason. This
    is a stricter contract than the T8/T9 wizard's own end-of-flow card (which uses fetch_piece()
    and DOES show held_reason) — a deliberate divergence per Nghiệp's AA-501 build decision, not
    a bug to reconcile with fetch_piece()'s behavior."""

    async def test_happy_path_assembles_full_context_no_gate_detail(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_review_piece_row(), _ATOM_CONTEXT_ROW]
        pool = _make_pool(conn)

        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        assert result["ready_state"] == "ready"
        assert result["content_text"] == "final piece text"
        assert result["channel"] == "facebook"
        assert result["goal"] == {"key": "promotion", "label": "Promotion"}
        assert result["angle"]["name"] == "A"
        assert result["atom"]["activity_type"] == "adventure"
        assert result["dfs_paa_snapshot"] == {
            "relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"],
        }
        assert "gate_ledger" not in result
        assert "repair_log" not in result
        assert "held_reason" not in result

    async def test_held_piece_not_ready_but_content_still_shown(self):
        """migration 115/118's own precedent: held keeps real writer output visible for review —
        content_text is real here, just the ready_state hides WHY it's held."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _review_piece_row(status="held", content_text="drafted but held"), None,
        ]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        assert result["ready_state"] == "not_ready"
        assert result["content_text"] == "drafted but held"

    async def test_processing_piece_in_progress_no_content(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_review_piece_row(status="processing", content_text=""), None]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        assert result["ready_state"] == "in_progress"
        assert result["content_text"] is None

    async def test_failed_piece_not_ready_no_content(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_review_piece_row(status="failed", content_text=""), None]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        assert result["ready_state"] == "not_ready"
        assert result["content_text"] is None

    async def test_no_piece_written_yet_raises(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None  # no content_piece rows for this request at all
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())):
            with pytest.raises(service.ContentWritingError):
                await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

    async def test_query_orders_by_created_at_not_attempt_number(self):
        """AA-497 (migration 125) — content_piece is no longer 1-row-per-request; the query must
        pick the latest by created_at, never assume attempt_number orders a request's history."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            _review_piece_row(content_text="latest after reopen"), _ATOM_CONTEXT_ROW,
        ]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request())), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        query = conn.fetchrow.call_args_list[0].args[0]
        assert "ORDER BY created_at DESC" in query
        assert "LIMIT 1" in query
        assert "gate_ledger" not in query and "repair_log" not in query and "held_reason" not in query
        assert result["content_text"] == "latest after reopen"

    async def test_no_trip_id_no_tour_context_and_trips_never_fetched(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_review_piece_row(), _ATOM_CONTEXT_ROW]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request(trip_id=None))), \
             patch.object(service, "get_goal", return_value=GOAL), \
             patch.object(service, "fetch_tenant_trips") as mock_trips:
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        mock_trips.assert_not_called()
        assert result["tour"] is None

    async def test_channel_falls_back_to_request_when_piece_channel_null(self):
        """Defensive fallback for pre-AA-469-Việc-4 rows where content_piece.channel is NULL —
        same COALESCE(cp.channel, agr.channel) every other real read site uses."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_review_piece_row(channel=None), _ATOM_CONTEXT_ROW]
        pool = _make_pool(conn)
        with patch.object(service.angle_gate_service, "fetch_request",
                           new=AsyncMock(return_value=_review_request(channel="facebook"))), \
             patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review(TENANT_ID, REQUEST_ID, pool)

        assert result["channel"] == "facebook"


def _review_list_row(**over):
    base = {
        "piece_id": PIECE_ID, "angle_gate_request_id": REQUEST_ID, "status": "approved",
        "content_text": "final piece text", "created_at": datetime.now(timezone.utc),
        "channel": "facebook", "goal": "promotion", "cta": "Book now",
        "trip_id": None,
        "dfs_paa_snapshot": {"relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"]},
        "angle_name": "A", "angle_why_it_works": "wa",
        "angle_formula_fit": "AIDA", "angle_best_final_style": "warm",
        "atom_text": "Cross the bamboo bridge", "atom_activity_type": "adventure",
        "atom_emotional_hook": "awe", "atom_season_note": "dry season best",
        # AA-519 Việc 4/5 — now selected by _TENANT_REVIEWS_QUERY.
        "route_hub_name": None, "route_segment_count": None, "flags": None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
class TestFetchReviewList:
    """AA-501 — GET /v1/content-writing/reviews, the /portal/t10-review list. One query (plus at
    most one fetch_tenant_trips() call, never per-row) — no gate_ledger/repair_log/held_reason
    anywhere, same contract as fetch_review()."""

    async def test_happy_path_single_row(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[_review_list_row()])
        pool = _make_pool(conn)

        with patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review_list(TENANT_ID, pool)

        assert len(result) == 1
        item = result[0]
        assert item["request_id"] == str(REQUEST_ID)
        assert item["piece_id"] == str(PIECE_ID)
        assert item["ready_state"] == "ready"
        assert item["content_text"] == "final piece text"
        assert item["goal"] == {"key": "promotion", "label": "Promotion"}
        assert item["angle"]["name"] == "A"
        assert item["atom"]["activity_type"] == "adventure"
        assert item["dfs_paa_snapshot"] == {
            "relevance": "HIGH", "people_also_ask": ["q1"], "related_keywords": ["k1"],
        }
        assert "gate_ledger" not in item and "repair_log" not in item and "held_reason" not in item

    async def test_empty_list(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)
        result = await service.fetch_review_list(TENANT_ID, pool)
        assert result == []

    async def test_held_row_content_shown_processing_row_content_hidden(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            _review_list_row(piece_id=uuid.uuid4(), status="held", content_text="drafted but held"),
            _review_list_row(piece_id=uuid.uuid4(), status="processing", content_text=""),
        ])
        pool = _make_pool(conn)
        with patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review_list(TENANT_ID, pool)

        held, processing = result
        assert held["ready_state"] == "not_ready" and held["content_text"] == "drafted but held"
        assert processing["ready_state"] == "in_progress" and processing["content_text"] is None

    async def test_trips_fetched_once_not_per_row_when_trip_ids_present(self):
        """No N+1 — fetch_tenant_trips() must be called at most once for the whole list, not
        once per row, even when multiple rows share/have a trip_id."""
        trip_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            _review_list_row(piece_id=uuid.uuid4(), trip_id=trip_id),
            _review_list_row(piece_id=uuid.uuid4(), trip_id=trip_id),
        ])
        pool = _make_pool(conn)
        trip = MagicMock(id=trip_id, destination="Vietnam")
        trip.name = "Sapa Trek"  # Mock(name=...) is reserved for the mock's own repr name
        mock_trips = AsyncMock(return_value=[trip])
        with patch.object(service, "get_goal", return_value=GOAL), \
             patch.object(service, "fetch_tenant_trips", new=mock_trips):
            result = await service.fetch_review_list(TENANT_ID, pool)

        mock_trips.assert_awaited_once()
        assert all(item["tour"] == {"name": "Sapa Trek", "destination": "Vietnam"} for item in result)

    async def test_no_trip_id_rows_never_call_fetch_tenant_trips(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[_review_list_row(trip_id=None)])
        pool = _make_pool(conn)
        with patch.object(service, "get_goal", return_value=GOAL), \
             patch.object(service, "fetch_tenant_trips") as mock_trips:
            result = await service.fetch_review_list(TENANT_ID, pool)

        mock_trips.assert_not_called()
        assert result[0]["tour"] is None

    async def test_dfs_paa_snapshot_parsed_when_json_string(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[_review_list_row(
            dfs_paa_snapshot=json.dumps({"relevance": "LOW", "people_also_ask": [], "related_keywords": []}),
        )])
        pool = _make_pool(conn)
        with patch.object(service, "get_goal", return_value=GOAL):
            result = await service.fetch_review_list(TENANT_ID, pool)

        assert result[0]["dfs_paa_snapshot"] == {
            "relevance": "LOW", "people_also_ask": [], "related_keywords": [],
        }

    async def test_query_never_selects_gate_or_repair_or_held_fields(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)
        await service.fetch_review_list(TENANT_ID, pool)

        query = conn.fetch.call_args.args[0]
        assert "gate_ledger" not in query and "repair_log" not in query and "held_reason" not in query

    async def test_tenant_id_bound_as_both_uuid_and_text_params(self):
        """Real bug caught by live-verify (30/08/2026, real RDS): the query used to reuse ONE
        placeholder both bare (against cp.tenant_id, uuid) and cast ($1::text, against
        ta.owner_scope, text) — asyncpg's single-preparation type inference picked ONE type for
        it, and Postgres has no `uuid = text` operator, so every real call 500'd
        ("UndefinedFunctionError: operator does not exist: uuid = text"). Mocked-pool unit tests
        alone can't catch this (mocks don't validate real SQL type resolution) — this only
        pins the fix shape: tenant_id must be bound as TWO separate params, uuid then str."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)
        await service.fetch_review_list(TENANT_ID, pool)

        args = conn.fetch.call_args.args
        assert args[1] == TENANT_ID  # bound bare (uuid) — compared against cp.tenant_id
        assert args[2] == str(TENANT_ID)  # bound as str (text) — compared against ta.owner_scope
        query = args[0]
        assert "$1::text" not in query, "reusing $1 both bare and ::text is the exact bug that shipped"
