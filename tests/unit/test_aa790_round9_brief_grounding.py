"""AA-790 round 9 (S256) — the T8 angle brief is grounded in the content seed before the T9 writer reads it
(services/acp_content_writing/brief_grounding.py), behind T9_BRIEF_GROUNDING_MODE (default off). Flag off, every
writer prompt is byte for byte the round-6 prompt: a 192-prompt fixture (3 packs x 8 channels x 2 briefs x route or
not x Facts + revision or not) is pinned to the hash it had on main before round 9. No DB, no model call."""
import hashlib
import json

import pytest

from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import GOALS
from services.acp_content_writing import brief_grounding as bg
from services.acp_content_writing import prompts, service

SEED = ("Riverside Monastery — visit the prayer hall\nLocation: Riverside Monastery, Valley Town, Example Province\n"
        "Day 2 of the trip\nSource itinerary: Walk 3 kilometres along the river to the monastery and join the "
        "evening prayers.\nStatus: included in the trip. Never call it optional.\n"
        "Place background (checked facts about the place itself — you may use these; they say nothing about what "
        "this trip does):\n- The monastery was founded in the 17th century by a travelling teacher.\n"
        "- Its hall holds a library of 300 hand-copied texts.")
SEED2 = ("Lakeside Camp — overnight in a tented camp\nLocation: Lakeside Camp, Example Province\nDay 3 of the trip\n"
         "Source itinerary: Arrive in the afternoon; overnight in a tented camp by the lake. Optional boat ride.")
BRIEF = ("Warm, picture-led story that starts in motion: the courtyard, the tea, the porter folding a blanket. "
         "Lead with the 3-kilometre walk along the river and the evening prayers. Three H2 sections in day order, "
         "a CTA paragraph, then a four-pair FAQ.")
PACKS = (
    {"brand_name": "Plain Co", "brand_type": "Small group tours", "voice": ["Clear", "Friendly"]},
    {"brand_name": "Pulse Co", "brand_type": "Young active adventure travel brand", "voice": ["Energetic", "Direct"]},
    {"brand_name": "Kin Co", "brand_type": "Premium family adventure travel brand", "voice": ["Warm", "Reassuring"]},
)
CHANNELS = ("blog", "linkedin", "facebook", "instagram", "tiktok", "ads", "email", "landing_page")
# sha256 of the 192 fixture prompts joined, computed on main 67af0922 (round 6 live) before round 9 existed
# (recomputed on 67af0922 and 717c58f1 after BRIEF became synthetic text, AA-919 golden-set guard).
# AA-789 tier 0 (S258): recomputed after the route blog's "Thin moment" rule lost its "ONE plain sentence" shape
# (was aec208af965d479a801d517e2267c73047f4e007e14335de4a0ca63b74a7a804).
ROUND6_FIXTURE_SHA = "9bf0f4fd5286922b01b020920f2491627d3d185a680d9f14734e08f15615a4fc"


def fixture_prompts(**extra) -> list[str]:
    out = []
    for pack in PACKS:
        for channel in CHANNELS:
            for brief in (BRIEF, "s"):
                for route in (None, [("a1", SEED), ("a2", SEED2)]):
                    for facts in (None, "[Fact id=f1] Visa: an e-visa costs about 30 USD."):
                        angle = {"name": "River Walk", "why_it_works": "w", "formula_fit": "f",
                                 "best_final_style": brief}
                        kw = {"atom_id": "a1"} if channel == "blog" else {}
                        out.append(prompts.build_user_prompt(
                            content_seed=SEED, goal=GOALS[0], channel_style=get_channel_style(channel),
                            brand_audience=pack, angle=angle, cta="Message us.", route_segments=route,
                            facts_text=facts, revision_feedback=["fix x"] if facts else None, **kw, **extra))
    return out


def fixture_sha(**extra) -> str:
    return hashlib.sha256("\n\x00\n".join(fixture_prompts(**extra)).encode()).hexdigest()


# ---------------------------------------------------------------- flag off: byte for byte the round-6 prompt

def test_flag_off_by_default_is_the_round6_prompt(monkeypatch):
    monkeypatch.delenv(bg.MODE_ENV, raising=False)
    assert len(fixture_prompts()) == 192
    assert fixture_sha() == ROUND6_FIXTURE_SHA


@pytest.mark.parametrize("value", ["off", "", "OFF", "r8", "on", "true", "1", "r9x"])
def test_any_other_env_value_is_off(monkeypatch, value):
    monkeypatch.setenv(bg.MODE_ENV, value)
    assert bg.brief_grounding_mode() == bg.MODE_OFF
    assert fixture_sha() == ROUND6_FIXTURE_SHA


def test_explicit_off_beats_the_env(monkeypatch):
    monkeypatch.setenv(bg.MODE_ENV, "r9")
    assert fixture_sha(brief_grounding="off") == ROUND6_FIXTURE_SHA


def test_system_prompt_and_writer_version_unchanged():
    """Round 9 never touches SYSTEM_PROMPT, so writer_prompt_version stays the round-6 hash."""
    assert service.WRITER_PROMPT_VERSION == "b5978839"


