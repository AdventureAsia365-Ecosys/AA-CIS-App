"""AA-1047 W1 — four of the six W0 review follow-ups (Linear AA-1047 comment) and the T2 / T9 contract records.

Follow-ups 3 (F9 classifies the claim span) and 6 (elephant code split) were reverted to main behaviour (S272,
deferred to W2: claim-level ground truth + a calibrated instrument, root ADR 0020); their counter-examples are
kept, not asserted, in tests/fixtures/aa1047_w2_claim_span_cases.json.

Invented tours, places and brands only (CI golden-set text guard)."""
from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import f9_policy as fp
from services.acp_content_writing import gate_eval
from services.quality import final_contract as fc
from services.quality import registry as qr


def _claim(sentence, claim, why="the source is silent"):
    return (f'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{sentence}" '
            f'(claims: {claim}; source: {why})')


def _f9(*violations):
    return {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": True, "repairable": True,
            "violations": list(violations)}


# ── 4. ledger_snapshot classifies with the lexicon ───────────────────────────────────────────────────────────────

def test_ledger_snapshot_passes_the_lexicon_like_failing_gates():
    from services.acp_content_writing.hygiene import build_scope_lexicon
    lexicon = build_scope_lexicon("Location: Sunset Ledge, Ondra\nSource itinerary: a walk to Sunset Ledge.")
    entry = _f9(_claim("Sunset Ledge overlooks the lake.", "Sunset Ledge overlooks the lake"))
    snap = gate_eval.ledger_snapshot([entry], lexicon)[0]
    failing = gate_eval.failing_gates([entry], lexicon)[0]
    assert snap["classes"] == failing["classes"] == {fp.UNSUPPORTED_OTHER: 1}


# ── 2. writing-memory replay skips the stop-and-label entry ──────────────────────────────────────────────────────

def test_stored_failures_skip_the_label_entries():
    from services.writing_memory.replay import stored_failures
    piece = {"gate_ledger": [], "repair_log": [
        {"attempt": 3, "gate_targeted": "stop_and_label", "repair_mode": "stop_and_label",
         "violations": ["stopped after attempt 2 at $0: unsupported_trip_promise"]},
        {"attempt": 2, "gate_targeted": "stop_and_label", "repair_mode": "label", "violations": ["held: x"]},
        {"attempt": 1, "gate_targeted": "F1_grounding", "violations": ["a new number"]},
    ]}
    assert stored_failures(piece) == [("F1_grounding", True, "a new number")]


# ── 5. replay --pieces reads writer_input seed / trip ────────────────────────────────────────────────────────────

def test_replay_piece_context_reads_seed_and_trip(tmp_path):
    from scripts.quality import replay_aa900_w0 as rp
    path = tmp_path / "pieces.json"
    path.write_text(json.dumps([{"piece_id": "p1", "channel": "blog", "gate_ledger": [], "flags": [],
                                 "repair_log": [], "status": "held", "attempt_number": 2,
                                 "writer_input": {"seed": "Location: Ondra lake", "trip": "Ondra Loop"}}]))
    [p] = rp.load_pieces(str(path))
    assert p["context"]["atom_text"] == "Location: Ondra lake" and p["context"]["trip_name"] == "Ondra Loop"
    assert rp._lexicon(p["context"]) is not None


# ── 1. admin /stats splits the F9 failures by class ──────────────────────────────────────────────────────────────

def test_stats_split_keeps_outages_and_text_integrity_apart():
    from api.routers.admin_content_quality import f9_stats_split
    promise = _f9(_claim("You board a boat at dawn.", "board a boat at dawn"))
    outage = {**_f9("F9 judge unavailable — treated as fail"), "blocking": False}
    integrity = _f9("TEXT_INTEGRITY: a sentence ends mid-word")
    decided = {**_f9(), "blocking": False, "f9_policy": {"classes": {fp.JUDGE_UNAVAILABLE: 1}}}
    out = f9_stats_split([("blog", promise), ("blog", outage), ("blog", integrity), ("linkedin", decided)])
    assert out["by_channel"]["blog"] == {"failed": 1, "blocking_failed": 1, "failed_judge_unavailable": 1,
                                         "failed_text_integrity": 1}
    assert out["by_channel"]["linkedin"]["failed"] == 0
    assert out["classes"]["blog"] == {fp.TRIP_PROMISE: 1, fp.JUDGE_UNAVAILABLE: 1, fp.TEXT_INTEGRITY: 1}


