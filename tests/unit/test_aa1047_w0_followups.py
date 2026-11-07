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


@pytest.mark.parametrize("sentence,claim", [
    # W1 fix-pass review: a promise in a relative / attached clause is still the trip's
    ("You stay at a riverside lodge, where dinner is served on the terrace.", "dinner served on the terrace"),
    ("You spend the night at the Ondra guesthouse, with breakfast on the veranda.", "breakfast on the veranda"),
    ("Day 3 is a free day, with a cooking class at the homestay.", "a cooking class at the homestay"),
    ("You reach the Tarvo ridge, which looks out over the valley at sunset.", "looks out over the valley at sunset"),
])
def test_a_promise_in_an_attached_clause_still_holds(sentence, claim):
    assert fp.claim_kinds(sentence, claim) != ()
    out = fp.apply_policy(_f9(_claim(sentence, claim)), mode=fp.INTERIM_MODE)
    assert out["blocking"] is True and out["f9_policy"]["classes"] == {fp.TRIP_PROMISE: 1}


@pytest.mark.parametrize("sentence,claim", [
    (SENT, "the fort has guarded the valley for 900 years"),
    ("You walk to the Ondra bridge, which was built by the old salt traders.", "built by the old salt traders"),
])
def test_a_background_relative_clause_still_warns(sentence, claim):
    assert fp.claim_kinds(sentence, claim) == ()
    out = fp.apply_policy(_f9(_claim(sentence, claim)), mode=fp.INTERIM_MODE)
    assert out["blocking"] is False and out["f9_policy"]["classes"] == {fp.UNSUPPORTED_OTHER: 1}


def test_a_trip_term_span_with_its_subject_after_falls_back_to_the_sentence():
    """No subject before the clause: the span alone names a service only, so the whole sentence decides."""
    sentence = "Served on the terrace each evening, dinner is included for you at the lodge."
    assert fp.claim_kinds(sentence, "served on the terrace each evening") != ()


# W1 fix pass 2 (review 1, blocking): an ACTIVITY promise in a relative / attached / list clause still holds — the
# span classifies as none, so the whole sentence decides (fail safe).
@pytest.mark.parametrize("sentence,claim", [
    ("You stay in the Ondra village, where a kayak excursion on the lake awaits.",
     "a kayak excursion on the lake awaits"),
    ("You spend day 3 at the lake, with a kayak excursion to the island.", "a kayak excursion to the island"),
    ("You visit the old market, the Ondra temple and the floating village.", "the floating village"),
    ("We stop at the Ondra market, then the hot springs.", "the hot springs"),
    ("You visit the old market, the Ondra temple, the floating village.", "the floating village"),
    ("You meet Ana, a local weaver who teaches you to dye silk.", "teaches you to dye silk"),
    ("You stay in the Ondra village, where weaving lessons are held for guests.",
     "weaving lessons are held for guests"),
    ("You overnight in Ondra, including a sunrise hike up the volcano.", "a sunrise hike up the volcano"),
    ("You cross the river, before a short climb to the Tarvo caves.", "a short climb to the Tarvo caves"),
    ("You board the boat, while a naturalist points out the birds.", "a naturalist points out the birds"),
])
def test_an_activity_promise_in_an_attached_or_list_clause_holds(sentence, claim):
    assert fp.claim_kinds(sentence, claim) != ()
    out = fp.apply_policy(_f9(_claim(sentence, claim)), mode=fp.INTERIM_MODE)
    assert out["blocking"] is True and out["f9_policy"]["classes"] == {fp.TRIP_PROMISE: 1}


# W1 fix pass 2 (review 1, minor): place background warns even when it names a view or a time word, and the trip
# subject is not put in front of it; a claim-word tie goes to the clause with the claim's verb.
@pytest.mark.parametrize("sentence,claim", [
    ("You dine at the lodge, near the Tarvo fort, which looks out over the valley.",
     "the fort looks out over the valley"),
    ("You dine at the lodge, near the temple, where monks have chanted at dawn for centuries.",
     "monks have chanted at dawn for centuries"),
    ("You dine at the lodge, near the temple famed for its views.", "the temple famed for its views"),
    ("You stay at the lodge, on Camp Road, which was paved in 1990.", "Camp Road was paved"),
])
def test_place_background_with_a_view_or_time_word_warns(sentence, claim):
    assert fp.claim_kinds(sentence, claim) == ()
    out = fp.apply_policy(_f9(_claim(sentence, claim)), mode=fp.INTERIM_MODE)
    assert out["blocking"] is False and out["f9_policy"]["classes"] == {fp.UNSUPPORTED_OTHER: 1}