def test_ground_angle_off_returns_the_same_object(monkeypatch):
    monkeypatch.delenv(bg.MODE_ENV, raising=False)
    angle = {"name": "n", "best_final_style": BRIEF}
    assert bg.ground_angle(angle, SEED) is angle
    assert bg.ground_angle(angle, SEED, "off") is angle


# ---------------------------------------------------------------- flag on: only the brief line moves

@pytest.mark.parametrize("how", ["env", "explicit"])
def test_r9_changes_only_the_brief_line(monkeypatch, how):
    monkeypatch.delenv(bg.MODE_ENV, raising=False)
    off = fixture_prompts()
    if how == "env":
        monkeypatch.setenv(bg.MODE_ENV, " R9 ")
        on = fixture_prompts()
    else:
        on = fixture_prompts(brief_grounding="r9")
    assert fixture_sha(brief_grounding="r9") != ROUND6_FIXTURE_SHA
    changed = 0
    for a, b in zip(off, on):
        la, lb = a.splitlines(), b.splitlines()
        assert len(la) == len(lb)
        diff = [(x, y) for x, y in zip(la, lb) if x != y]
        assert all(x.startswith("ANGLE'S BEST FINAL STYLE: ") and y.startswith("ANGLE'S BEST FINAL STYLE: ")
                   for x, y in diff)
        changed += bool(diff)
    assert changed == 96  # every prompt with the long brief; the one-word brief 's' has nothing to drop


def test_r9_brief_line_content():
    prompt = fixture_prompts(brief_grounding="r9")[0]
    line = next(ln for ln in prompt.splitlines() if ln.startswith("ANGLE'S BEST FINAL STYLE: "))
    assert "folding a blanket" not in line and "Warm" not in line
    assert "Lead with the 3-kilometre walk along the river and the evening prayers." in line
    assert "Three H2 sections in day order, a CTA paragraph; a four-pair FAQ." in line


# ---------------------------------------------------------------- ground_brief

def test_scene_tone_and_thesis_clauses_are_dropped_grounded_ones_kept():
    out, dropped = bg.ground_brief(BRIEF, SEED)
    assert dropped == ["Warm, picture-led story that starts in motion: the courtyard, the tea, the porter "
                       "folding a blanket"]
    assert out.startswith("Lead with the 3-kilometre walk")


def test_a_brief_with_nothing_to_drop_comes_back_unchanged():
    brief = "Lead with the evening prayers at the monastery; three H2 sections; a 3–4 pair FAQ at the end."
    assert bg.ground_brief(brief, SEED) == (brief, [])


@pytest.mark.parametrize("brief", ["", "   ", None])
def test_empty_brief(brief):
    assert bg.ground_brief(brief, SEED) == (brief or "", [])


def test_everything_dropped_falls_back():
    out, dropped = bg.ground_brief("Tone is reassuring but never soft. Ends with a forward-looking nudge.", SEED)
    assert out == bg.FALLBACK_BRIEF and len(dropped) == 2


def test_numbers_must_be_in_the_seed_but_shape_counts_never_count():
    src = bg.Source(SEED)
    assert bg.unsupported_words("Lead with the 300 hand-copied texts of the 17th century", src) == []
    assert bg.unsupported_words("900–1,100 words, four H2 sections, then 3–4 FAQ pairs, 2 paragraphs", src) == []
    assert bg.unsupported_words("the 1,283 metres of the river", src) == ["1283"]
    assert bg.unsupported_words("Close on the ruins of Day 14", src) == ["ruins", "14"]


def test_paraphrased_seed_words_are_supported():
    src = bg.Source("Students spend years mastering one craft. The institute supplies artisans for the restoration "
                    "of dzongs. Overnight in a standard ger camp.")
    assert bg.unsupported_words("the mastery students reach, restoring dzongs, a night in a ger", src) == []


def test_function_and_brief_words_never_count():
    src = bg.Source(SEED)
    assert bg.unsupported_words("each section without any other detail; the reader arrives and continues", src) == []


def test_a_scene_the_seed_lacks_is_dropped_with_its_continuation():
    seed = ("Craft Institute — visit\nSource itinerary: visit the craft institute.\n"
            "Place background (checked facts about the place itself — you may use these; they say nothing about "
            "what this trip does):\n- Students learn painting, wood carving and embroidery over several years.")
    brief = ("Reader: the family watches students at work on painting, wood carving, or embroidery — crafts being "
             "practised, not demonstrated for visitors. Lead with: painting, wood carving and embroidery. "
             "Shape: four short paragraphs.")
    out, dropped = bg.ground_brief(brief, seed)
    assert out == "Lead with: painting, wood carving and embroidery. Shape: four short paragraphs."
    assert [d[:20] for d in dropped] == ["Reader: the family w", "crafts being practis"]


def test_a_label_moves_to_the_next_kept_clause():
    out, _ = bg.ground_brief("Brand: comfort and safety; the evening prayers at the monastery.", SEED)
    assert out == "Brand: the evening prayers at the monastery."


