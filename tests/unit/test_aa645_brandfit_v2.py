"""AA-645 (S249) — A1 brand-fit rubric v2 (services/content_generation/brand_fit_v2.py), its wiring in
brand_fit.score_brand_fit and the A1 rubric / gate selection in judge_node."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.content_generation import brand_fit as bf
from services.content_generation import brand_fit_v2 as v2
from services.content_generation import judge_node as jn

SOURCE = {
    "name": "Vang Vieng Kayak and Caves",
    "summary": "A day of kayaking on the Nam Song river near Vang Vieng.",
    "description": "Group size: 2-8",
    "highlights": ["Tham Nam water cave", "Nam Song river"],
    "itineraries": "Day 1\nPick-up from your hotel in Vang Vieng. Drive 13 km to the Tham Nam cave and float through "
                   "it on an inner tube. Lunch (L). Kayak 10 km down the Nam Song back to town.",
    "country": "Laos", "duration": "1 day", "inclusions": "Lunch, guide, kayak", "exclusions": "Drinks",
}
PAGE = {
    "name": "Vang Vieng Kayak and Caves — 1 Day",
    "subtitle": "Tube through Tham Nam cave and kayak the Nam Song back to Vang Vieng.",
    "summary": "A one-day trip from Vang Vieng: a 13 km drive to Tham Nam, a tube float through the water cave and a "
               "10 km kayak down the Nam Song.",
    "highlights": ["Tham Nam water cave", "Nam Song river by kayak"],
    "itineraries": "Day 1 — Tham Nam cave and the Nam Song\nPick-up in Vang Vieng and a 13 km drive to Tham Nam. "
                   "Float through the cave on an inner tube, then kayak 10 km downstream, past limestone karsts at "
                   "sunset with a cold beer at a riverside bar. Distance: 13 km drive, 10 km kayak. Meals: lunch.",
    "seo_title": "Vang Vieng Kayak and Caves — 1 Day",
    "seo_meta": "One day from Vang Vieng: tube through Tham Nam water cave, then kayak 10 km down the Nam Song.",
}


def _reply(score=8, distinct=8, claims=(), feedback=""):
    return {"unsupported_specifics": [{"quote": q, "why": "not in the source"} for q in claims],
            "brand_fit_score": score, "cross_brand_distinct": distinct, "feedback": feedback}


# ── version pin ───────────────────────────────────────────────────────────────────────────────────────────


def test_rubric_fingerprint_is_pinned_bump_the_version_with_it():
    # Changing the v2 prompt or a cap changes the fingerprint: bump RUBRIC_V2_VERSION and update the pin, so a
    # stored score (metadata.judge.rubric, llm_call_log quality_signal.rubric) always names the text that made it.
    assert v2.fingerprint() == v2.RUBRIC_V2_FINGERPRINT
    assert v2.RUBRIC_V2_VERSION == "a1-voice-v2/1"


# ── prompt ────────────────────────────────────────────────────────────────────────────────────────────────


def test_prompt_shows_the_source_and_every_page_field():
    p = v2.build_user_prompt(PAGE, v2.source_sheet(SOURCE))
    assert "SOURCE (the supplier's text" in p and "Tham Nam" in p and "Group size: 2-8" in p
    for f in ("NAME:", "SUBTITLE:", "SUMMARY:", "HIGHLIGHTS:", "ITINERARIES", "SEO_TITLE:", "SEO_META:"):
        assert f in p
    assert "riverside bar" in p   # the day body is shown far enough for claims, not cut at 350


def test_prompt_never_asks_for_brand_colour_and_protects_plain_text():
    text = (v2.SYSTEM_V2 + v2.build_user_prompt(PAGE, "x")).lower()
    assert "neutral wording that stays close to the source is not \"generic\"" in text
    assert "never ask for added facts, colour, emotion or a brand angle" in text
    assert "a faithful, clean page" in text and "thin source" in text
    # none of the v1 brand-angle framing
    assert "brand profile" not in text and "core idea" not in text and "mission" not in text
    assert "generic travel copy that would fit any brand must score low" not in text


def test_prompt_has_no_brand_profile_input_even_when_state_has_one():
    p = v2.build_user_prompt(PAGE, v2.source_sheet(SOURCE))
    assert "Adventure Asia" in p          # only in the house-style "never name" rule
    assert p.count("Adventure Asia") == 1


def test_source_sheet_is_cut_to_the_cap():
    big = {**SOURCE, "itineraries": "Day 1\n" + ("Walk to the village. " * 2000)}
    sheet = v2.source_sheet(big)
    assert len(sheet) <= v2.MAX_SOURCE_CHARS + 60 and sheet.endswith("[source shortened for length]")
    assert v2.source_sheet(None) == "" and v2.source_sheet({}) == ""


def test_source_sheet_reads_the_agency_fields():
    sheet = v2.source_sheet({**SOURCE, "description": ""}, {"group_size": "max 6", "best_time_to_go": None})
    assert "Group size: max 6" in sheet


# ── reply: claims, caps, leaks ────────────────────────────────────────────────────────────────────────────


def test_clean_reply_keeps_the_score():
    out = v2.apply_reply(_reply(8, 9), PAGE, v2.source_text(SOURCE))
    assert (out.brand_fit, out.raw_brand_fit, out.distinct, out.caps, out.unsupported) == (8.0, 8.0, 9.0, (), ())


def test_one_verified_unsupported_specific_caps_at_6():
    out = v2.apply_reply(_reply(9, claims=["past limestone karsts at sunset"]), PAGE, v2.source_text(SOURCE))
    assert out.raw_brand_fit == 9.0 and out.brand_fit == v2.CAP_UNSUPPORTED_ONE and out.caps == ("unsupported",)
    assert out.unsupported[0]["quote"] == "past limestone karsts at sunset"


def test_three_unsupported_specifics_cap_at_4():
    claims = ["past limestone karsts at sunset", "with a cold beer", "at a riverside bar"]
    out = v2.apply_reply(_reply(8, claims=claims), PAGE, v2.source_text(SOURCE))
    assert out.brand_fit == v2.CAP_UNSUPPORTED_MANY and out.caps == ("unsupported_many",)


def test_a_quote_that_is_not_in_the_page_is_dropped_not_capped():
    out = v2.apply_reply(_reply(8, claims=["a hot-air balloon ride over the valley", "x"]), PAGE,
                         v2.source_text(SOURCE))
    assert out.brand_fit == 8.0 and out.unsupported == () and out.unverified == 2


def test_quote_match_folds_case_quotes_and_trailing_ellipsis():
    page = v2.page_text(PAGE)
    assert v2.quote_in_page("“Float through the cave on an inner tube…”", page)
    assert v2.quote_in_page("FLOAT THROUGH THE CAVE", page)
    assert not v2.quote_in_page("cave", page)                   # one word cannot name a claim
    assert not v2.quote_in_page("float through the grotto", page)


def test_score_never_goes_up_and_is_clamped():
    out = v2.apply_reply(_reply(5, claims=["with a cold beer"]), PAGE, v2.source_text(SOURCE))
    assert out.brand_fit == 5.0                                  # a cap never lifts a lower score
    assert v2.apply_reply(_reply(14), PAGE, "").brand_fit == 10.0
    assert v2.apply_reply({"brand_fit_score": "n/a"}, PAGE, "").brand_fit == 0.0


def test_brand_leak_caps_at_5_only_when_the_source_lacks_it():
    leaky = {**PAGE, "summary": PAGE["summary"] + " Adventure Asia guides lead the day. Design this journey."}
    out = v2.apply_reply(_reply(9), leaky, v2.source_text(SOURCE))
    assert out.leaks == ("Adventure Asia", "Design This Journey") and out.brand_fit == v2.CAP_BRAND_LEAK
    assert out.caps == ("brand_leak",)
    src = {**SOURCE, "description": "Join Adventure Asia on the river. Design This Journey with us."}
    assert v2.apply_reply(_reply(9), leaky, v2.source_text(src)).leaks == ()


def test_leak_is_whole_word():
    assert v2.brand_leaks("Southeast Asian food", "") == []
    assert v2.brand_leaks("adventure   asia", "") == ["Adventure Asia"]


def test_unsupported_and_leak_caps_stack():
    leaky = {**PAGE, "summary": PAGE["summary"] + " With Adventure Asia."}
    out = v2.apply_reply(_reply(9, claims=["with a cold beer"]), leaky, v2.source_text(SOURCE))
    assert out.brand_fit == v2.CAP_BRAND_LEAK and out.caps == ("unsupported", "brand_leak")


def test_claims_accept_strings_and_bad_shapes():
    out = v2.apply_reply({"unsupported_specifics": ["with a cold beer", None, {"quote": ""}], "brand_fit_score": 8},
                         PAGE, "")
    assert len(out.unsupported) == 1 and out.unverified == 0
    assert v2.apply_reply({"unsupported_specifics": "none", "brand_fit_score": 8}, PAGE, "").unsupported == ()


# ── score_brand_fit(rubric="v2") ──────────────────────────────────────────────────────────────────────────


class _Resp(SimpleNamespace):
    pass


def _llm(*replies):
    resps = [_Resp(content=json.dumps(r), model_used="satellite-gpt-5.6-luna", cost_usd=0.004, input_tokens=3000,
                   output_tokens=200, stop_reason="end_turn", satellite_account="acc3", fallback_used=False)
             for r in replies]
    calls = []

    class FakeClient:
        def generate(self, request):
            calls.append(request)
            return resps[len(calls) - 1]
    return FakeClient, calls


def _unseeded(_key):
    return SimpleNamespace(supports_temperature=False)


def test_score_brand_fit_v2_end_to_end_single_call():
    client, calls = _llm(_reply(9, 8, claims=["with a cold beer", "at a riverside bar", "at sunset with a cold"],
                                feedback="Drop the beer and the sunset."))
    with patch.object(bf, "LLMClient", client), \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = bf.score_brand_fit({"brand_core_idea": "x"}, PAGE, stage="s1_judge", rubric="v2", source=SOURCE)
    assert len(calls) == 1          # capped to 4: outside the 6..8 band, no repeat
    req = calls[0]
    assert req.stage == "s1_judge" and req.system_prompt == v2.SYSTEM_V2 and "Tham Nam" in req.user_prompt
    assert req.temperature == bf._JUDGE_TEMPERATURE and req.seed == bf._JUDGE_SEED
    assert out.rubric == "v2" and out.raw_brand_fit == 9.0 and out.brand_fit_score == v2.CAP_UNSUPPORTED_MANY
    assert out.caps == ("unsupported_many",) and len(out.unsupported) == 3
    assert out.feedback == "Drop the beer and the sunset." and out.judge_score == 4.0


def test_score_brand_fit_v2_median_of_three_on_capped_scores():
    client, calls = _llm(_reply(9, claims=["with a cold beer"]), _reply(8), _reply(9, claims=["with a cold beer"]))
    with patch.object(bf, "LLMClient", client), \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = bf.score_brand_fit({}, PAGE, rubric="v2", source=SOURCE)
    assert len(calls) == 3                         # first score 6 (capped) lands in 6..8 -> median of 3
    assert out.brand_fit_score == 6.0              # median(6, 8, 6)
    assert out.raw_brand_fit == 9.0                # median of the raw scores (9, 8, 9)
    assert out.rubric == "v2" and out.caps == ("unsupported",)
    assert out.cost_usd == pytest.approx(0.012)
    assert [r["caps"] for r in out.runs] == [["unsupported"], [], ["unsupported"]]


def test_score_brand_fit_v2_clear_score_is_one_call_and_never_tie_capped_by_mission():
    client, calls = _llm(_reply(9, 9))
    with patch.object(bf, "LLMClient", client), \
            patch("shared.llm_client.catalog.get_model_sync", side_effect=_unseeded):
        out = bf.score_brand_fit({}, PAGE, rubric="v2", source=SOURCE, mission_absent_cap=6.0)
    assert len(calls) == 1 and out.brand_fit_score == 9.0 and out.mission_present is True and out.judge_score == 9.0


def test_score_brand_fit_v2_guards():
    with pytest.raises(ValueError, match="source"):
        bf.score_brand_fit({}, PAGE, rubric="v2")
    with pytest.raises(ValueError, match="mission"):
        bf.score_brand_fit({}, PAGE, rubric="v2", source=SOURCE, mission="m")
    with pytest.raises(ValueError, match="mission"):
        bf.score_brand_fit({}, PAGE, rubric="v2", source=SOURCE, anchors=True)
    with pytest.raises(ValueError, match="rubric"):
        bf.score_brand_fit({}, PAGE, rubric="v3", source=SOURCE)


def test_v2_malformed_reply_is_billed_and_raises():
    class Bad:
        def generate(self, request):
            return _Resp(content="not json", model_used="m", cost_usd=0.004, input_tokens=1, output_tokens=1,
                         stop_reason="end_turn", satellite_account="acc3", fallback_used=False)
    with patch.object(bf, "LLMClient", Bad), pytest.raises(Exception) as e:
        bf.score_brand_fit({}, PAGE, rubric="v2", source=SOURCE)
    assert bf.billed_cost(e.value) == pytest.approx(0.004)


def test_v1_default_prompt_is_unchanged():
    client, calls = _llm({"brand_fit_score": 9, "cross_brand_distinct": 9, "mission_present": True, "feedback": ""})
    with patch.object(bf, "LLMClient", client), patch("shared.llm_client.catalog.get_model_sync", return_value=None):
        out = bf.score_brand_fit({"brand_core_idea": "x"}, PAGE)
    assert calls[0].system_prompt == bf.JUDGE_SYSTEM
    assert calls[0].user_prompt == bf._build_judge_prompt({"brand_core_idea": "x"}, PAGE)
    assert out.rubric == "v1" and out.caps == () and out.raw_brand_fit is None


def test_debate_and_t2_callers_never_pass_a_rubric():
    import inspect

    from services.acp_shared import debate
    assert "rubric" not in inspect.getsource(debate)
    t2 = inspect.getsource(jn.judge_node).split("else:")[0]   # the is_t2 branch
    assert "rubric" not in t2


# ── judge_node: A1 rubric + gate selection ────────────────────────────────────────────────────────────────


def _result(score, rubric="v1", caps=(), unsupported=()):
    return bf.BrandFitResult(brand_fit_score=score, cross_brand_distinct=8.0, mission_present=True,
                             feedback="drop the sunset", judge_score=score, model_used="judge", cost_usd=0.002,
                             input_tokens=10, output_tokens=5, stop_reason="end_turn", account=None,
                             fallback_used=False, rubric=rubric, caps=tuple(caps), unsupported=tuple(unsupported),
                             raw_brand_fit=9.0 if rubric == "v2" else None)


BASE = {"generated": PAGE, "quality_score": 9.0, "feedback": "", "cost_usd": 0.0, "brand_core_idea": "x",
        "brand_customer_mindset": "y", "brand_voice_examples": ["z"], "tour": SOURCE}


def _run(state, result, env=None):
    with patch.dict("os.environ", env or {}, clear=False), \
            patch.object(jn, "score_brand_fit", return_value=result) as score, \
            patch.object(jn, "_write_judge_rows") as rows, patch.object(jn, "_tie_break",
                                                                       return_value=(7.0, {})) as tie:
        out = jn.judge_node({**BASE, **state})
    return out, rows.call_args.args[0][0]["quality_signal"], score.call_args.kwargs, tie


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(jn.A1_BRAND_FIT_RUBRIC_ENV, raising=False)
    monkeypatch.delenv(jn.A1_V2_BRAND_FIT_GATE_ENV, raising=False)


def test_auto_scores_a_v2_draft_with_v2_report_only():
    res = _result(5.0, "v2", caps=("unsupported",), unsupported=({"quote": "at sunset", "why": "-"},))
    out, signal, kw, tie = _run({"writer_version": "v2", "tour_agency": {"group_size": "6"}}, res)
    assert kw["rubric"] == "v2" and kw["source"] is SOURCE and kw["tour_agency"] == {"group_size": "6"}
    assert signal["gate"] == jn.A1_REPORT_ONLY_GATE and out["quality_score"] == 9.0 and out["feedback"] == ""
    assert signal["rubric"] == v2.RUBRIC_V2_VERSION and signal["unsupported"] == 1 and signal["caps"] == ["unsupported"]
    assert signal["raw_brand_fit"] == 9.0 and signal["unsupported_quotes"] == ["at sunset"]
    assert out["judge_rubric"] == v2.RUBRIC_V2_VERSION and out["judge_unsupported"][0]["quote"] == "at sunset"
    tie.assert_not_called()


def test_auto_keeps_v1_for_todays_writer_and_rows_unchanged():
    out, signal, kw, _ = _run({}, _result(5.0))
    assert "rubric" not in kw and signal["gate"] == "brand_fit" and out["quality_score"] == 5.0
    assert "rubric" not in signal and "judge_rubric" not in out


def test_enforce_gates_a_v2_draft_on_its_v2_score_without_jev_tiebreak():
    out, signal, _, tie = _run({"writer_version": "v2"}, _result(6.8, "v2"),
                               {jn.A1_V2_BRAND_FIT_GATE_ENV: "enforce"})
    assert signal["gate"] == jn.A1_V2_ENFORCED_GATE and out["quality_score"] == 6.8
    assert "drop the sunset" in out["feedback"] and signal["passed"] is False
    tie.assert_not_called()   # in the 6.5-7.5 band, but the a1_brand_fit Question was set up on v1 scores


def test_enforce_never_gates_a_v2_draft_scored_with_v1():
    out, signal, kw, _ = _run({"writer_version": "v2"}, _result(5.0),
                              {jn.A1_V2_BRAND_FIT_GATE_ENV: "enforce", jn.A1_BRAND_FIT_RUBRIC_ENV: "v1"})
    assert "rubric" not in kw and signal["gate"] == jn.A1_REPORT_ONLY_GATE and out["quality_score"] == 9.0


def test_forced_v2_on_a_v1_draft_gates_as_before():
    out, signal, kw, _ = _run({}, _result(5.0, "v2"), {jn.A1_BRAND_FIT_RUBRIC_ENV: "v2"})
    assert kw["rubric"] == "v2" and signal["gate"] == "brand_fit" and out["quality_score"] == 5.0


def test_v2_without_a_source_falls_back_to_v1():
    out, signal, kw, _ = _run({"writer_version": "v2", "tour": None}, _result(5.0))
    assert "rubric" not in kw and signal["gate"] == jn.A1_REPORT_ONLY_GATE


def test_unknown_rubric_setting_is_auto():
    with patch.dict("os.environ", {jn.A1_BRAND_FIT_RUBRIC_ENV: "v9"}):
        assert jn.a1_brand_fit_rubric({**BASE, "writer_version": "v2"}) == "v2"
        assert jn.a1_brand_fit_rubric(BASE) == "v1"


def test_tenant_rewrite_never_gets_v2():
    with patch.object(jn, "t2_mission", return_value=""):
        out, signal, kw, _ = _run({"writer_version": "v2", "is_tenant_rewrite": True, "tenant_id": "t"},
                                  _result(5.0), {jn.A1_BRAND_FIT_RUBRIC_ENV: "v2"})
    assert "rubric" not in kw and "rubric" not in signal


def test_metadata_judge_records_the_rubric():
    from api.routers.admin_pipeline import _build_generated_metadata
    kw = dict(brand_rule_id=None, brand_name="", seo_mode="dataforseo", model_used="m", llm_cost_usd=0.0,
              dataforseo_used=False)
    result = {"judge_brand_fit": 6.0, "judge_score": 6.0, "judge_rubric": v2.RUBRIC_V2_VERSION,
              "judge_unsupported": [{"quote": "q", "why": "w"}], "judge_caps": ["unsupported"]}
    meta = _build_generated_metadata(result, **kw)
    assert meta["judge"]["rubric"] == v2.RUBRIC_V2_VERSION and meta["judge"]["caps"] == ["unsupported"]
    assert meta["judge"]["unsupported"] == [{"quote": "q", "why": "w"}]
    assert "rubric" not in _build_generated_metadata({"judge_brand_fit": 8.0}, **kw)["judge"]
