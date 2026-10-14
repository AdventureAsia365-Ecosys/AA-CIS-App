"""AA-801 option (b) rubric anchors (T2_JUDGE_ANCHORS, default off) and option (c) the adhoc-only
judge model override. All mock-only: no AWS, no DB, no LLM.
"""
import dataclasses
import json
from unittest.mock import patch

import pytest

from services.content_generation import brand_fit as bf
from services.content_generation import judge_node as jn
from shared.llm_client.models import LLMResponse

GEN = {"name": "Kyoto Days", "subtitle": "Quiet temples", "summary": "Calm.", "highlights": ["Nanzen-ji"],
       "itineraries": "Day 1 — Arrive Kyoto.\nWe ride out at dawn, legs burning, toward the temples.",
       "seo_title": "Kyoto", "seo_meta": "A calm Kyoto trip."}

FULL = {"brand_name": "Trail Pulse", "brand_core_idea": "Movement and freedom.",
        "brand_customer_segment": "active couples", "brand_customer_mindset": "to feel alive",
        "brand_voice_examples": ["Energetic", "", "Direct"],
        "brand_good_examples": "We ride out at dawn, legs burning, the valley opening below us.",
        "brand_forbidden_words": ["luxury", "pamper"]}
BARE = {"brand_customer_mindset": "unhurried"}     # no name, core idea, voice, example or forbidden words


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    for k in ("T2_JUDGE_ANCHORS", "T2_JUDGE_REPEAT", "T2_JUDGE_GATE"):
        monkeypatch.delenv(k, raising=False)


# ── build_rubric_anchors ───────────────────────────────────────────────────────────────────────

def test_anchors_use_every_brand_pack_field():
    text = bf.build_rubric_anchors(FULL, "Movement and freedom.")
    assert "SCORE ANCHORS" in text
    for anchor in ("- 10:", "- 8:", "- 7:", "- 6:", "- 4:"):
        assert text.count(anchor) == 2            # brand_fit and cross_brand_distinct
    assert "Trail Pulse" in text
    assert '"Movement and freedom."' in text
    assert "active couples" in text and "to feel alive" in text
    assert "(Energetic, Direct)" in text          # empty voice entries dropped
    assert "We ride out at dawn" in text
    assert "caps brand_fit_score at 6: luxury, pamper." in text


def test_anchors_without_examples_or_core_idea_leave_those_clauses_out():
    text = bf.build_rubric_anchors(BARE, None)
    assert "this brand" in text
    assert "core idea" not in text.split("- 8:")[0]      # no core-idea clause in the 10 anchor
    assert "on-brand example" not in text
    assert "forbidden" not in text
    assert "(none)" not in text
    assert "its travellers (what they want: unhurried)" in text


def test_anchors_are_deterministic_and_clip_a_long_example():
    long = dict(FULL, brand_good_examples="word " * 300)
    a, b = bf.build_rubric_anchors(long, "m"), bf.build_rubric_anchors(long, "m")
    assert a == b
    quoted = a.split('allow: "')[1].split('"')[0]
    assert len(quoted) <= bf._ANCHOR_EXAMPLE_CHARS + 2 and quoted.endswith("…")


def test_mission_argument_wins_over_the_profile_core_idea():
    assert '"Tenant mission"' in bf.build_rubric_anchors(FULL, "Tenant mission")


# ── _build_judge_prompt(anchors=...) ───────────────────────────────────────────────────────────

def test_prompt_without_anchors_has_no_anchor_block():
    for mission in (None, "Movement and freedom."):
        p = bf._build_judge_prompt(FULL, GEN, mission)
        assert p == bf._build_judge_prompt(FULL, GEN, mission, anchors=False)
        assert "SCORE ANCHORS" not in p and "mission_evidence" not in p


def test_anchored_prompt_with_mission_has_the_quoted_yes_no_test():
    p = bf._build_judge_prompt(FULL, GEN, "Movement and freedom.", anchors=True)
    assert "SCORE ANCHORS" in p
    assert "MISSION TEST" in p and 'core idea is:\n  "Movement and freedom."' in p
    assert "WORD FOR WORD" in p
    shape = p.rsplit("exactly:\n", 1)[1]
    keys = list(json.loads(shape.replace("<int>", "1").replace("<bool>", "true")))
    assert set(keys) == {"brand_fit_score", "cross_brand_distinct", "mission_present", "mission_evidence",
                         "feedback"}
    # the quote is asked for before the verdict
    assert keys.index("mission_evidence") < keys.index("mission_present")