def test_a_list_item_after_a_dropped_one_is_judged_on_its_own():
    out, _ = bg.ground_brief("Lead with: the smell of incense and butter lamps; the evening prayers; the 300 texts.",
                             SEED)
    assert out == "Lead with: the evening prayers; the 300 texts."


def test_a_fragment_after_a_dropped_clause_goes_with_it():
    out, dropped = bg.ground_brief("Open with the weight of the date — 300 — without explaining it immediately. "
                                   "Lead with the evening prayers.", SEED)
    assert out == "Lead with the evening prayers."
    assert dropped[1] == "300"


def test_a_capitalised_clause_after_a_dropped_one_is_judged_on_its_own():
    out, _ = bg.ground_brief("Tone is reassuring but never soft; Lead with the evening prayers.", SEED)
    assert out == "Lead with the evening prayers."


def test_facts_section_counts_as_source():
    seed = SEED + "\n\nFACTS:\n[Fact id=f1] Visa: an e-visa costs about 30 USD and takes three business days."
    out, dropped = bg.ground_brief("Answer the visa question: the e-visa costs about 30 USD.", seed)
    assert dropped == [] and out.startswith("Answer the visa question")


def test_ground_angle_r9_changes_only_the_brief():
    angle = {"name": "n", "why_it_works": "Tone is warm and soft.", "formula_fit": "f", "best_final_style": BRIEF}
    out = bg.ground_angle(angle, SEED, "r9")
    assert out is not angle and out["why_it_works"] == angle["why_it_works"]
    assert out["best_final_style"] != BRIEF and angle["best_final_style"] == BRIEF


def test_module_text_is_generic():
    """No demo tenant name or brand word list in the module: it reads the seed only."""
    src = open(bg.__file__, encoding="utf-8").read()
    for name in ("Terra", "Trail Pulse", "Atlas", "WildKind", "Zorig", "Khövsgöl", "Kuang Si"):
        assert name not in src


# ---------------------------------------------------------------- writer_input marker

def test_writer_input_marks_the_mode_only_when_on(monkeypatch):
    monkeypatch.delenv(bg.MODE_ENV, raising=False)
    assert "brief_grounding" not in service.writer_input_snapshot({})
    monkeypatch.setenv(bg.MODE_ENV, "r9")
    assert service.writer_input_snapshot({})["brief_grounding"] == "r9"


# ---------------------------------------------------------------- the A/B harness

def _row(channel: str = "linkedin") -> dict:
    return {"piece_id": "p1", "tenant_id": "t", "request_id": "r1", "channel": channel, "status": "approved",
            "created_at": "2026-10-25 12:00:00", "content_text": "Old text.", "atom_id": "a1", "tenant": "Kin Co",
            "writer_input": {"seed": SEED, "goal": "introduction_awareness", "channel": channel,
                             "brand_pack": PACKS[2], "cta": "Message us.", "trip": "Valley Trail",
                             "angle": {"name": "River Walk", "why_it_works": "w", "formula_fit": "f",
                                       "best_final_style": BRIEF}}}


def test_harness_passes_the_mode_and_reads_the_brief_back():
    from scripts.aa790 import t9_writer_repanel as rp
    off, on = rp.writer_args(_row(), 0), rp.writer_args(_row(), 0, None, "r9")
    assert off["brief_grounding"] == "off" and on["brief_grounding"] == "r9"
    seen_off = rp.brief_seen(prompts.build_user_prompt(**off))
    seen_on = rp.brief_seen(prompts.build_user_prompt(**on))
    assert "folding a blanket" in seen_off and "folding a blanket" not in seen_on
    # explicit 'off' in the harness is the default prompt byte for byte
    default = {k: v for k, v in off.items() if k != "brief_grounding"}
    assert prompts.build_user_prompt(**off) == prompts.build_user_prompt(**default)


def test_harness_reads_a_multi_line_brief_whole():
    from scripts.aa790 import t9_writer_repanel as rp
    row = _row()
    row["writer_input"]["angle"]["best_final_style"] = "Reader: the evening prayers.\nShape: four short paragraphs."
    seen = rp.brief_seen(prompts.build_user_prompt(**rp.writer_args(row, 0)))
    assert seen == "Reader: the evening prayers.\nShape: four short paragraphs."


def test_harness_angles_file_keeps_only_chosen_requests(tmp_path):
    from scripts.aa790 import t9_writer_repanel as rp
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"arm": "r9c", "requests": {
        "r1": {"chosen": {"name": "n", "best_final_style": "b"}, "t8_seed_sha": "x", "mode": "off"},
        "r2": {"error": "skipped_cost_cap"}}}))
    assert list(rp.load_angles_file(str(path))) == ["r1"]


def test_harness_rejects_angles_file_with_regen(monkeypatch):
    from scripts.aa790 import t9_writer_repanel as rp
    monkeypatch.setattr("sys.argv", ["x", "--ids", "a", "--angles", "regen", "--angles-file", "f.json"])
    with pytest.raises(SystemExit):
        rp.main()
