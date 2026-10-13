"""AA-499/AA-484 — services/acp_content_writing/service.py's own embedding wiring.

Every attempt (approved OR held) computes an embedding (when tenant_id is present) and does a
CROSS-tenant lookup for AA-484's cannibalization gate — that gate needs to be re-checked on
every retry, same as every other T10 gate. Only on the FINAL 'approved' outcome does the
service additionally do a WITHIN-tenant lookup (the AA-499 reuse flag) and persist the
embedding at all (a held piece never ships, nothing to index it for).

Mirrors test_aa450_content_writing_service.py's own mocking shape (`_context()`/
`_passing_outcome()` helpers duplicated locally rather than imported, same pattern
test_aa452_t10_nine_gates.py already uses)."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import service
from services.acp_shared.piece_similarity import SimilarPiece


@pytest.fixture(autouse=True)
def _no_cta_insert():
    """S234 round 3 — run_write_background now restores the configured CTA line word for word
    (hygiene.ensure_cta). These tests pin the retry loop on placeholder texts ('draft 1') that carry
    no CTA; the CTA step has its own tests in test_aa768_s234_round3.py. S234 round 4 — the final
    text-integrity pass (one CTA, fragments) is off here too; it is covered in test_aa768_s234_round4.py."""
    with patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch("services.acp_content_writing.service.text_integrity.final_pass",
                  side_effect=lambda text, hyg, **kw: (text, [], [])):
        yield

TENANT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()

GOAL = {"key": "promotion", "name": "Promotion", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
ANGLE = {"idx": 0, "name": "A", "why_it_works": "wa", "formula_fit": "AIDA",
         "best_final_style": "warm", "recommended": True, "chosen": True}
CHANNEL_STYLE = {"key": "facebook", "tone": "casual"}
EMBEDDING = [0.1] * 1536


def _context(**over):
    base = {
        "atom_text": "atom text", "goal": GOAL, "channel_style": CHANNEL_STYLE,
        "brand_audience": {}, "chosen": ANGLE, "cta": "Book a consultation",
        "destination": None, "trip_name": None, "brand_rubric_text": "rubric",
        "channel": "facebook", "atom_id": "atom_abc123", "tenant_id": str(TENANT_ID),
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


def _passing_outcome():
    ledger = [{"gate": "F6_cta_present", "passed": True, "violations": [], "repairable": True}]
    return {"passed": True, "gate_ledger": ledger, "first_failure": None, "flags": []}


def _failing_outcome(gate="F2_banned_patterns", repairable=True):
    failure = {"gate": gate, "passed": False, "violations": [f"{gate} violation"], "repairable": repairable}
    return {"passed": False, "gate_ledger": [failure], "first_failure": failure, "flags": []}


@pytest.mark.asyncio
class TestEmbeddingComputedEveryAttempt:
    async def test_approved_piece_computes_and_persists_embedding(self):
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "compute_embedding", return_value=EMBEDDING) as mock_compute, \
             patch.object(service, "find_similar_pieces", new=AsyncMock(return_value=[])) as mock_find, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_compute.assert_called_once_with("final piece text")
        # 2 calls: cross-tenant (cannibalization, inside the loop) + within-tenant (reuse flag,
        # after the loop, approved-only).
        assert mock_find.await_count == 2
        cross_call = next(c for c in mock_find.call_args_list if c.kwargs.get("cross_tenant"))
        within_call = next(c for c in mock_find.call_args_list if not c.kwargs.get("cross_tenant"))
        assert cross_call.kwargs["exclude_tenant_id"] == TENANT_ID
        assert within_call.kwargs["tenant_id"] == TENANT_ID
        assert within_call.kwargs["exclude_piece_id"] == PIECE_ID
        assert mock_finalize.call_args.kwargs["content_embedding"] == EMBEDDING
        # run_quality_gates() got the (empty, no cannibalization match) result explicitly.
        assert mock_gates.call_args.kwargs["cannibalization_match"] is None

    async def test_held_piece_computes_and_persists_embedding(self):
        """AA-770 (S233): a held piece now reaches the tenant too, so its embedding is persisted
        like an approved one (F10 cross-tenant and within-tenant reuse must see it)."""
        with patch.object(service, "write_content", return_value=("draft", 0.02, {}, None)), \
             patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates",
                           side_effect=[_failing_outcome(repairable=True), _failing_outcome(repairable=False)]), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING) as mock_compute, \
             patch.object(service, "find_similar_pieces", new=AsyncMock(return_value=[])), \
             patch.object(service, "_finalize_piece",
                           new=AsyncMock(return_value=_finalized_row(status="held"))) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_compute.call_count == 2  # once per attempt
        assert mock_finalize.call_args.kwargs["content_embedding"] == EMBEDDING

    async def test_embedding_call_failure_soft_fails_no_similarity_query_at_all(self):
        """compute_embedding()'s own soft-fail contract (returns None) means find_similar_pieces()
        must never even be called — neither the cross-tenant nor the within-tenant path."""
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=None), \
             patch.object(service, "find_similar_pieces", new=AsyncMock()) as mock_find, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        mock_find.assert_not_awaited()
        assert mock_finalize.call_args.kwargs["content_embedding"] is None


@pytest.mark.asyncio
class TestCannibalizationMatchReachesGates:
    """AA-484 — a real cross-tenant match >= threshold must reach run_quality_gates() as
    `cannibalization_match`, correctly shaped, so gate_cannibalization() can turn it into a real
    blocking gate failure."""

    async def test_cross_tenant_match_above_threshold_passed_to_gates(self):
        cross_match = SimilarPiece(piece_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()),
                                    atom_id="atom_other_tenant", similarity=0.95)

        async def fake_find(*args, **kwargs):
            return [cross_match] if kwargs.get("cross_tenant") else []

        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", side_effect=fake_find), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        match = mock_gates.call_args.kwargs["cannibalization_match"]
        assert match["piece_id"] == cross_match.piece_id
        assert match["tenant_id"] == cross_match.tenant_id
        assert match["similarity"] == cross_match.similarity
        assert "writer_missing_brand_rules" in match

    async def test_cross_tenant_match_below_threshold_not_passed(self):
        cross_match = SimilarPiece(piece_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4()),
                                    atom_id="atom_other_tenant", similarity=0.5)

        async def fake_find(*args, **kwargs):
            return [cross_match] if kwargs.get("cross_tenant") else []

        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()) as mock_gates, \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", side_effect=fake_find), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        assert mock_gates.call_args.kwargs["cannibalization_match"] is None

    async def test_cross_tenant_query_excludes_the_writing_tenants_own_pieces(self):
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", new=AsyncMock(return_value=[])) as mock_find, \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())):
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())

        cross_call = next(c for c in mock_find.call_args_list if c.kwargs.get("cross_tenant"))
        assert cross_call.kwargs["exclude_tenant_id"] == TENANT_ID


@pytest.mark.asyncio
class TestWithinTenantReuseFlag:
    async def test_match_on_a_different_atom_above_threshold_becomes_a_flag(self):
        match = SimilarPiece(piece_id=str(uuid.uuid4()), tenant_id=str(TENANT_ID),
                              atom_id="atom_OTHER", similarity=0.97)

        async def fake_find(*args, **kwargs):
            return [] if kwargs.get("cross_tenant") else [match]

        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", side_effect=fake_find), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(atom_id="atom_abc123"), pool=MagicMock())

        flags = mock_finalize.call_args.kwargs["flags"]
        assert len(flags) == 1
        assert flags[0]["gate"] == "within_tenant_reuse"
        assert flags[0]["blocking"] is False
        assert flags[0]["similarity"] == 0.97

    async def test_match_on_the_SAME_atom_is_excluded_not_flagged(self):
        """Decision 4's own history block already covers same-atom repetition — this signal is
        specifically about a DIFFERENT atom converging on the same content."""
        match = SimilarPiece(piece_id=str(uuid.uuid4()), tenant_id=str(TENANT_ID),
                              atom_id="atom_abc123", similarity=0.99)

        async def fake_find(*args, **kwargs):
            return [] if kwargs.get("cross_tenant") else [match]

        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", side_effect=fake_find), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(atom_id="atom_abc123"), pool=MagicMock())

        assert mock_finalize.call_args.kwargs["flags"] == []

    async def test_match_below_threshold_is_not_flagged(self):
        match = SimilarPiece(piece_id=str(uuid.uuid4()), tenant_id=str(TENANT_ID),
                              atom_id="atom_OTHER", similarity=0.5)

        async def fake_find(*args, **kwargs):
            return [] if kwargs.get("cross_tenant") else [match]

        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", side_effect=fake_find), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(atom_id="atom_abc123"), pool=MagicMock())

        assert mock_finalize.call_args.kwargs["flags"] == []

    async def test_no_similar_pieces_at_all_is_not_flagged(self):
        with patch.object(service, "write_content", return_value=("final piece text", 0.02, {}, None)), \
             patch.object(service, "run_quality_gates", return_value=_passing_outcome()), \
             patch.object(service, "compute_embedding", return_value=EMBEDDING), \
             patch.object(service, "find_similar_pieces", new=AsyncMock(return_value=[])), \
             patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as mock_finalize:
            await service.run_write_background(REQUEST_ID, PIECE_ID, _context(atom_id="atom_abc123"), pool=MagicMock())

        assert mock_finalize.call_args.kwargs["flags"] == []


@pytest.mark.asyncio
class TestFinalizePiecePersistsEmbeddingAsPgvectorLiteral:
    async def test_finalize_piece_casts_embedding_to_vector_literal(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        await service._finalize_piece(
            pool, piece_id=PIECE_ID, attempt_number=1, content_text="text", status="approved",
            held_reason=None, gate_ledger=[], repair_log=[], flags=[], content_embedding=[0.1, 0.2],
        )
        query, *params = conn.fetchrow.call_args[0]
        assert "content_embedding = $13::vector" in query
        # AA-570 — discarded_attempts ($14) is now the true last param; embedding stays $13/[-2].
        assert params[12] == "[0.1,0.2]"  # $13 (AA-769 added $15 writer_input after it)

    async def test_finalize_piece_none_embedding_stays_none(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        await service._finalize_piece(
            pool, piece_id=PIECE_ID, attempt_number=1, content_text="text", status="held",
            held_reason="x", gate_ledger=[], repair_log=[], flags=[],
        )
        params = conn.fetchrow.call_args[0]
        # AA-570 — discarded_attempts ($14) is now the true last param; embedding stays $13/[-2].
        assert params[13] is None  # $13 (args[0] is the query here)


@pytest.mark.asyncio
class TestFinalizePieceAuditLog:
    """AA-559 — _finalize_piece() writes a content_piece.finished audit_log row (the real
    "content viết xong" completion event, unlike _insert_placeholder_piece()'s content_piece.
    created which fires before any LLM/gate work) on every call — covers all 3 call sites of this
    function (main loop, exception-handler fallback, buffer retry) since they all funnel through
    here."""

    async def test_writes_finished_row_with_status_and_held_reason(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row(status="held", held_reason="F2 violation")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        await service._finalize_piece(
            pool, piece_id=PIECE_ID, attempt_number=2, content_text="text", status="held",
            held_reason="F2 violation", gate_ledger=[], repair_log=[], flags=[],
        )

        conn.execute.assert_awaited_once()
        query, *params = conn.execute.call_args[0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert params[0] == str(TENANT_ID)
        assert params[1] == f"tenant:{TENANT_ID}"
        assert params[2] == "content_piece.finished"
        assert params[3] == "content_piece"
        assert params[4] == str(PIECE_ID)
        details = json.loads(params[5])
        assert details == {
            "status": "held", "held_reason": "F2 violation", "attempt_number": 2,
            "request_id": str(REQUEST_ID),
        }

    async def test_writes_finished_row_for_approved_status(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _finalized_row(status="approved")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        await service._finalize_piece(
            pool, piece_id=PIECE_ID, attempt_number=1, content_text="text", status="approved",
            held_reason=None, gate_ledger=[], repair_log=[], flags=[],
        )

        query, *params = conn.execute.call_args[0]
        details = json.loads(params[5])
        assert details["status"] == "approved"
        assert details["held_reason"] is None