def test_anchored_mission_test_does_not_ask_one_sentence_to_express_the_whole_core_idea():
    """S236 round 2: "answer true ONLY when you can copy one sentence VERBATIM ... that expresses it"
    got mission=false on 45/45 Dev judgements. The quote only has to SHOW the idea, and the yes bar is
    the brand_fit 7 anchor's (overview + at least half of the days, not quoted)."""
    p = bf._build_judge_prompt(FULL, GEN, "Movement and freedom.", anchors=True)
    assert "ONLY when you can copy one sentence" not in p
    assert "does not have to name or restate the core idea" in p
    assert "at least half of the itinerary days reflect this core idea" in p
    assert 'without the "…" that marks a' in p


# ── offline replay: render the real prompt, parse realistic replies ────────────────────────────

_PACK = {"brand_name": "Atlas & Hearth",
         "brand_core_idea": "Private cultural journeys with depth, beauty, and meaning for discerning travellers",
         "brand_customer_segment": "Senior executives, private wealth, cultural philanthropists aged 45-65",
         "brand_customer_mindset": "They seek depth over access, meaning over spectacle.",
         "brand_voice_examples": ["elegant", "discreet", "cultured", "calm", "precise"],
         "brand_good_examples": "At dusk the monastery courtyard empties.",
         "brand_forbidden_words": ["bucket list"]}
_DRAFT = {"name": "Mongolia: Khövsgöl Lake and Gobi Desert", "subtitle": "Private steppe journey",
          "summary": "A private journey into the Mongolian steppe.", "highlights": ["Gandan library"],
          "itineraries": [
              {"day": 1, "title": "Ulaanbaatar",
               "body": "Your private guide meets you at the airport. In the afternoon a curator opens the "
                       "Gandan monastery library for a quiet hour among its sutras."},
              {"day": 2, "title": "Khövsgöl",
               "body": "Fly north to the lake. Evening by the water with a herder family who share the "
                       "story of their reindeer. " + "The shore path continues. " * 20},
          ],
          "seo_title": "Mongolia", "seo_meta": "Private Mongolia."}


def _replay(reply: str):
    """score_brand_fit end to end on the rendered prompt with a canned model reply."""
    with patch.object(bf, "LLMClient") as client, patch.object(bf, "_seeded_model", return_value=True):
        client.return_value.generate.return_value = LLMResponse(
            content=reply, model_used="satellite-gpt-5.6-luna", cost_usd=0.001, provider="bedrock")
        out = bf.score_brand_fit(_PACK, _DRAFT, stage="t2_judge", mission=_PACK["brand_core_idea"],
                                 anchors=True)
    return out, client.return_value.generate.call_args.args[0].user_prompt


def test_replay_yes_with_a_quote_from_the_digest_counts():
    reply = ('{"brand_fit_score": 8, "cross_brand_distinct": 7, "mission_evidence": '
             '"In the afternoon a curator opens the Gandan monastery library for a quiet hour among its '
             'sutras.", "mission_present": true, "feedback": ""}')
    out, prompt = _replay(reply)
    assert "a curator opens the Gandan monastery library" in prompt      # the judge saw it
    assert out.mission_present is True and out.evidence_verified is True
    assert out.mission_evidence.startswith("In the afternoon") and out.judge_score == 7.0


def test_replay_quote_with_day_header_and_cut_marker_still_verifies():
    reply = ('```json\n{"brand_fit_score": 8, "cross_brand_distinct": 8, "mission_evidence": '
             '"Evening by the water with a herder family who share the story of their reindeer. The shore\u2026",'
             ' "mission_present": "true", "feedback": ""}\n```')
    out, _ = _replay(reply)
    assert out.mission_present is True and out.evidence_verified is True and out.judge_score == 8.0