# W1 fix pass 3: a dated "built in" is place history, not an inclusion; a timed event after a locative antecedent
# (no background marker) and an event noun are not proven background, so they hold.
@pytest.mark.parametrize("sentence,claim,holds", [
    ("You visit the temple, which was built in 1200.", "built in 1200", False),
    ("You visit the market, then the Ondra temple, which dates back to 1100.", "dates back to 1100", False),
    ("You stay in Ondra, near the old fort, which comes alive with a lantern parade each evening.",
     "lantern parade each evening", True),
    ("You stay in Ondra, near the old fort, which hosts a nightly dance show.", "hosts a nightly dance show", True),
    ("You stay near the lake, where boat tours have run since 1990.", "boat tours have run since 1990", True),
    ("You stay near the waterfall, famous for its sunrise boat rides.", "famous for its sunrise boat rides", True),
    ("You stay near the waterfall, where swimming is possible.", "swimming is possible", True),
])
def test_dated_history_warns_and_timed_events_hold(sentence, claim, holds):
    assert (fp.claim_kinds(sentence, claim) != ()) is holds


def test_built_in_is_still_an_inclusion_without_a_date():
    assert fp.INCLUSION_RE.search("Time to swim is built in.")
    assert not fp.INCLUSION_RE.search("The bridge was built in 1200.")
    assert not fp.INCLUSION_RE.search("The bridge was built in the early 1900s.")


def test_a_claim_word_tie_goes_to_the_clause_with_the_claims_verb():
    assert fp.claim_span("You stay at the lodge, on Camp Road, which was paved in 1990.", "Camp Road was paved") \
        == "which was paved in 1990."


def test_claim_span_takes_the_lexicon():
    """The scope lexicon is passed through (signature): a None lexicon keeps the unmasked text."""
    s = "You reach the Tarvo ridge, which looks out over the valley at sunset."
    assert fp.claim_span(s, "looks out over the valley at sunset", None).startswith("You ")


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


# A realistic v2 issue list: the voice / SEO findings are report-only codes but their issue text stays.
VOICE_SEO = ["SEO meta is 162 characters; keep it between 140 and 155.",
             "The subtitle reads as a list of towns.", "The summary leans on hype ('unforgettable')."]
JUDGE_FEEDBACK = ["Calm and precise overall; the second highlight could be more specific."]
WILD = "Wild elephant sightings at the Tarvo reserve are not in the source."
SANCTUARY = "The elephant sanctuary is placed in the wrong valley."
RIDE_ISSUE = "Elephant riding is offered on day 2."


@pytest.mark.parametrize("codes,issues,keep", [
    (["POLICY_ELEPHANT_RIDING"], [], True),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [RIDE_ISSUE], True),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [RIDE_ISSUE, *VOICE_SEO], True),
    # W0 review #1355: an unrelated LLM fact check next to the ride is NOT a policy keep
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], ["A rare marsh cat is placed in the wrong valley."], False),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [RIDE_ISSUE, WILD], False),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [RIDE_ISSUE, SANCTUARY], False),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [WILD], False),
    # the fact check must be explained as the ride: none, or only voice / SEO text, keeps the hold
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], [], False),
    (["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"], VOICE_SEO, False),
    (["POLICY_ELEPHANT_RIDING", "PRODUCT_TRUTH_RISK"], [], False),
    # a stored version from before the split: the pre-split rule unless an issue names another concern
    (["FACT_CHECK_MANUAL_CHECK"], [], True),
    (["FACT_CHECK_MANUAL_CHECK"], JUDGE_FEEDBACK, True),
    (["FACT_CHECK_MANUAL_CHECK"], [RIDE_ISSUE, *VOICE_SEO], True),
    (["FACT_CHECK_MANUAL_CHECK"], ["A rare marsh cat is placed in the wrong valley."], False),
    (["FACT_CHECK_MANUAL_CHECK"], [*VOICE_SEO, WILD], False),
])
def test_policy_only_hold_reads_the_issues(codes, issues, keep):
    assert ep.is_policy_only_hold("manual_check", codes, RIDE, None, issues=issues) is keep


@pytest.mark.parametrize("text,riding", [
    ("Elephant riding is offered on day 2.", True),
    ("The page must not promote elephant rides.", True),
    ("An elephant-back crossing of the Tarvo river.", True),
    ("A morning elephant safari on the back of a trained elephant.", True),
    (WILD, False),
    (SANCTUARY, False),
    ("The elephant safari location does not match the source.", False),
])
def test_names_riding_is_about_the_ride_not_the_animal(text, riding):
    assert ep.names_riding(text) is riding


# W1 fix pass 2 (reviews 1 + 2): the issue rule fails CLOSED. Any issue that is not about the ride and not a narrow
# voice / SEO / format issue keeps the hold, however it is worded; a mixed riding issue is split into clauses.
POLICY_FC = ["POLICY_ELEPHANT_RIDING", "FACT_CHECK_MANUAL_CHECK"]
LEGACY = ["FACT_CHECK_MANUAL_CHECK"]
PRIVATE_BOAT = "Highlights promise a private boat to the islands; the itinerary only lists a shared ferry."
LEOPARD = "Elephant riding on day 2 and a rare clouded leopard sighting the source does not support need a fact check."
BALLOON_MIXED = "Fact check: elephant ride on Day 2; also the Day 4 hot-air balloon is not in the source."


