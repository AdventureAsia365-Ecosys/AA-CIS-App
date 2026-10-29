"""AA-790 round 10 (S257) — the READER CHECKLIST block in the T9 writer prompt
(services/acp_content_writing/reader_checklist.py), behind T9_READER_CHECKLIST_MODE (default off). Flag off, every
writer prompt is byte for byte what it was: the round-9 192-prompt fixture keeps its pinned hash, with the grounding off
and with it on. No DB, no model call."""
import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import brief_grounding as bg
from services.acp_content_writing import prompts, service
from services.acp_content_writing import reader_checklist as rc
from tests.unit.test_aa790_round9_brief_grounding import (PACKS, ROUND6_FIXTURE_SHA, SEED, SEED2, _row,
                                                          fixture_prompts, fixture_sha)

FACTS = "[Fact id=f1]\nVisa: an e-visa costs about 30 USD.\n\n[Fact id=f2]\nSeason: the dry season runs Nov to Apr."
SEED_OFFERED = ("Lakeside Camp — overnight in a tented camp\nLocation: Lakeside Camp, Example Province\n"
                "Day 3 of the trip\nSource itinerary: Arrive in the afternoon; overnight in a tented camp by the lake. "
                "Optional boat rides are available.\n"
                "Status: OFFERED, NOT INCLUDED (the itinerary says: \"Optional boat rides are available.\"). "
                "Describe it as available, never as something the reader will do.")
SEED_NO_DAY = ("Hill Temple — visit\nLocation: Hill Temple, Example Province\n"
               "Source itinerary: Drive two hours to the hill temple and walk up the stone stairs.")
SEED_THIN = ("Old Market — browse the stalls\nLocation: Old Market, Valley Town\nDay 2 of the trip\n"
             "Source itinerary: Stop at the old market on the way.\n"
             "Thin moment: only 1 source fact — mention it in one sentence at most; never make it the hook.")


def blog(seed=SEED, **kw):
    return rc.checklist_block("r10", content_seed=seed, channel="blog", **kw)


# ---------------------------------------------------------------- flag off: the prompt does not move

def test_flag_off_by_default_keeps_the_round9_fixture_hash(monkeypatch):
    monkeypatch.delenv(rc.MODE_ENV, raising=False)
    monkeypatch.delenv(bg.MODE_ENV, raising=False)
    assert fixture_sha() == ROUND6_FIXTURE_SHA


@pytest.mark.parametrize("value", ["off", "", "OFF", "r9", "r10x", "on", "true", "1"])
def test_any_other_env_value_is_off(monkeypatch, value):
    monkeypatch.setenv(rc.MODE_ENV, value)
    assert rc.reader_checklist_mode() == rc.MODE_OFF
    assert fixture_sha() == ROUND6_FIXTURE_SHA


def test_explicit_off_beats_the_env(monkeypatch):
    monkeypatch.setenv(rc.MODE_ENV, "r10")
    assert fixture_sha(reader_checklist="off") == ROUND6_FIXTURE_SHA


def test_off_leaves_the_grounded_prompt_unchanged(monkeypatch):
    monkeypatch.delenv(rc.MODE_ENV, raising=False)
    assert fixture_prompts(brief_grounding="r9") == fixture_prompts(brief_grounding="r9", reader_checklist="off")


def test_system_prompt_and_writer_version_unchanged():
    assert service.WRITER_PROMPT_VERSION == "b5978839"


def test_block_is_empty_when_off():
    assert rc.checklist_block("off", content_seed=SEED, channel="blog") == ""
    assert rc.checklist_block(None, content_seed=SEED, channel="blog") == ""  # env unset in CI


# ---------------------------------------------------------------- flag on: only a block is added

@pytest.mark.parametrize("how", ["env", "explicit"])
def test_r10_only_adds_the_checklist_lines(monkeypatch, how):
    monkeypatch.delenv(rc.MODE_ENV, raising=False)
    off = fixture_prompts(brief_grounding="r9")
    if how == "env":
        monkeypatch.setenv(rc.MODE_ENV, " R10 ")
        on = fixture_prompts(brief_grounding="r9")
    else:
        on = fixture_prompts(brief_grounding="r9", reader_checklist="r10")
    for a, b in zip(off, on):
        lines = b.split("\n")
        i = next(k for k, ln in enumerate(lines) if ln.startswith("READER CHECKLIST ("))
        j = i + 1
        while j < len(lines) and lines[j].startswith("- "):
            j += 1
        assert lines[i - 1] == ""  # the block is its own paragraph
        assert "\n".join(lines[:i - 1] + lines[j:]) == a  # nothing else moved


def test_the_block_sits_after_the_seed_and_search_questions_before_the_revision():
    args = dict(content_seed=SEED, goal=GOALS[0], channel_style=get_channel_style("blog"), brand_audience=PACKS[0],
                angle={"name": "n", "why_it_works": "w", "formula_fit": "f", "best_final_style": "b"},
                cta="Message us.", atom_id="a1", search_questions=["How far is the monastery?"],
                revision_feedback=["fix x"], reader_checklist="r10")
    p = prompts.build_user_prompt(**args)
    assert p.index("CONTENT SEED") < p.index("SEARCH QUESTIONS") < p.index("READER CHECKLIST") \
        < p.index("PREVIOUS ATTEMPT FAILED")


# ---------------------------------------------------------------- the blog block