def test_replay_no_reply_is_capped_and_not_an_evidence_check():
    reply = ('{"brand_fit_score": 8, "cross_brand_distinct": 8, "mission_evidence": "", '
             '"mission_present": false, "feedback": "Frame each day around a private encounter."}')
    out, _ = _replay(reply)
    assert out.mission_present is False and out.evidence_verified is None and out.judge_score == 6.0


def test_replay_string_no_is_a_no_not_a_yes():
    """`bool("false")` is True: a string verdict used to count as a yes (then be overruled as unquoted)."""
    reply = ('{"brand_fit_score": 8, "cross_brand_distinct": 8, "mission_evidence": "", '
             '"mission_present": "false", "feedback": ""}')
    out, _ = _replay(reply)
    assert out.mission_present is False and out.evidence_verified is None


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("Yes", True), ("yes.", True), ("1", True), (1, True),
    ("false", False), ("no", False), ("", False), (None, False), (0, False), ("maybe", False),
])
def test_coerce_bool(value, expected):
    assert bf._coerce_bool(value) is expected


def test_quote_returned_as_a_list_is_read():
    out, _ = _judge(LLMResponse(content=json.dumps({
        "brand_fit_score": 8, "cross_brand_distinct": 7, "mission_present": True,
        "mission_evidence": ["", "We ride out at dawn, legs burning"], "feedback": "f"}),
        model_used="m", cost_usd=0.0, provider="bedrock"),
        require_evidence=True, evidence_source="Day 1\nWe ride out at dawn, legs burning, below.")
    assert out.mission_present is True and out.evidence_verified is True


def test_anchored_prompt_without_mission_keeps_the_old_mission_axis_and_shape():
    p = bf._build_judge_prompt(BARE, GEN, None, anchors=True)
    assert "SCORE ANCHORS" in p
    assert "MISSION TEST" not in p and "mission_evidence" not in p
    assert "mission-hook" in p                     # the A1/generic axis


# ── _judge: required evidence ──────────────────────────────────────────────────────────────────

def _resp(mission=True, evidence=None, fit=8):
    body = {"brand_fit_score": fit, "cross_brand_distinct": 7, "mission_present": mission, "feedback": "f"}
    if evidence is not None:
        body["mission_evidence"] = evidence
    return LLMResponse(content=json.dumps(body), model_used="satellite-gpt-5.6-luna", cost_usd=0.001,
                       provider="bedrock")


def _judge(resp, **kw):
    with patch.object(bf, "LLMClient") as client:
        client.return_value.generate.return_value = resp
        out = bf._judge("s", "u", mission_absent_cap=6.0, stage="t2_judge", **kw)
    return out, client.return_value.generate.call_args.args[0]


def test_unquoted_yes_does_not_count_when_evidence_is_required():
    out, _ = _judge(_resp(True, ""), require_evidence=True)
    assert out.mission_present is False and out.judge_score == 6.0 and out.mission_evidence == ""


def test_quoted_yes_counts_and_keeps_the_quote():
    out, _ = _judge(_resp(True, "We ride out at dawn."), require_evidence=True)
    assert out.mission_present is True and out.mission_evidence == "We ride out at dawn."


def test_without_the_requirement_an_unquoted_yes_still_counts():
    out, req = _judge(_resp(True))
    assert out.mission_present is True and out.mission_evidence == ""
    assert req.model_tier is None and "model_tier" not in req.model_fields_set


def test_quote_found_in_the_source_is_verified():
    src = "Day 2 — Hills\nWe ride out at dawn, legs **burning**, the valley opening below us."
    out, _ = _judge(_resp(True, "\u201cWe ride out at dawn, legs burning\u2026\u201d"), require_evidence=True,
                    evidence_source=src)
    assert out.mission_present is True and out.evidence_verified is True and out.judge_score == 7.0


def test_paraphrased_quote_is_overruled_and_recorded():
    src = "Day 2 — Hills\nWe ride out at dawn, legs burning, the valley opening below us."
    out, _ = _judge(_resp(True, "At sunrise we cycle out with burning legs."), require_evidence=True,
                    evidence_source=src)
    assert out.mission_present is False and out.evidence_verified is False
    assert out.judge_score == 6.0 and out.mission_evidence == ""