@pytest.mark.parametrize("codes,issues", [
    # review 1
    (POLICY_FC, [RIDE_ISSUE, "The hotel on day 3 is a different property."]),
    (POLICY_FC, [RIDE_ISSUE, "Day 4 adds a hot-air balloon flight."]),
    (POLICY_FC, [RIDE_ISSUE, "A balloon flight at sunrise is promised."]),
    (POLICY_FC, ["Elephant riding is offered on day 2, and the hotel on day 3 is not in the source."]),
    # review 2: unknown wordings
    (POLICY_FC, [RIDE_ISSUE, PRIVATE_BOAT]),
    (LEGACY, [PRIVATE_BOAT]),
    (POLICY_FC, [RIDE_ISSUE, "The Day 3 cooking class is not listed in the itinerary."]),
    (POLICY_FC, [RIDE_ISSUE, "Cannot confirm the tour includes a visit to the Royal Palace."]),
    (POLICY_FC, [RIDE_ISSUE, "Sunrise at Mount Popa is mentioned in the summary but absent from the itinerary."]),
    (POLICY_FC, [RIDE_ISSUE,
                 "The hot-air balloon flight is presented as included but is an optional extra at additional cost."]),
    # review 2: several concerns in one riding issue
    (POLICY_FC, [LEOPARD]),
    (LEGACY, [LEOPARD]),
    (POLICY_FC, [BALLOON_MIXED]),
    (LEGACY, [BALLOON_MIXED]),
    # review 2 (minor): legacy judge praise naming a product-truth word now holds (fail safe, documented)
    (LEGACY, ["Calm voice; the 4-day duration reads well."]),
    (LEGACY, ["Faithful to the source and precise on location; calm voice."]),
    # review 2 (minor): a bare string is one issue, not a list of letters
    (LEGACY, "Tiger sighting not in the source"),
])
def test_an_unaccounted_issue_keeps_the_hold(codes, issues):
    assert ep.is_policy_only_hold("manual_check", codes, RIDE, None, issues=issues) is False


@pytest.mark.parametrize("codes,issues", [
    (POLICY_FC, "Elephant riding offered"),
    (POLICY_FC, ["Per policy, elephant riding stays as is and needs a human review."]),
    (POLICY_FC, ["FACT_CHECK_MANUAL_CHECK: elephant riding on day 2 must be manually checked."]),
    (POLICY_FC, ["Elephant riding is offered on day 2; the SEO meta is 162 characters."]),
    (LEGACY, []),
    (LEGACY, None),
])
def test_a_riding_or_voice_only_issue_list_is_the_policy(codes, issues):
    assert ep.is_policy_only_hold("manual_check", codes, RIDE, None, issues=issues) is True


# W1 fix pass 3: how the LLM audit usually words the ride (brand_standards: "Elephant riding: always
# FACT_CHECK_MANUAL_CHECK") is the policy; a relative "which must be verified" or a "(Day 3)" locator names no
# other concern.
@pytest.mark.parametrize("issue", [
    "Highlights mention elephant riding, which requires manual fact check per brand standards.",
    "Elephant riding mentioned in Day 2 itinerary - requires FACT_CHECK_MANUAL_CHECK per AA brand standards.",
    "Elephant riding is included on Day 2; this requires a manual fact check.",
    "Itinerary includes elephant riding (Day 3), which must be manually verified.",
    "The tour offers an elephant ride at the camp; flagged for human review per brand standards.",
    "Elephant back safari on Day 2 needs manual verification.",
])
def test_usual_riding_issue_wordings_are_the_policy(issue):
    assert ep.is_policy_only_hold("manual_check", POLICY_FC, RIDE, None, issues=[issue]) is True


@pytest.mark.parametrize("issue", [
    "Itinerary includes elephant riding (Day 3), which is not in the source.",
    "Elephant riding on day 2, which must be verified, and the Day 4 sunset cruise is not listed.",
])
def test_a_riding_issue_with_another_concern_in_a_relative_still_holds(issue):
    assert ep.is_policy_only_hold("manual_check", POLICY_FC, RIDE, None, issues=[issue]) is False


@pytest.mark.parametrize("text,voice", [
    ("SEO meta is 162 characters; keep it between 140 and 155.", True),
    ("The subtitle reads as a list of towns.", True),
    ("The meta promises a private boat to the islands.", False),
    ("Day 4 adds a hot-air balloon flight.", False),
])
def test_is_voice_issue_is_narrow(text, voice):
    assert ep.is_voice_issue(text) is voice


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
