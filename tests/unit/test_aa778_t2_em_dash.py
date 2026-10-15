"""AA-778 (S238) — no em-dash in a T2 tenant tour version, and none in our own T2 writer prompts.

The sentences are real: the S238 T2 versions of the Mongolia 15-day tour (Trail Pulse v2, Atlas &
Hearth v3, 15/10/2026), which carried 45 and 43 em-dashes. Mock-only: no AWS, no DB, no LLM.
"""
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing.hygiene import fix_dashes, sentence_spans
from services.content_generation import batch_prompt, itinerary_utils, prompts
from services.content_generation.t2_dashes import em_dash_count, fix_t2_dashes, unify_day_markers

EM = "—"
MASTER_NAME = f"Mongolia: Khövsgöl Lake and Gobi Desert {EM} 15 Days"
SOURCE = {"name": MASTER_NAME, "summary": "A 15-day journey from Lake Khövsgöl to the Gobi.",
          "highlights": '["Uran Togoo crater"]', "itineraries": "Day 1: Arrive in Ulaanbaatar."}


def _fix(field, value, source=SOURCE):
    return fix_t2_dashes({field: value}, source)[0][field]


# ── the version's text fields ───────────────────────────────────────────────────────────────

def test_canonical_day_marker_becomes_a_colon():
    text = (f"Day 1 {EM} Arrival in Ulaanbaatar\nLand at Chinggis Khaan International Airport.\n\n"
            f"Day 2 {EM} Amarbayasgalant Monastery and a Ger Camp Arrival\nBody.")
    assert _fix("itineraries", text) == (
        "Day 1: Arrival in Ulaanbaatar\nLand at Chinggis Khaan International Airport.\n\n"
        "Day 2: Amarbayasgalant Monastery and a Ger Camp Arrival\nBody.")


def _day_lines(text):
    return [line for line in text.splitlines() if re.match(r"Day \d+\b", line)]


def test_a_title_with_its_own_colon_switches_the_whole_tour_to_one_separator():
    # S238: 8 of 15 Atlas & Hearth and 7 of 15 Trail Pulse day titles carry their own colon; a colon
    # marker would give "Day 4: Northward to Lake Khövsgöl: Taiga Shore …" (two colons on one line)
    text = (f"Day 1 {EM} Arrival in Ulaanbaatar\nLand and rest.\n\n"
            f"Day 2 {EM} Amarbayasgalant Monastery: Woodcraft, History, and a Ger Camp Arrival\nBody.\n\n"
            f"Day 3 {EM} Karakorum {EM} the old capital\nBody.\n\n"
            f"Day 4 {EM} Northward to Lake Khövsgöl: Taiga Shore and Steppe Boundary\nBody: a long drive.\n\n"
            f"Day 5 {EM} Rest Day by the Lake\nBody.")
    out = _fix("itineraries", text)
    assert out == (
        "Day 1 · Arrival in Ulaanbaatar\nLand and rest.\n\n"
        "Day 2 · Amarbayasgalant Monastery: Woodcraft, History, and a Ger Camp Arrival\nBody.\n\n"
        "Day 3 · Karakorum, the old capital\nBody.\n\n"
        "Day 4 · Northward to Lake Khövsgöl: Taiga Shore and Steppe Boundary\nBody: a long drive.\n\n"
        "Day 5 · Rest Day by the Lake\nBody.")
    days = _day_lines(out)
    assert len(days) == 5
    assert {re.match(r"Day \d+(\W+)", d).group(1) for d in days} == {" · "}   # one separator per tour
    assert all(d.count(":") <= 1 for d in days)                               # never two colons on a day line
    assert em_dash_count(out) == 0


def test_the_marker_never_becomes_a_sentence_end():
    # "Day 4. Title" would be cut into its own sentence by the admin T2 sentence editor; " · " keeps one span
    out = _fix("itineraries", f"Day 4 {EM} Lake Khövsgöl: Taiga Shore\nBody.")
    assert out == "Day 4 · Lake Khövsgöl: Taiga Shore\nBody."
    assert [sp["text"] for sp in sentence_spans(out.splitlines()[0])] == ["Day 4 · Lake Khövsgöl: Taiga Shore"]
    assert len(sentence_spans("Day 4. Lake Khövsgöl Taiga Shore")) == 2   # why not "Day 4. Title"


