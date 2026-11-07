"""AA-1047 W1 — the six W0 review follow-ups (Linear AA-1047 comment) and the T2 / T9 contract records.

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
from services.quality import elephant_policy as ep
from services.quality import final_contract as fc
from services.quality import registry as qr


def _claim(sentence, claim, why="the source is silent"):
    return (f'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{sentence}" '
            f'(claims: {claim}; source: {why})')


def _f9(*violations):
    return {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": True, "repairable": True,
            "violations": list(violations)}


# ── 3. the interim F9 policy classifies the claim span ───────────────────────────────────────────────────────────

SENT = "You dine at a family lodge near the old Tarvo fort, which has guarded the valley for 900 years."


def test_a_background_claim_next_to_a_supported_dinner_warns():
    """W0 review #1356: the place-background claim held as 'service' because the dinner was in the same sentence."""
    span = fp.claim_span(SENT, "the fort has guarded the valley for 900 years")
    assert "dine" not in span and fp.trip_promise_kinds(span) == ()
    out = fp.apply_policy(_f9(_claim(SENT, "the fort has guarded the valley for 900 years")), mode=fp.INTERIM_MODE)
    assert out["blocking"] is False and out["f9_policy"]["classes"] == {fp.UNSUPPORTED_OTHER: 1}


def test_the_dinner_claim_in_the_same_sentence_still_holds():
    out = fp.apply_policy(_f9(_claim(SENT, "dinner at a family lodge")), mode=fp.INTERIM_MODE)
    assert out["blocking"] is True and out["f9_policy"]["classes"] == {fp.TRIP_PROMISE: 1}


def test_a_chained_clause_keeps_the_trip_subject():
    span = fp.claim_span("On day 3 you visit the Ondra market and taste fresh tea at sunrise.",
                         "taste fresh tea at sunrise")
    assert span.startswith("you ") and set(fp.trip_promise_kinds(span)) >= {"time", "activity"}


def test_a_paraphrased_or_empty_claim_falls_back_to_the_sentence():
    assert fp.claim_span("You board a boat at dawn.", "x") == "You board a boat at dawn."
    assert fp.claim_span("You board a boat at dawn.", "") == "You board a boat at dawn."


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


# ── 6. the elephant code is split from the shared manual-check code ──────────────────────────────────────────────

RIDE = {"highlights": ["An elephant ride to the Tarvo gate"]}


def test_the_policy_has_its_own_code():
    assert ep.POLICY_CODE == "POLICY_ELEPHANT_RIDING" and ep.LEGACY_CODE == "FACT_CHECK_MANUAL_CHECK"
    from services.content_generation.brand_audit_node import _MANUAL_CHECK_CODES, pre_audit_checks
    assert ep.POLICY_CODE in _MANUAL_CHECK_CODES
    codes = pre_audit_checks({"name": "Tarvo", "highlights": RIDE["highlights"], "itineraries": ""}, {})
    assert ep.POLICY_CODE in codes and ep.LEGACY_CODE not in codes


@pytest.mark.parametrize("codes,issues,keep", [
    (["POLICY_ELEPHANT_RIDING"], [], True),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], ["Elephant riding is offered on day 2."], True),
    # W0 review #1355: an unrelated LLM fact check next to the ride is NOT a policy keep
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], ["A rare marsh cat is placed in the wrong valley."], False),
    (["POLICY_ELEPHANT_RIDING", "PRODUCT_TRUTH_RISK"], [], False),
    # a stored version from before the split
    (["FACT_CHECK_MANUAL_CHECK"], [], True),
    (["FACT_CHECK_MANUAL_CHECK"], ["A rare marsh cat is placed in the wrong valley."], False),
])
def test_policy_only_hold_reads_the_issues(codes, issues, keep):
    assert ep.is_policy_only_hold("manual_check", codes, RIDE, None, issues=issues) is keep


def test_no_ride_is_never_a_policy_keep():
    assert ep.is_policy_only_hold("manual_check", ["POLICY_ELEPHANT_RIDING"], {"highlights": ["A river walk"]}) \
        is False


def test_approve_gate_reads_the_issues():
    from services.quality import approve_gate
    row = {"brand_audit_status": "manual_check", "score_overall": 9.0, "failure_codes": "[]",
           "brand_audit_codes": json.dumps(["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"]),
           "brand_audit_issues": json.dumps(["A rare marsh cat is placed in the wrong valley."]),
           "aa_highlights": json.dumps(RIDE["highlights"]), "metadata": json.dumps({"grounding": {}})}
    assert approve_gate.is_policy_keep(approve_gate.stored_result(row)) is False
    assert [b["code"] for b in approve_gate.blockers(row)] == ["manual_check"]


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
        "repair_log": "[]", "created_at": datetime.datetime(2026, 11, 7), "seo_title": None, "meta_description": None, "slug": None,
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