def test_blog_block_quotes_the_seeds_own_answers():
    out = blog(route_segments=[("a1", SEED), ("a2", SEED2)], facts_text=FACTS)
    assert "- When and where: Day 2: Riverside Monastery; Day 3: Lakeside Camp." in out
    assert ('Riverside Monastery: "Walk 3 kilometres along the river to the monastery and join the evening '
            'prayers."') in out
    getting = next(ln for ln in out.splitlines() if ln.startswith("- Getting there"))
    assert "Walk 3 kilometres" in getting
    stay = next(ln for ln in out.splitlines() if ln.startswith("- Meals and nights"))
    assert "overnight in a tented camp" in stay
    costs = next(ln for ln in out.splitlines() if ln.startswith("- Costs"))
    assert "Visa: an e-visa costs about 30 USD." in costs
    season = next(ln for ln in out.splitlines() if ln.startswith("- Best time"))
    assert "dry season" in season
    effort = next(ln for ln in out.splitlines() if ln.startswith("- How long"))
    assert effort.endswith("not in the seed: leave it out, never guess it")
    assert "plain sentence in the body" in out


def test_place_background_is_never_quoted():
    out = blog()
    assert "300 hand-copied texts" not in out and "17th century" not in out


def test_a_sentence_answers_every_topic_it_matches():
    seed = ("Ridge Trail — cycle\nDay 1 of the trip\nSource itinerary: A short ride gives a gauge of fitness levels "
            "before the journey begins.")
    out = blog(seed)
    for head in ("- Getting there", "- How long"):
        line = next(ln for ln in out.splitlines() if ln.startswith(head))
        assert "gauge of fitness levels" in line


def test_offered_moments_are_listed_as_a_choice():
    out = blog(SEED_OFFERED)
    line = next(ln for ln in out.splitlines() if ln.startswith("- Included or a choice"))
    assert '"Optional boat rides are available."' in line


def test_no_day_line_means_no_day_number():
    assert "the seed gives no day number: do not give one" in blog(SEED_NO_DAY)


def test_a_thin_moment_gets_its_day_but_no_activity_quote():
    out = blog(route_segments=[("a1", SEED), ("a2", SEED_THIN)])
    assert "Day 2: Riverside Monastery, Old Market" in out
    assert "Stop at the old market" not in out


def test_search_questions_line_only_with_questions():
    assert "SEARCH QUESTIONS above" in blog(search_questions=["q?"])
    assert "SEARCH QUESTIONS above" not in blog()


def test_long_quotes_are_cut_on_a_word():
    seed = "Long Road — drive\nDay 1 of the trip\nSource itinerary: " + " ".join(["drive"] * 80) + "."
    quoted = [q for q in blog(seed).split('"')[1::2] if q.startswith("drive")]
    assert quoted and all(len(q) <= rc.QUOTE_MAX + 2 and q.endswith(" …") for q in quoted)


def test_seed_without_moments_gives_no_block():
    assert blog("just a stray paragraph with no label") == ""


def test_fact_entries():
    assert rc.fact_entries(FACTS) == ["Visa: an e-visa costs about 30 USD.", "Season: the dry season runs Nov to Apr."]
    assert rc.fact_entries(None) == []


# ---------------------------------------------------------------- the social block

def test_social_block_is_one_line_with_the_label_action_not_the_itinerary_line():
    out = rc.checklist_block("r10", content_seed=SEED, channel="linkedin")
    assert "\n" not in out
    assert "when it happens (Day 2: Riverside Monastery)" in out
    assert 'what the traveller does there ("visit the prayer hall")' in out
    assert "Walk 3 kilometres" not in out  # an itinerary line may name other stops the SCOPE line forbids
    assert "stay out of the post" in out


def test_social_prompt_carries_the_block(monkeypatch):
    monkeypatch.delenv(rc.MODE_ENV, raising=False)
    p = prompts.build_user_prompt(
        content_seed=SEED_OFFERED, goal=GOALS[0], channel_style=get_channel_style("linkedin"), brand_audience=PACKS[2],
        angle={"name": "n", "why_it_works": "w", "formula_fit": "f", "best_final_style": "b"}, cta="Message us.",
        reader_checklist="r10")
    line = next(ln for ln in p.splitlines() if ln.startswith("READER CHECKLIST ("))
    assert "A choice, not part of the plan" in line and "Day 3" in line


def test_module_text_is_generic():
    src = open(rc.__file__, encoding="utf-8").read()
    for name in ("Terra", "Trail Pulse", "Atlas", "WildKind", "Zorig", "Kuang Si", "Luang Prabang"):
        assert name not in src


# ---------------------------------------------------------------- writer_input marker + harness

def test_writer_input_marks_the_mode_only_when_on(monkeypatch):
    monkeypatch.delenv(rc.MODE_ENV, raising=False)
    assert "reader_checklist" not in service.writer_input_snapshot({})
    monkeypatch.setenv(rc.MODE_ENV, "r10")
    assert service.writer_input_snapshot({})["reader_checklist"] == "r10"


def test_harness_passes_the_mode_and_reads_the_block_back():
    from scripts.aa790 import t9_writer_repanel as rp
    off = rp.writer_args(_row(), 0)
    on = rp.writer_args(_row(), 0, None, "r9", "r10")
    assert off["reader_checklist"] == "off" and on["reader_checklist"] == "r10"
    assert rp.checklist_seen(prompts.build_user_prompt(**off)) == ""
    block = rp.checklist_seen(prompts.build_user_prompt(**on))
    assert block.startswith("READER CHECKLIST (") and "Day 2" in block and "\n" not in block
    default = {k: v for k, v in off.items() if k != "reader_checklist"}
    assert prompts.build_user_prompt(**off) == prompts.build_user_prompt(**default)


def test_harness_reads_the_whole_blog_block():
    from scripts.aa790 import t9_writer_repanel as rp
    row = _row("blog")
    block = rp.checklist_seen(prompts.build_user_prompt(**rp.writer_args(row, 0, None, "off", "r10")))
    assert block.splitlines()[0].startswith("READER CHECKLIST (")
    assert block.splitlines()[-1].startswith("- Say each fact once")
