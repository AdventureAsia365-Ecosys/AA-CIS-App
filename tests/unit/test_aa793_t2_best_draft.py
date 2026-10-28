"""AA-793 — T2 best draft wins, targeted repair, background-fact allowance in T3 grounding.

S235 (report 12 §2.2-2.3): the T2 job saved the last draft, not the best (three versions lost a 7.0
draft); every repair round was a whole-tour rewrite; and T3 flagged place background ("1954 battle",
"17th-century dzong", "3,120 m") as invented, forcing 12 of 14 versions through 2 full rewrites.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_produce import tenant_pipeline as tp
from services.acp_produce.t3_background import is_background_number
from services.content_generation import t2_repair

YEAR = 2026


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    for env in (t2_repair.T2_BEST_DRAFT_ENV, t2_repair.T2_TARGETED_REPAIR_ENV,
                t2_repair.T3_BACKGROUND_FACTS_ENV):
        monkeypatch.delenv(env, raising=False)


# ── switches ──────────────────────────────────────────────────────────────────

def test_defaults_best_draft_and_targeted_on_background_off():
    assert t2_repair.best_draft_enabled() is True
    assert t2_repair.targeted_repair_enabled() is True
    assert t2_repair.background_facts_enabled() is False
    assert t2_repair.t2_drafts_graph_enabled() is True


@pytest.mark.parametrize("raw,expected", [("off", False), ("0", False), ("false", False), ("NO", False),
                                          ("on", True), ("1", True), ("junk", True), ("", True)])
def test_default_on_switch_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("T2_BEST_DRAFT", raw)
    assert t2_repair.best_draft_enabled() is expected


@pytest.mark.parametrize("raw,expected", [("on", True), ("true", True), ("yes", True), ("1", True),
                                          ("off", False), ("junk", False), ("", False)])
def test_background_switch_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("T3_BACKGROUND_FACTS", raw)
    assert t2_repair.background_facts_enabled() is expected


def test_drafts_graph_off_only_when_both_switches_off(monkeypatch):
    monkeypatch.setenv("T2_BEST_DRAFT", "off")
    assert t2_repair.t2_drafts_graph_enabled() is True
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    assert t2_repair.t2_drafts_graph_enabled() is False


# ── background-fact allowance (F-d) ───────────────────────────────────────────

@pytest.mark.parametrize("sentence,token", [
    ("Walk the valley of Dien Bien Phu, site of the 1954 battle.", "1954"),        # S235 case
    ("Visit the 17th-century Punakha Dzong at the river fork.", "17"),             # S235 case
    ("Cross Dochula Pass (3,120 m) on the way to Punakha.", "3120"),               # S235 case
    ("The monastery sits at 3,120 m above sea level.", "3120"),
    ("Cross Larkya La at 5,106 m before the long descent.", "5106"),
    ("Murals from the 17th and 18th centuries line the hall.", "17"),
    ("Murals from the 17th and 18th centuries line the hall.", "18"),
    ("The fort, built in 1637–38, guards the old trade road.", "1638"),            # range end year
    ("A temple complex rebuilt in the 1630s.", "1630s"),
    ("Ruins dating to 500 BC stand by the river.", "500"),
    ("The 4,000-metre-high pass opens onto the plateau.", "4000"),
    ("The pass rises to 3,120 m above sea level.", "3120"),
])
def test_place_background_is_allowed(sentence, token):
    assert is_background_number(token, sentence, year_now=YEAR)


@pytest.mark.parametrize("sentence,token", [
    ("A 30-minute flight to Lukla.", "30"),                                  # duration
    ("Drive 260 km to Hue.", "260"),                                          # distance
    ("Departures run every spring from 2027.", "2027"),                      # future date = promise
    ("The festival returns in 2026.", "2026"),                               # this year = promise
    ("Since 2010 our guides have led this trek.", "2010"),                   # operator claim
    ("We have run this route since 1998.", "1998"),
    ("A 22-metre-long reclining Buddha fills the hall.", "22"),              # length, not elevation
    ("Gain 1,200 m on the steep climb to camp.", "1200"),                    # gain, no elevation cue
    ("The 500 m path leads to the gate.", "500"),
    ("Prices start at USD 1950 per person.", "1950"),                        # price
    ("Up to 1200 people gather in the courtyard.", "1200"),                  # count
    ("Lunch is served at 1230 hrs by the lake.", "1230"),                    # clock time
    ("Day 3 ends with a 3rd night in the lodge.", "3"),
    # review: elevation gains are tour facts, not the height of a place
    ("The trail climbs 1,200 m to Dochula Pass.", "1200"),
    ("Hike up 1,200 m to the monastery perched on the cliff.", "1200"),
    ("A 1,500 m ascent takes you to the high plateau.", "1500"),
    ("Gain 2,000 m of elevation over the trek.", "2000"),
    # review: accommodation / service sentences are claims about THIS tour (fidelity rule B)
    ("Since 1998 the lodge has been family-run.", "1998"),
    ("Sleep at 4,460 m near the pass.", "4460"),
    ("Stay in a 17th-century palace hotel.", "17"),
    ("Camp at 4,460 m below the summit.", "4460"),
    # adversarial review: counts, prices and distances that the first version let through as
    # "years" or "elevations"
    ("Home to 1,500 monks, the dzong dominates the valley.", "1500"),      # comma count
    ("Over 1,108 prayer wheels line the courtyard.", "1108"),
    ("The temple holds 1500 statues of the Buddha.", "1500"),              # bare count, no year cue
    ("The dzong is home to 1500 monks.", "1500"),                          # year cue, but a plural after
    ("Entry is 1200 ngultrum at the high gate.", "1200"),                  # local-currency price
    ("Tickets cost 1500 baht in the old town.", "1500"),
    ("A short walk (1,500 m) leads to the temple.", "1500"),               # distance in brackets
    ("A 1,500 m walk takes you to the summit of the hill.", "1500"),       # distance, cue elsewhere
    ("Walk 2,000 m along the high ridge.", "2000"),
    ("The trail runs 1,200 m to the pass.", "1200"),
    ("Trek through 1,200 m of forest to the peak.", "1200"),
    ("Raft 1,500 m of rapids below the peak.", "1500"),                    # "N m of ..." is a length
    ("The ride covers 1,500 m of high plateau.", "1500"),
])
def test_promises_and_quantities_are_not_background(sentence, token):
    assert not is_background_number(token, sentence, year_now=YEAR)


def test_restore_best_also_restores_the_drafts_call_metadata():
    best = {"generated": {"name": "A"}, "quality_score": 6.5, "judge_tiebreak": {"before": 6.4},
            "fallback_used": False, "satellite_account": "acc3", "retry_count": 0}
    best_snap = t2_repair.snapshot(best)
    last = {"generated": {"name": "B"}, "quality_score": 5.0, "judge_tiebreak": None,
            "fallback_used": True, "satellite_account": "acc3", "retry_count": 2, "best_draft": best_snap}
    out = t2_repair.restore_best_node(last)
    assert out["generated"] == {"name": "A"} and out["judge_tiebreak"] == {"before": 6.4}
    assert out["fallback_used"] is False and out["satellite_account"] == "acc3"
    assert out["retry_count"] == 2   # run counters keep counting the whole run


def test_every_occurrence_must_qualify():
    # "1954" as a year and as a height (no elevation cue) in one sentence: not cleared
    assert not is_background_number("1954", "In 1954 a 1954 m tower stood here.", year_now=YEAR)


def test_grounding_check_unchanged_with_the_allowance_off():
    rewritten = {"summary": "Visit the 17th-century dzong, site of the 1954 battle, in 30 minutes."}
    out = tp._t3_grounding_check(rewritten, ["Visit the dzong."], background=False)
    assert out == [{"field": "summary", "sentence": rewritten["summary"],
                    "novel_numbers": ["17", "1954", "30"]}]


def test_grounding_check_keeps_only_the_promise_with_the_allowance_on():
    rewritten = {"summary": "Visit the 17th-century dzong, site of the 1954 battle, in 30 minutes."}
    cleared = []
    out = tp._t3_grounding_check(rewritten, ["Visit the dzong."], background=True, cleared=cleared)
    assert out[0]["novel_numbers"] == ["30"]
    assert cleared == [{"field": "summary", "sentence": rewritten["summary"],
                        "background_numbers": ["17", "1954"]}]


def test_grounding_check_reads_the_env_switch(monkeypatch):
    rewritten = {"summary": "Cross Dochula Pass (3,120 m) today."}
    assert tp._t3_grounding_check(rewritten, ["Cross Dochula Pass."])          # default off: flagged
    monkeypatch.setenv("T3_BACKGROUND_FACTS", "on")
    assert tp._t3_grounding_check(rewritten, ["Cross Dochula Pass."]) == []


# ── graph nodes: best draft wins + revise the best draft (F-c) ───────────────

def _state(score, name, attempt=0, **kw):
    return {"generated": {"name": name}, "quality_score": score, "retry_count": attempt,
            "feedback": f"fb-{name}", "judge_feedback": f"judge-{name}", "judge_score": score,
            "is_tenant_rewrite": True, **kw}


def test_track_best_keeps_the_highest_score_and_logs_every_attempt():
    s = t2_repair.track_best_node(_state(6.0, "a"))
    s = t2_repair.track_best_node({**s, **_state(5.0, "b", 1)})
    s = t2_repair.track_best_node({**s, **_state(6.0, "c", 2)})     # a tie keeps the earlier draft
    assert s["best_draft"]["generated"] == {"name": "a"} and s["best_draft"]["attempt"] == 0
    assert [d["score"] for d in s["draft_scores"]] == [6.0, 5.0, 6.0]


def test_track_best_never_keeps_an_empty_draft():
    s = t2_repair.track_best_node({**_state(0.0, "x"), "generated": {}})
    assert "best_draft" not in s


def test_restore_best_puts_the_better_earlier_draft_back():
    s = t2_repair.track_best_node(_state(6.0, "a"))
    s = t2_repair.track_best_node({**s, **_state(4.0, "b", 2), "error": "x"})
    out = t2_repair.restore_best_node(s)
    assert out["generated"] == {"name": "a"} and out["quality_score"] == 6.0
    assert out["judge_feedback"] == "judge-a" and out["best_draft_restored"] is True
    assert out["retry_count"] == 2 and out["error"] == ""        # the run's counters are kept


def test_restore_best_is_a_noop_when_last_is_best_or_switch_off(monkeypatch):
    s = t2_repair.track_best_node(_state(4.0, "a"))
    s = t2_repair.track_best_node({**s, **_state(6.0, "b", 1)})
    assert t2_repair.restore_best_node(s)["generated"] == {"name": "b"}
    s2 = t2_repair.track_best_node({**s, **_state(3.0, "c", 2)})
    monkeypatch.setenv("T2_BEST_DRAFT", "off")
    assert t2_repair.restore_best_node(s2)["generated"] == {"name": "c"}


def test_retry_revises_the_best_draft_with_its_feedback():
    s = t2_repair.track_best_node(_state(6.0, "a"))
    s = t2_repair.track_best_node({**s, **_state(4.0, "b", 1)})
    out = t2_repair.increment_retry_t2(s)
    assert out["retry_count"] == 2
    assert out["previous_draft"] == {"name": "a"} and out["feedback"] == "fb-a"


def test_retry_revises_the_last_draft_when_best_draft_is_off(monkeypatch):
    monkeypatch.setenv("T2_BEST_DRAFT", "off")
    s = t2_repair.track_best_node(_state(6.0, "a"))
    s = t2_repair.track_best_node({**s, **_state(4.0, "b", 1)})
    assert t2_repair.increment_retry_t2(s)["previous_draft"] == {"name": "b"}


def test_retry_is_a_plain_reroll_when_targeted_is_off_or_a1(monkeypatch):
    s = t2_repair.track_best_node(_state(6.0, "a"))
    assert "previous_draft" not in t2_repair.increment_retry_t2({**s, "is_tenant_rewrite": False})
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    assert "previous_draft" not in t2_repair.increment_retry_t2(s)


def test_revise_block_shows_only_writer_fields():
    block = t2_repair.revise_block({"name": "N", "summary": "S", "seo_keywords_used": ["k"], "subtitle": ""})
    assert "YOUR PREVIOUS DRAFT" in block and '"summary": "S"' in block
    assert "seo_keywords_used" not in block and "subtitle" not in block
    assert t2_repair.revise_block(None) == "" and t2_repair.revise_block({}) == ""


def _capture_prompt(state):
    from services.content_generation import graph
    captured = {}

    class FakeClient:
        def generate(self, request):
            captured["user"] = request.user_prompt
            raise RuntimeError("stop here")

    with patch.object(graph, "LLMClient", return_value=FakeClient()):
        graph.generate_node({"tour": {"name": "T"}, "seo": {}, "few_shots": [], "retry_count": 1,
                             "generate_stage": "t2_generate", "feedback": "make it warmer", **state})
    return captured["user"]


def test_generate_node_revises_the_previous_draft_on_a_t2_retry():
    prompt = _capture_prompt({"is_tenant_rewrite": True, "previous_draft": {"name": "Draft A"}})
    assert "YOUR PREVIOUS DRAFT" in prompt and "Draft A" in prompt
    assert prompt.index("YOUR PREVIOUS DRAFT") < prompt.index("PREVIOUS ATTEMPT FEEDBACK")


def test_generate_node_never_revises_for_a1_or_with_targeted_off(monkeypatch):
    assert "YOUR PREVIOUS DRAFT" not in _capture_prompt({"previous_draft": {"name": "Draft A"}})
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    assert "YOUR PREVIOUS DRAFT" not in _capture_prompt(
        {"is_tenant_rewrite": True, "previous_draft": {"name": "Draft A"}})


def test_build_graph_variant_adds_draft_nodes_only_when_asked():
    from services.content_generation.graph import build_graph
    assert "track_best" not in build_graph().get_graph().nodes
    nodes = build_graph(t2_drafts=True).get_graph().nodes
    assert "track_best" in nodes and "restore_best" in nodes


@pytest.mark.asyncio
async def test_rewrite_tour_picks_the_t2_graph_and_seeds_previous_draft(monkeypatch):
    from api.routers import v1_pipeline
    seen, built = [], []

    class FakeGraph:
        async def astream(self, state, stream_mode):
            seen.append(dict(state))
            yield {"generate": {**state, "generated": {"name": "x"}, "draft_scores": [{"score": 5.0}]}}

    def fake_build(*args, **kwargs):
        built.append(kwargs)
        return FakeGraph()

    with patch.object(v1_pipeline, "build_graph", side_effect=fake_build):
        out = await v1_pipeline._rewrite_tour({"name": "T"}, idx=0, total=1, is_tenant_rewrite=True,
                                              previous_draft={"name": "prev"})
        await v1_pipeline._rewrite_tour({"name": "T"}, idx=0, total=1, previous_draft={"name": "prev"})
    assert built == [{"t2_drafts": True}, {}]
    assert out["draft_scores"] == [{"score": 5.0}]
    assert seen[0]["previous_draft"] == {"name": "prev"}
    assert "previous_draft" not in seen[1]             # an A1 call never carries it


# ── T3 gate: best draft wins + targeted repair ───────────────────────────────

GROUNDING_BAD = {"summary": "A 30-minute flight to Lukla."}
GROUNDING_OK = {"summary": "A short flight to Lukla."}


def _res(generated, score, judge_feedback=""):
    return {"generated": dict(generated), "quality_score": score, "judge_feedback": judge_feedback,
            "status": "success"}


@pytest.mark.asyncio
async def test_t3_keeps_the_best_draft_when_rounds_run_out(monkeypatch):
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    rewrite = AsyncMock(side_effect=[_res(GROUNDING_BAD, 5.0), _res(GROUNDING_BAD, 4.0)])
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["no numbers"], _res(GROUNDING_BAD, 7.0), {},
                                      tenant_id="t1")
    assert out["passed"] is False and out["attempts"] == 2
    assert out["result"]["quality_score"] == 7.0 and out["chosen_attempt"] == 0
    assert [d["score"] for d in out["drafts"]] == [7.0, 5.0, 4.0]
    assert out["grounding_issues"][0]["novel_numbers"] == ["30"]


@pytest.mark.asyncio
async def test_t3_legacy_loop_when_both_switches_off(monkeypatch):
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    monkeypatch.setenv("T2_BEST_DRAFT", "off")
    rewrite = AsyncMock(side_effect=[_res(GROUNDING_BAD, 5.0), _res(GROUNDING_BAD, 4.0)])
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["no numbers"], _res(GROUNDING_BAD, 7.0), {},
                                      tenant_id="t1")
    assert out["result"]["quality_score"] == 4.0            # the last draft, as before AA-793
    assert "previous_draft" not in rewrite.await_args.kwargs
    assert "BRAND VOICE FEEDBACK" not in rewrite.await_args.kwargs["feedback"]


@pytest.mark.asyncio
async def test_t3_grounding_only_failure_gets_a_sentence_repair_not_a_rewrite():
    rewrite = AsyncMock()
    repair = MagicMock(return_value={"generated": dict(GROUNDING_OK), "fields": ["summary"],
                                     "cost_usd": 0.004, "error": ""})
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("services.content_generation.grounding.repair", repair), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T", "summary": "Fly to Lukla."}, ["Fly to Lukla."],
                                      _res(GROUNDING_BAD, 7.5), {"forbidden_words": ["epic"]}, tenant_id="t1")
    rewrite.assert_not_awaited()
    assert out["passed"] is True and out["attempts"] == 1 and out["chosen_via"] == "sentence_repair"
    assert out["result"]["generated"] == GROUNDING_OK and out["result"]["quality_score"] == 7.5
    assert out["sentence_repairs"] == 1
    kw = repair.call_args.kwargs
    assert kw["stage"] == "t2_flag_fix" and kw["tenant_id"] == "t1"
    assert kw["system_prompt"] == tp.T3_REPAIR_SYSTEM and "Adventure Asia" not in kw["system_prompt"]
    assert "epic" in kw["forbidden"]
    # the T3 test (not the A1 one) decides whether a replacement is still unsupported
    assert kw["still_unsupported"]("A 45-minute flight.") and not kw["still_unsupported"]("A flight.")


@pytest.mark.asyncio
async def test_t3_falls_back_to_a_revision_when_the_sentence_repair_changes_nothing():
    rewrite = AsyncMock(return_value=_res(GROUNDING_OK, 6.0))
    repair = MagicMock(return_value={"generated": dict(GROUNDING_BAD), "fields": [], "cost_usd": 0.0,
                                     "error": ""})
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("services.content_generation.grounding.repair", repair), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["no numbers"],
                                      _res(GROUNDING_BAD, 5.0, judge_feedback="Sound more like the brand."),
                                      {}, tenant_id="t1")
    assert out["passed"] is True and out["attempts"] == 1 and out["chosen_via"] == "rewrite"
    kw = rewrite.await_args.kwargs
    assert kw["previous_draft"] == GROUNDING_BAD
    assert "30" in kw["feedback"] and "BRAND VOICE FEEDBACK: Sound more like the brand." in kw["feedback"]


@pytest.mark.asyncio
async def test_t3_structural_failure_revises_the_draft():
    rewrite = AsyncMock(return_value=_res(GROUNDING_OK, 8.0))
    repair = MagicMock()
    structural = MagicMock(side_effect=[["MISSING_FIELD"], []])
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("services.content_generation.grounding.repair", repair), \
            patch.object(tp, "_t3_structural_issues", structural):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["src"], _res(GROUNDING_OK, 8.0, "ignored above 7"),
                                      {}, tenant_id="t1")
    repair.assert_not_called()
    kw = rewrite.await_args.kwargs
    assert kw["previous_draft"] == GROUNDING_OK and "MISSING_FIELD" in kw["feedback"]
    assert "BRAND VOICE FEEDBACK" not in kw["feedback"]       # the judge already passed this draft
    assert out["passed"] is True


@pytest.mark.asyncio
async def test_t3_repairs_the_best_draft_not_the_last():
    # round 1 rewrite scores lower and still fails structurally; round 2 must repair attempt 0
    first = _res(GROUNDING_BAD, 7.0)
    rewrite = AsyncMock(return_value=_res(GROUNDING_OK, 4.0))
    structural = MagicMock(side_effect=[["MISSING_FIELD"], ["MISSING_FIELD"], []])
    repair = MagicMock()
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("services.content_generation.grounding.repair", repair), \
            patch.object(tp, "_t3_structural_issues", structural):
        await tp.run_t3_qa_gate({"name": "T"}, ["no numbers"], first, {}, tenant_id="t1")
    assert rewrite.await_count == 2
    assert rewrite.await_args_list[1].kwargs["previous_draft"] == first["generated"]


@pytest.mark.asyncio
async def test_t3_clean_draft_returns_even_with_a_higher_scoring_unclean_one(monkeypatch):
    """Truth first: a clean draft passes T3 at once; an earlier higher-scoring draft that states an
    unsupported number is never preferred over it."""
    monkeypatch.setenv("T2_TARGETED_REPAIR", "off")
    rewrite = AsyncMock(return_value=_res(GROUNDING_OK, 6.0))
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["no numbers"], _res(GROUNDING_BAD, 8.0), {},
                                      tenant_id="t1")
    assert out["passed"] is True and out["result"]["quality_score"] == 6.0


@pytest.mark.asyncio
async def test_t3_with_background_on_passes_the_s235_draft_without_any_repair(monkeypatch):
    monkeypatch.setenv("T3_BACKGROUND_FACTS", "on")
    draft = {"summary": "Stop at the 17th-century Punakha Dzong, then cross Dochula Pass (3,120 m).",
             "itineraries": "Day 2 — Walk to Dien Bien Phu, site of the 1954 battle."}
    rewrite, repair = AsyncMock(), MagicMock()
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("services.content_generation.grounding.repair", repair), \
            patch.object(tp, "_t3_structural_issues", return_value=[]):
        out = await tp.run_t3_qa_gate({"name": "T"}, ["Punakha Dzong. Dochula Pass. Dien Bien Phu."],
                                      _res(draft, 7.0), {}, tenant_id="t1")
    rewrite.assert_not_awaited()
    repair.assert_not_called()
    assert out["passed"] is True and out["attempts"] == 0 and out["background_cleared"] == 2


# ── job.result carries the evidence ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_result_records_the_chosen_draft_and_every_score():
    from services.jobs import t2_rewrite_job as t2

    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 5.0
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    chosen = {**_res(GROUNDING_OK, 7.0), "draft_scores": [{"attempt": 0, "score": 7.0}],
              "best_draft_restored": True}
    qa = {"passed": True, "result": chosen, "attempts": 1, "structural_issues": [], "grounding_issues": [],
          "chosen_attempt": 1, "chosen_via": "sentence_repair", "sentence_repairs": 1,
          "background_cleared": 0, "drafts": [{"attempt": 0, "score": 7.0}, {"attempt": 1, "score": 7.0}],
          # AA-925: T3's degraded entries reach job.result
          "degraded": [{"name": "jev_unavailable", "severity": "count", "detector": "t3_jev_clear", "count": 2}]}
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value=_res(GROUNDING_BAD, 7.0))), \
            patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
            patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))):
        diag = await t2._rewrite_and_save(pool, {"tour_id": "tour-1"}, {"name": "T", "itineraries": ""}, {},
                                          "t-1", "v-1", "p-1")
    assert diag["t3"] == {"chosen_attempt": 1, "chosen_via": "sentence_repair", "drafts": qa["drafts"],
                          "sentence_repairs": 1, "background_cleared": 0,
                          "graph_draft_scores": [{"attempt": 0, "score": 7.0}], "best_draft_restored": True,
                          "degraded": qa["degraded"]}