def test_unify_day_markers_leaves_a_colon_free_tour_alone():
    text = "Day 1: Arrival\nBody.\n\nDay 2: Gobi Desert\nBody: dunes."
    assert unify_day_markers(text) == text
    assert unify_day_markers("Day 1: Arrival\nBody.\n\nDay 2: Gobi: Dunes\nBody.") == \
        "Day 1 · Arrival\nBody.\n\nDay 2 · Gobi: Dunes\nBody."


def test_a_dash_inside_a_day_title_does_not_pair_with_the_marker():
    assert _fix("itineraries", f"Day 3 {EM} Karakorum {EM} the old capital\nBody.") == \
        "Day 3: Karakorum, the old capital\nBody."


@pytest.mark.parametrize("before,after", [
    # a pair is a parenthesis: commas, or brackets when it has its own commas
    (f"The afternoon moves into Yol Valley {EM} Vulture Valley {EM} where cliff walls rise above 2,000 meters.",
     "The afternoon moves into Yol Valley, Vulture Valley, where cliff walls rise above 2,000 meters."),
    (f"It is also the traditional territory of the Tsaatan people {EM} the Dukha {EM} whose lives remain shaped "
     "by reindeer herding.",
     "It is also the traditional territory of the Tsaatan people, the Dukha, whose lives remain shaped "
     "by reindeer herding."),
    # S238: an opening phrase, a bracketed pair, then the main clause keeps its comma
    (f"Founded in 1727 and built entirely with traditional woodworking methods {EM} no metal beams, no nails "
     f"{EM} it stands as one of Mongolia's three major monastic centers.",
     "Founded in 1727 and built entirely with traditional woodworking methods (no metal beams, no nails), "
     "it stands as one of Mongolia's three major monastic centers."),
    # a single dash: a list after a full clause gets a colon, a continuing clause a comma
    (f"Along the way, it passes through landscapes shaped by eruption, empire, and centuries of nomadic life {EM} "
     "dormant volcanic craters, the ruins of once-vast monastic complexes.",
     "Along the way, it passes through landscapes shaped by eruption, empire, and centuries of nomadic life: "
     "dormant volcanic craters, the ruins of once-vast monastic complexes."),
    (f"Built in 1727 using traditional woodworking methods, the structure contains no metal beams or nails {EM} "
     "and unlike most Mongolian monasteries, it survived the political purges of the 1930s largely intact.",
     "Built in 1727 using traditional woodworking methods, the structure contains no metal beams or nails, "
     "and unlike most Mongolian monasteries, it survived the political purges of the 1930s largely intact."),
    (f"This is Mongolia as a physical journey {EM} wide, fast-changing, and worth every kilometer.",
     "This is Mongolia as a physical journey: wide, fast-changing, and worth every kilometer."),
])
def test_prose_sentences(before, after):
    assert _fix("summary", before) == after


def test_highlights_each_item():
    out = _fix("highlights", [
        f"Kharkhorin {EM} the former capital of the Mongol Empire {EM} and Erdene Zuu Monastery",
        f"Ride camels to the Khongor Sand Dunes {EM} 120 kilometers long, 6–12 kilometers wide",
        "Trek the shores of Lake Khövsgöl",
    ])
    assert out == ["Kharkhorin (the former capital of the Mongol Empire) and Erdene Zuu Monastery",
                   "Ride camels to the Khongor Sand Dunes: 120 kilometers long, 6–12 kilometers wide",
                   "Trek the shores of Lake Khövsgöl"]


def test_a_number_range_stays_a_range():
    assert _fix("summary", f"The monastery was rebuilt in 1637{EM}1638.") == "The monastery was rebuilt in 1637–1638."


def test_the_master_name_is_kept_verbatim_everywhere():
    out, _ = fix_t2_dashes({"name": MASTER_NAME, "seo_title": MASTER_NAME,
                            "summary": f"{MASTER_NAME} is the classic route {EM} it covers the north and the south."},
                           SOURCE)
    assert out["name"] == MASTER_NAME and out["seo_title"] == MASTER_NAME
    assert out["summary"] == f"{MASTER_NAME} is the classic route. It covers the north and the south."