def test_verified_means_the_quote_exists_not_that_it_shows_the_idea():
    """Design limit (S236 review): evidence_in_source is an existence check. A logistics line copied
    from the digest verifies even though it says nothing about the core idea, so a verified yes is the
    judge's un-anchored mission verdict plus "the quote is not invented"."""
    src = "Day 1 — Arrival\nYour private guide meets you at the airport and drives you to the hotel."
    out, _ = _judge(_resp(True, "Your private guide meets you at the airport"), require_evidence=True,
                    evidence_source=src)
    assert out.mission_present is True and out.evidence_verified is True


def test_unquoted_yes_is_recorded_as_unverified():
    out, _ = _judge(_resp(True, ""), require_evidence=True, evidence_source="anything at all here")
    assert out.evidence_verified is False


def test_a_plain_no_is_not_an_evidence_check():
    out, _ = _judge(_resp(False, ""), require_evidence=True, evidence_source="x")
    assert out.evidence_verified is None
    out, _ = _judge(_resp(True))
    assert out.evidence_verified is None


@pytest.mark.parametrize("quote,ok", [
    ("we ride out at dawn", True),                       # case and trailing punctuation ignored
    ("We  ride out\nat dawn.", True),                   # whitespace collapsed
    ("ride out at", False),                              # too short to be a sentence
    ("We ride out at noon", False),                      # not in the source
    ("the valley opening below us", False),              # cut off by the digest, never seen
])
def test_evidence_in_source(quote, ok):
    src = bf.itinerary_digest([{"day": 1, "title": "Hills", "body": "We ride out at dawn. " + "x" * 400
                                + " the valley opening below us."}])
    assert bf.evidence_in_source(quote, src) is ok


def test_evidence_is_dropped_when_mission_is_false():
    out, _ = _judge(_resp(False, "something"), require_evidence=True)
    assert out.mission_present is False and out.mission_evidence == ""


# ── score_brand_fit: anchors + model_override ──────────────────────────────────────────────────

def _res(fit=8.0, mission=True, evidence=""):
    return bf.BrandFitResult(brand_fit_score=fit, cross_brand_distinct=7.0, mission_present=mission,
                             feedback="f", judge_score=min(fit, 7.0), model_used="m", cost_usd=0.001,
                             input_tokens=1, output_tokens=1, stop_reason=None, account=None,
                             fallback_used=None, mission_evidence=evidence)


def test_anchors_build_the_anchored_prompt_and_require_evidence():
    with patch.object(bf, "_judge", return_value=_res()) as j, \
            patch.object(bf, "_seeded_model", return_value=True):
        bf.score_brand_fit(FULL, GEN, stage="t2_judge", mission="Movement and freedom.", anchors=True)
    assert "SCORE ANCHORS" in j.call_args.args[1]
    assert j.call_args.kwargs["require_evidence"] is True
    assert "We ride out at dawn" in j.call_args.kwargs["evidence_source"]   # the digest the judge saw
    assert "model_tier" not in j.call_args.kwargs


def test_anchors_without_mission_do_not_require_evidence():
    with patch.object(bf, "_judge", return_value=_res()) as j, \
            patch.object(bf, "_seeded_model", return_value=True):
        bf.score_brand_fit(BARE, GEN, stage="t2_judge", anchors=True)
    assert "require_evidence" not in j.call_args.kwargs and "evidence_source" not in j.call_args.kwargs


def test_anchors_with_median3_pass_the_requirement_to_every_call():
    with patch.object(bf, "_judge", return_value=_res(evidence="q")) as j:
        out = bf.score_brand_fit(FULL, GEN, stage="t2_judge", mission="m", anchors=True, repeat=3)
    assert j.call_count == 3
    assert all(c.kwargs["require_evidence"] is True for c in j.call_args_list)
    assert out.mission_evidence == "q"


def test_model_override_is_refused_on_a_production_stage():
    with pytest.raises(ValueError, match="adhoc_"):
        bf.score_brand_fit(FULL, GEN, stage="t2_judge", model_override="sonnet-5")