# ── T2 / T9 contract records ─────────────────────────────────────────────────────────────────────────────────────

def test_t2_final_contract_record_flags_a_code_fix_after_t3():
    from services.jobs.t2_rewrite_job import final_contract_record
    master = {"name": "Ondra Loop", "itineraries": "Day 1 — Lake\nWalk.\n\nDay 2 — Ridge\nWalk.", "country": "X"}
    saved = {"name": "Ondra Loop", "summary": "A walk.", "itineraries": master["itineraries"], "subtitle": "s",
             "seo_meta": "m", "seo_title": "t"}
    gated = {**saved, "summary": "A walk — slow."}
    rec = final_contract_record(saved, master, {}, exit="done", actual="ai_generated", gated=gated, score=8.0)
    assert rec["verdict"] == fc.AI_GENERATED and rec["sha_match"] is True
    assert "POST_GATE_EDIT" in [f["code"] for f in rec["findings"] if f["severity"] == fc.WARN]
    with patch.object(fc, "check_t2", side_effect=RuntimeError("x")):
        assert final_contract_record(saved, master, {}, exit="done", actual="ai_generated") is None


def _finalize_pool():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[uuid.uuid4(), None])
    conn.fetchrow = AsyncMock(return_value={
        "piece_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "angle_gate_request_id": uuid.uuid4(),
        "attempt_number": 1, "content_text": "x", "status": "approved", "held_reason": None, "gate_ledger": "[]",
        "repair_log": "[]", "created_at": datetime.datetime(2026, 11, 7), "seo_title": None, "meta_description": None,
        "slug": None,
        "route_hub_name": None, "route_segment_count": None, "flags": "[]", "discarded_attempts": "[]"})
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool, conn


@pytest.mark.parametrize("stored,match", [("A walk by the lake.", True), ("An edited walk.", False)])
def test_t9_finalize_compares_the_stored_sha_with_the_gated_sha(stored, match):
    """ADR 0020 invariant 4: the piece is stored with the contract of the text it gated (sha compared at the write)."""
    from services.acp_content_writing import service
    record = fc.check_t9(fc.T9Input(content_text="A walk by the lake."), exit="done").record()
    pool, conn = _finalize_pool()
    with patch.object(service, "write_audit_log", new=AsyncMock()), \
         patch.object(service, "_subject_gate_after_write", new=AsyncMock()):
        asyncio.run(service._finalize_piece(pool, piece_id=uuid.uuid4(), attempt_number=1, content_text=stored,
                                            status="approved", held_reason=None, gate_ledger=[], repair_log=[],
                                            flags=[], writer_input={"seed": "s"}, final_contract=record))
    wi = json.loads(conn.fetchrow.call_args.args[15])
    assert wi["final_contract"]["sha_match"] is match and wi["final_contract"]["actual"] == "approved"
    assert wi["seed"] == "s"


def test_t9_contract_helper_never_raises():
    from services.acp_content_writing import service
    with patch.object(fc, "check_t9", side_effect=RuntimeError("x")):
        assert service._t9_final_contract("t", channel="blog", cta=None, seo_meta={}, gate_ledger=[],
                                          held_reason=None, status="approved") is None
    rec = service._t9_final_contract("A walk.", channel="linkedin", cta=None, seo_meta={}, gate_ledger=[],
                                     held_reason=None, status="approved")
    assert rec["verdict"] == fc.APPROVED and rec["text_sha"] == fc.text_sha("A walk.")