def test_a_name_the_tenant_writer_made_up_is_tenant_copy():
    out, _ = fix_t2_dashes({"name": f"Mongolia: Khövsgöl Lake to the Gobi Desert {EM} 15 Days",
                            "seo_title": f"Mongolia: Khövsgöl Lake & Gobi Desert {EM} 15 Days",
                            "subtitle": f"North to south through Mongolia {EM} reindeer herders, volcanic craters"},
                           SOURCE)
    assert out == {"name": "Mongolia: Khövsgöl Lake to the Gobi Desert, 15 Days",
                   "seo_title": "Mongolia: Khövsgöl Lake & Gobi Desert, 15 Days",
                   "subtitle": "North to south through Mongolia: reindeer herders, volcanic craters"}


def test_a_quote_copied_from_the_source_keeps_its_dash():
    source = dict(SOURCE, description=f"Locals call it the \"Singing Sand {EM} the dunes that hum\".")
    assert _fix("summary", f"Locals call it the \"Singing Sand {EM} the dunes that hum\".", source) == \
        f"Locals call it the \"Singing Sand {EM} the dunes that hum\"."


def test_other_keys_and_shapes():
    rc = {"status": "done", "trip_type": "adventure", "seo_meta": "No dash here.",
          "itineraries": [{"day": 1, "title": f"Ulaanbaatar {EM} arrival", "body": f"Land {EM} and rest."}]}
    out, log = fix_t2_dashes(rc, SOURCE)
    assert out["status"] == "done" and out["trip_type"] == "adventure" and out["seo_meta"] == "No dash here."
    assert out["itineraries"] == [{"day": 1, "title": "Ulaanbaatar: arrival", "body": "Land, and rest."}]
    assert log == ["itineraries: 2 -> 0 em-dash(es)"]
    assert rc["itineraries"][0]["title"] == f"Ulaanbaatar {EM} arrival"  # the input is not mutated


def test_em_dash_count():
    assert em_dash_count({"a": f"x {EM} y", "b": [f"{EM}", {"c": f"{EM}{EM}"}], "d": 3}) == 4


def test_bracket_close_comma_needs_a_subject_after_the_pair():
    # 'are' opens no new clause: the pair is inside the subject, no comma
    assert fix_dashes(f"Its walls {EM} thick, whitewashed {EM} are visible from the road.")[0] == \
        "Its walls (thick, whitewashed) are visible from the road."
    # the text before the pair has a finite verb: it is already the main clause
    assert fix_dashes(f"The hall is old {EM} dark, quiet {EM} and it feels it.")[0] == \
        "The hall is old (dark, quiet) and it feels it."


# ── the T2 job saves the converted text ─────────────────────────────────────────────────────

def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_the_t2_job_saves_dash_free_content():
    from services.jobs import t2_rewrite_job as t2
    generated = {"name": f"Mongolia: Khövsgöl Lake to the Gobi Desert {EM} 15 Days",
                 "subtitle": f"North to south {EM} reindeer herders and dunes",
                 "summary": f"Start at Lake Khövsgöl {EM} 2,760 square kilometers of freshwater {EM} then go south.",
                 "highlights": [f"Uran Togoo {EM} a dormant volcano"],
                 "itineraries": f"Day 1 {EM} Arrival in Ulaanbaatar\nLand and rest.",
                 "seo_title": f"Mongolia {EM} 15 Days", "seo_meta": "Meta.", "trip_type": "adventure"}
    result = {"status": "success", "generated": generated, "quality_score": 8.0}
    qa = {"result": result, "passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": []}
    conn = AsyncMock()
    conn.fetchrow.return_value = None   # no admin SEO row
    conn.fetchval.return_value = 5.0    # source quality score
    tour = dict(SOURCE, subtitle="", description="", seo_title="", seo_meta="", country="Mongolia",
                duration="15 Days")
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value=result)), \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
         patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))):
        diag = await t2._rewrite_and_save(_pool(conn), {"tour_id": "tour-1"}, tour, {}, "t-1", "v-1", "p-1")
    saved = [c.args for c in conn.execute.call_args_list if "rewritten_content" in c.args[0]]
    rc = json.loads(saved[0][1])
    assert em_dash_count(rc) == 0
    assert rc["itineraries"] == "Day 1: Arrival in Ulaanbaatar\nLand and rest."
    assert rc["summary"] == "Start at Lake Khövsgöl (2,760 square kilometers of freshwater) then go south."
    assert diag["em_dashes"]["before"] == 7 and diag["em_dashes"]["after"] == 0
    assert "itineraries: 1 -> 0 em-dash(es)" in diag["em_dashes"]["fields"]