def test_model_override_on_an_adhoc_stage_reaches_the_request_as_model_tier():
    with patch.object(bf, "LLMClient") as client:
        client.return_value.generate.return_value = _resp(True, "q")
        bf.score_brand_fit(FULL, GEN, stage="adhoc_aa801", mission="m", anchors=True,
                           model_override="sonnet-5", repeat=3)
    reqs = [c.args[0] for c in client.return_value.generate.call_args_list]
    assert len(reqs) == 3
    assert all(r.model_tier == "sonnet-5" and r.stage == "adhoc_aa801" for r in reqs)


# ── judge_node flag ────────────────────────────────────────────────────────────────────────────

_T2 = {"is_tenant_rewrite": True, "tenant_id": "t-1", "tour": {"tour_id": "x"},
       "brand_core_idea": "Movement and freedom.", "brand_customer_mindset": "Wants to feel alive.",
       "brand_voice_examples": ["Energetic"]}


def _run(state, response):
    with patch.object(bf, "LLMClient") as client, patch.object(jn, "record_call_sync") as rec, \
            patch.object(bf, "_seeded_model", return_value=True):
        client.return_value.generate.return_value = response
        out = jn.judge_node(dict(state, generated=GEN, quality_score=9.75, feedback=""))
    return out, client.return_value.generate.call_args.args[0], rec.call_args.kwargs


def test_anchors_flag_default_off():
    assert jn.t2_judge_anchors() is False


@pytest.mark.parametrize("value,on", [("on", True), ("1", True), ("TRUE", True), ("off", False), ("x", False)])
def test_anchors_flag_values(monkeypatch, value, on):
    monkeypatch.setenv("T2_JUDGE_ANCHORS", value)
    assert jn.t2_judge_anchors() is on


def test_flag_off_t2_prompt_and_signal_unchanged():
    with patch.object(jn, "score_brand_fit", wraps=jn.score_brand_fit) as score:
        out, req, rec = _run(_T2, _resp(True))
    assert "anchors" not in score.call_args.kwargs
    assert "SCORE ANCHORS" not in req.user_prompt
    assert "judge_anchors" not in rec["quality_signal"]
    assert out["judge_mission_present"] is True


def test_flag_on_t2_uses_anchors_and_logs_the_quote(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_ANCHORS", "on")
    out, req, rec = _run(_T2, _resp(True, "We ride out at dawn."))
    assert "SCORE ANCHORS" in req.user_prompt and "MISSION TEST" in req.user_prompt
    assert rec["quality_signal"]["judge_anchors"] is True
    assert rec["quality_signal"]["mission_evidence"] == "We ride out at dawn."
    assert rec["quality_signal"]["evidence_verified"] is True
    assert out["judge_score"] == 8.0


def test_flag_on_invented_quote_is_capped(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_ANCHORS", "on")
    out, _, rec = _run(_T2, _resp(True, "Every day is about movement and freedom."))
    assert out["judge_mission_present"] is False and out["judge_score"] == 6.0
    assert rec["quality_signal"]["evidence_verified"] is False


def test_flag_on_unquoted_mission_is_capped(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_ANCHORS", "on")
    out, _, rec = _run(_T2, _resp(True, ""))
    assert out["judge_mission_present"] is False and out["judge_score"] == 6.0
    assert rec["quality_signal"]["mission_evidence"] == ""


def test_median3_aggregates_evidence_verified():
    yes = dataclasses.replace(_res(evidence="q"), evidence_verified=True)
    overruled = dataclasses.replace(_res(mission=False), evidence_verified=False)
    plain_no = _res(mission=False)
    assert bf.aggregate_runs([yes, yes, overruled], mission_absent_cap=6.0).evidence_verified is True
    agg = bf.aggregate_runs([yes, overruled, overruled], mission_absent_cap=6.0)
    assert agg.mission_present is False and agg.evidence_verified is False
    assert [r.get("evidence_verified") for r in agg.runs] == [True, False, False]
    assert bf.aggregate_runs([plain_no] * 3, mission_absent_cap=6.0).evidence_verified is None


def test_flag_on_does_not_touch_a1(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_ANCHORS", "on")
    out, req, rec = _run(dict(_T2, is_tenant_rewrite=False), _resp(True))
    assert "SCORE ANCHORS" not in req.user_prompt
    assert "judge_anchors" not in rec["quality_signal"]