# ── our own T2 prompt text ──────────────────────────────────────────────────────────────────

_TOUR = {"name": f"Bhutan: Culinary Trail East to West {EM} 10 Days", "country": "Bhutan", "duration": "10 Days",
         "summary": "A journey from Samdrup Jongkhar to Paro.", "highlights": ["Punakha Dzong"],
         "itineraries": "Day 1: Arrive in Samdrup Jongkhar.\nDay 2: Drive to Tashigang."}
_T2 = {"tour": _TOUR, "seo": {}, "few_shots": [], "subtitle_focus": "standard", "is_tenant_rewrite": True,
       "brand_name": "Trail Pulse", "brand_system_prompt": "Write for riders.", "brand_style_guide": "Short.",
       "brand_forbidden_words": ["conquer"], "brand_core_idea": "Movement and freedom.",
       "brand_customer_segment": "Active 25-40", "brand_customer_mindset": "Wants energy.",
       "brand_voice_examples": ["Energetic"], "brand_good_examples": "Start the morning on a trail."}
_RULE = "- Never use an em-dash (—) in any field"


def _dash_lines(text: str) -> list[str]:
    """Lines with an em-dash, minus the PUNCTUATION rule (it names the character) and the source tour's
    own name (TOUR DATA is the source, not our instruction)."""
    return [ln for ln in text.splitlines() if EM in ln and not ln.startswith(_RULE)
            and not ln.startswith("- Name: ")]


@pytest.mark.parametrize("state", [_T2, {"tour": _TOUR, "seo": {}, "is_tenant_rewrite": True}])
def test_t2_writer_prompts_carry_no_em_dash_but_the_rule(state):
    system, user, _ = batch_prompt.materialize_s1_prompt(dict(state))
    assert _dash_lines(system) == [] and _dash_lines(user) == []
    assert _RULE in system


def test_t2_nudge_prompt_carries_no_em_dash():
    client = MagicMock()
    client.generate.side_effect = RuntimeError("stop")
    with pytest.raises(RuntimeError):
        itinerary_utils.nudge_itinerary_day(client, "Day 1 source.", "Title", "Body.", 50,
                                            voice_hint="Energetic")
    assert EM not in client.generate.call_args.args[0].system_prompt


def test_every_t2_dashless_fragment_is_still_in_an_a1_prompt():
    """A shared-text edit that changes a fragment must update T2_DASHLESS too, or the dash comes back."""
    a1_state = {**_T2, "is_tenant_rewrite": False}
    system, user, _ = batch_prompt.materialize_s1_prompt(dict(a1_state))
    a1 = system + user + itinerary_utils._ITINERARY_NUDGE_SYSTEM_PROMPT
    missing = [old for old, _ in prompts.T2_DASHLESS if old not in a1]
    assert missing == []
    assert all(EM not in new for _, new in prompts.T2_DASHLESS)


def test_t3_feedback_carries_no_em_dash():
    from services.acp_produce.tenant_pipeline import _revision_feedback, t3_repair_feedback
    fb = t3_repair_feedback(["FORBIDDEN_WORD"], [{"field": "summary", "novel_numbers": ["9"], "sentence": "s"}])
    fb2 = _revision_feedback({"structural": [], "grounding": [], "score": 5.0,
                              "result": {"judge_feedback": "Sound more like the brand."}})
    assert EM not in fb and EM not in fb2
