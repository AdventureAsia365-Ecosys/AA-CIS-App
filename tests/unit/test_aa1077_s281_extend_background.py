"""AA-1077 (S281, CIS 426) — the two causes of paid trial 5's length hold, each behind a setting.

Trial 5: a repair round shrank a blog from above the floor to under it, then the lengthening repair asked two sections
and the writer left both empty ("no fitting fact"): the repair may only retell untold source facts.

- cis.t9.blog_extend_shrink_sections (default 3): after a repair round that SHRANK the blog under the floor, the plan
  asks that many sections (each another chance that a paragraph fits), the same words shared between them; 0 = the
  deficit alone (the plan before S281).
- cis.t9.blog_extend_material (default source_facts = the repair before S281): with_background lets a paragraph give
  general background about what the post or its source names, never a trip claim. Each background paragraph is
  checked in code: no reader / group address, no new name, no number or time the source lacks, no new F17 trip-fact or
  F18 rarity finding on the spliced text; the gates then run on the whole text as for any attempt.
- the defaults keep the source-facts prompt byte for byte; the options are pinned once per piece with the switch.

Invented places only (Vellmar Gorge, Tessaly, the Ast river); the model is always mocked; real settings reader with a
hand-set snapshot (no DB).
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.acp_content_writing import generate, length_contract as lc, length_extend as le
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing.trip_facts import TripFactContext
from services.quality import settings_keys
from shared import settings
from shared.llm_client.models import LLMResponse
from shared.settings.reader import _Row

TRIAL = "00000000-0000-4000-8000-0000000000a1"    # invented tenant ids
OTHER = "00000000-0000-4000-8000-0000000000b2"
MATERIAL, SHRINK = settings_keys.T9_BLOG_EXTEND_MATERIAL, settings_keys.T9_BLOG_EXTEND_SHRINK_SECTIONS

CTA = "Write to us to plan your days in Vellmar Gorge."
SEED = (
    "Vellmar Gorge — walk the rim path\n"
    "Source itinerary: A walk along the rim path above the gorge.\n"
    "Place background:\n"
    "- Vellmar Gorge is a limestone canyon cut by the Ast river.\n"
    "\n"
    "Tessaly — stop at the village market\n"
    "Source itinerary: A stop at the market of Tessaly.\n"
)
FACTS = (
    "\nFacts:\n"
    "- The shepherd huts on the ridge are built from flat river stones without any mortar.\n"
    "- Lake Orrin freezes from the northern shore first and the ice stays until late spring.\n"
    "- The market in Tessaly sells smoked trout and sheep cheese from the villages around the lake.\n"
    "- The rim path is lined with old juniper trees bent low by the wind from the plateau.\n"
)
SOURCE = SEED + FACTS

# Distinct invented sentences per section (no repeats, so the seam checks read them as they would a real post).
WORDS = ["amber", "birch", "cedar", "dune", "elder", "fern", "gorse", "hazel", "iris", "juniper", "kelp", "larch",
         "moss", "nettle", "oak", "pine", "quince", "rowan", "sorrel", "thyme", "umber", "vetch", "willow", "yarrow"]
HEADS = ["The Rim Path", "Above the Ast", "Tessaly Lanes", "The Lake Road", "Stone Huts"]


def _sentences(head: str, n: int) -> list[str]:
    tag = head.split()[-1].lower()
    return [f"The {WORDS[i % 24]} slope near the {tag} keeps a {WORDS[(i * 7 + 3) % 24]} hedge along the "
            f"{WORDS[(i * 5 + 1) % 24]} wall." for i in range(n)]


def _post(cut: int = 0) -> str:
    """A blog over the floor (before the repair); `cut` drops that many sentences of 'Above the Ast' (the repair)."""
    secs = []
    for k, head in enumerate(HEADS):
        sents = _sentences(head + str(k), 16)
        if head == "Above the Ast" and cut:
            sents = sents[:-cut]
        secs.append(f"## {head}\n\n" + " ".join(sents[:8]) + "\n\n" + " ".join(sents[8:]))
    intro = "Vellmar Gorge rewards a slow walk. Tessaly waits at the end of the lake road."
    return (f"{intro}\n\n" + "\n\n".join(secs) + f"\n\n{CTA}\n\n## FAQ\n\n**Q: Where is Tessaly?**\n"
            "A: Tessaly sits at the end of the lake road.\n")


def _trial_shape() -> tuple[str, str]:
    """(before, after): the repair took a blog over the floor to one just under it, the deficit to the target being
    the 81-160 words for which the deficit alone asks TWO sections (trial 5: 4,988 → 4,669 chars)."""
    before = _post()
    for cut in range(1, 16):
        after = _post(cut)
        if lc.measure(after) < lc.floor_chars():
            return before, after
    raise AssertionError("fixture never went under the floor")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(le.EXTEND_ENV, raising=False)
    monkeypatch.setattr(settings.READER, "_snapshot", {})
    le._PINNED.set(None)
    le._PINNED_OPTS.set(None)


def _values(monkeypatch, **rows):
    """rows: {"<setting>__<platform|trial|other>": value} into the reader's snapshot."""
    snap = {}
    for name, value in rows.items():
        key, _, scope = name.partition("__")
        tid = {"platform": None, "trial": TRIAL, "other": OTHER}[scope]
        snap[({"material": MATERIAL, "shrink": SHRINK, "switch": le.EXTEND_SETTING}[key], tid)] = _Row(value=value,
                                                                                                       version=1)
    monkeypatch.setattr(settings.READER, "_snapshot", snap)


def _call(adds: dict, cost=0.004, model="writer-model"):
    def call(*, sections, facts, voice=None, **kw):
        call.seen = {"sections": sections, "facts": facts, "voice": voice, **kw}
        return [adds.get(s["heading"], "") for s in sections], cost, model
    return call


def _ctx() -> TripFactContext:
    return TripFactContext(seed=SEED, source=SOURCE, cta=CTA, channel="blog")


# ---------------------------------------------------------------- the settings

def test_the_keys_are_the_compiled_definitions():
    m, s = settings.definition(MATERIAL), settings.definition(SHRINK)
    assert (le.MATERIAL_SETTING, le.SHRINK_SECTIONS_SETTING) == (MATERIAL, SHRINK)
    assert m.default_value == le.MATERIAL_FACTS and list(m.allowed_values) == [le.MATERIAL_FACTS,
                                                                               le.MATERIAL_BACKGROUND]
    assert s.default_value == 3 and m.tenant_scoped and s.tenant_scoped
    assert m.evidence_required and s.evidence_required and m.env_override is None and s.env_override is None


def test_options_default_and_per_tenant(monkeypatch):
    assert le.options(TRIAL) == {"material": "source_facts", "shrink_sections": 3}
    _values(monkeypatch, material__trial="with_background", shrink__platform=0)
    assert le.options(TRIAL) == {"material": "with_background", "shrink_sections": 0}
    assert le.options(OTHER) == {"material": "source_facts", "shrink_sections": 0}


def test_options_that_cannot_be_read_are_the_repair_before_s281():
    with patch.object(le.settings, "get", side_effect=KeyError("unknown key")):
        assert le.options(TRIAL) == le.MAIN_OPTIONS == {"material": "source_facts", "shrink_sections": 0}


def test_the_options_are_pinned_with_the_switch_only_when_it_is_on(monkeypatch):
    _values(monkeypatch, switch__trial="on", material__trial="with_background")
    assert le.pin(OTHER, "blog") == "off" and le._PINNED_OPTS.get() is None
    assert le.pin(TRIAL, "blog") == "on" and le._PINNED_OPTS.get()["material"] == "with_background"
    _values(monkeypatch, switch__trial="on")          # a mid-piece change is not seen: the pin stands
    assert le._opts(None, TRIAL)["material"] == "with_background"
    le.record({"length_trace": {}}, "blog", "on")
    assert le._PINNED.get() is None and le._PINNED_OPTS.get() is None


# ---------------------------------------------------------------- cause 1: a repair that shrank the blog

def test_the_fixture_is_the_trial_shape():
    before, after = _trial_shape()
    assert lc.measure(before) >= lc.floor_chars() > lc.measure(after)
    words = -(-(lc.extend_target_chars() - lc.measure(after)) // lc.CHARS_PER_WORD)
    assert le.WORDS_MAX // 2 < words <= le.WORDS_MAX                    # the deficit alone asks two sections


def test_a_shrunk_blog_asks_the_shrink_setting_sections_and_the_deficit_alone_asks_two():
    before, after = _trial_shape()
    main_plan, why = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE, switch="on",
                                          opts={"material": "source_facts", "shrink_sections": 0})
    assert why == "expand" and len(main_plan["sections"]) == 2 and "shrunk" not in main_plan
    plan, _ = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE, switch="on",
                                   opts={"material": "source_facts", "shrink_sections": 3})
    assert len(plan["sections"]) == 3 and plan["shrunk"] is True
    assert plan["sections"][0]["heading"] == "Above the Ast" or any(s.get("hole_chars") for s in plan["sections"])
    words = sum(s["words"] for s in plan["sections"])
    assert words >= sum(s["words"] for s in main_plan["sections"]) - 2          # same words, shared out
    assert all(s["words"] >= le.WORDS_MIN for s in plan["sections"])


def test_a_blog_the_repair_did_not_shorten_keeps_the_deficit_plan():
    _, after = _trial_shape()
    plan, _ = le.plan_after_repair(after, before=after, channel="blog", source=SOURCE, switch="on",
                                   opts={"material": "source_facts", "shrink_sections": 3})
    assert len(plan["sections"]) == 2 and "shrunk" not in plan


def test_a_shrunk_blog_is_re_extended_over_the_floor_when_two_sections_have_nothing():
    """The trial case: the first two sections asked get nothing. With the deficit alone that is the end (held F4);
    with the shrink plan the third section's paragraph takes the blog back over the floor."""
    before, after = _trial_shape()
    main_plan, _ = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE, switch="on",
                                        opts={"material": "source_facts", "shrink_sections": 0})
    plan, _ = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE, switch="on",
                                   opts={"material": "source_facts", "shrink_sections": 3})
    extra = next(s["heading"] for s in plan["sections"] if s["heading"] not in
                 {m["heading"] for m in main_plan["sections"]})
    para = ("The shepherd huts on the ridge are built from flat river stones laid without any mortar, and the market "
            "in Tessaly sells smoked trout and sheep cheese brought in from the villages around the lake.")
    adds = {extra: para}
    text0, _, e0 = le.extend(after, main_plan, call=_call(adds), source=SOURCE, cta=CTA, trip_ctx=_ctx())
    assert e0["added"] == 0 and not e0["reached_floor"] and text0 == after
    assert all(r["why"].startswith("empty") for r in e0["refused"])
    text, cost, entry = le.extend(after, plan, call=_call(adds), source=SOURCE, cta=CTA, trip_ctx=_ctx())
    assert entry["added"] == 1 and entry["reached_floor"] and entry["shrunk"] is True and cost == 0.004
    assert lc.measure(text) >= lc.floor_chars() and para in text
    assert not qg.gate_extreme_length(qg.strip_citation_tags(text), "blog")["violations"]


# ---------------------------------------------------------------- cause 2: background material

def _told() -> str:
    """A short blog that already tells every source fact (no untold fact left)."""
    told = " ".join(ln.lstrip("- ").strip() for ln in FACTS.splitlines() if ln.startswith("- "))
    return (f"Vellmar Gorge rewards a slow walk.\n\n## The Rim Path\n\nA walk along the rim path above the gorge. "
            f"{told}\n\n## Tessaly\n\nA stop at the market of Tessaly. Vellmar Gorge is a limestone canyon cut by "
            f"the Ast river.\n\n{CTA}\n")


def test_no_untold_fact_is_a_dead_end_with_source_facts_only_and_not_with_background():
    text = _told()
    assert not qg.unused_source_facts(SOURCE, text)
    assert le.plan_after_repair(text, before=None, channel="blog", source=SOURCE, switch="on",
                                opts={"material": "source_facts", "shrink_sections": 3}) == (None, "no_untold_facts")
    plan, why = le.plan_after_repair(text, before=None, channel="blog", source=SOURCE, switch="on",
                                     opts={"material": "with_background", "shrink_sections": 3})
    assert why == "expand" and plan["material"] == "with_background" and plan["facts"] == []
    assert plan["named"] == ["Vellmar Gorge", "Tessaly"] and len(plan["sections"]) == 2   # not capped by facts


def test_background_needs_a_name_from_the_source():
    text = _told()
    no_moments = FACTS
    assert le.background_names(no_moments) == []
    assert le.plan_after_repair(text, before=None, channel="blog", source=no_moments, switch="on",
                                opts={"material": "with_background", "shrink_sections": 0}) == (None,
                                                                                                "no_untold_facts")


def test_plan_for_routes_a_short_blog_with_no_untold_fact_only_with_background():
    text = _told()
    f4 = qg.gate_extreme_length(qg.strip_citation_tags(text), "blog")
    assert le.plan_for(text, [f4], channel="blog", source=SOURCE, switch="on",
                       opts={"material": "source_facts", "shrink_sections": 3}) is None
    plan = le.plan_for(text, [f4], channel="blog", source=SOURCE, switch="on",
                       opts={"material": "with_background", "shrink_sections": 3})
    assert plan is not None and plan["material"] == "with_background" and plan["only_length"]


BACKGROUND = {
    "The Rim Path": "Limestone canyons like Vellmar Gorge form where a river cuts slowly through soft rock, and the "
                    "Ast river still carries fine pale silt from the walls downstream.",
    "Tessaly": "Markets of this kind have long been where upland shepherd families trade cheese, wool and smoked "
               "fish with the lakeside villages.",
}


def _background_plan():
    plan, why = le.plan_after_repair(_told(), before=None, channel="blog", source=SOURCE, switch="on",
                                     opts={"material": "with_background", "shrink_sections": 3})
    assert why == "expand"
    return plan


def test_background_paragraphs_pass_the_trip_fact_gate():
    text0 = _told()
    ctx = _ctx()
    before = qg.gate_trip_facts(text0, ctx)
    text, _, entry = le.extend(text0, _background_plan(), call=_call(BACKGROUND), source=SOURCE, cta=CTA,
                               trip_ctx=ctx)
    assert entry["added"] == 2 and entry["refused"] == [] and entry["material"] == "with_background"
    after = qg.gate_trip_facts(text, ctx)
    assert [(g["gate"], g["passed"], g["violations"]) for g in after] == \
        [(g["gate"], g["passed"], g["violations"]) for g in before]
    assert all(g["passed"] for g in after)


def test_the_background_call_gets_the_named_list():
    call = _call(BACKGROUND)
    le.extend(_told(), _background_plan(), call=call, source=SOURCE, cta=CTA, trip_ctx=_ctx())
    assert call.seen["background"] == ["Vellmar Gorge", "Tessaly"] and call.seen["facts"] == []


@pytest.mark.parametrize("bad,why", [
    ("You can join a boat trip across the lake from the market and watch the sunset over the water.", "addresses"),
    ("Our guide explains how the gorge was carved by the river through the soft limestone.", "addresses"),
    ("Travellers stop here for lunch at noon before the walk back along the rim to the village.", "new specifics"),
    ("The gorge was first mapped in 1620 by surveyors who followed the river up from the plain.", "new specifics"),
    ("The Orvane monks once kept a small shrine above the gorge where the river bends towards the plain.",
     "names what neither"),
    ("Vellmar Gorge is the oldest canyon in the world and unlike any other gorge anywhere on earth today.",
     "trip fact"),
])
def test_a_background_paragraph_with_a_trip_claim_number_or_new_name_is_refused(bad, why):
    adds = dict(BACKGROUND, **{"Tessaly": bad})
    text, _, entry = le.extend(_told(), _background_plan(), call=_call(adds), source=SOURCE, cta=CTA,
                               trip_ctx=_ctx())
    assert entry["added"] == 1
    (refused,) = entry["refused"]
    assert why in refused["why"] and bad[:30] not in text


def test_the_same_paragraph_is_not_checked_for_reader_address_with_source_facts():
    """source_facts keeps the checks of the repair before S281 (no reader-address / new-name / trip delta check)."""
    _, after = _trial_shape()
    plan, _ = le.plan_after_repair(after, before=None, channel="blog", source=SOURCE, switch="on",
                                   opts={"material": "source_facts", "shrink_sections": 0})
    para = ("You can buy smoked trout and sheep cheese at the market in Tessaly, brought in from the villages "
            "around the lake.")
    head = plan["sections"][0]["heading"]
    _, _, entry = le.extend(after, plan, call=_call({head: para}), source=SOURCE, cta=CTA, trip_ctx=_ctx())
    assert entry["added"] == 1 and "material" not in entry


# ---------------------------------------------------------------- the gateway call

def _resp(content):
    return LLMResponse(content=content, model_used="writer-model", provider="bedrock", input_tokens=900,
                       output_tokens=200, cost_usd=0.003, satellite_account="acc1")


SECS = [{"heading": "Tessaly", "text": "A stop at the market of Tessaly.", "words": 60}]


def test_extend_sections_with_background_uses_the_background_prompt_and_logs_it():
    client = MagicMock()
    client.generate.return_value = _resp(json.dumps({"additions": ["One."]}))
    with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync") as rec:
        generate.extend_sections(sections=SECS, facts=[], background=["Vellmar Gorge", "Tessaly"], tenant_id="t-1")
    req = client.generate.call_args.args[0]
    assert req.system_prompt == generate._EXTEND_BACKGROUND_SYSTEM
    assert "SOURCE FACTS:\n(none left)" in req.user_prompt and "NAMED:\n- Vellmar Gorge\n- Tessaly" in req.user_prompt
    kw = rec.call_args.kwargs
    assert kw["quality_signal"]["material"] == "with_background" and kw["quality_signal"]["named"] == 2
    assert "pricing_mode" in kw                                       # **billed_fields(resp)


def test_extend_sections_without_background_is_the_source_facts_prompt_byte_for_byte():
    client = MagicMock()
    client.generate.return_value = _resp(json.dumps({"additions": ["One."]}))
    with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync") as rec:
        generate.extend_sections(sections=SECS, facts=["A fact sentence."], voice="warm")
    req = client.generate.call_args.args[0]
    assert req.system_prompt == generate._EXTEND_SYSTEM
    assert req.user_prompt == ("SOURCE FACTS:\n1. A fact sentence.\n\nVOICE: warm\n\nSECTIONS:\nSECTION 1 (add about "
                               "60 words)\nHEADING: Tessaly\nTEXT:\nA stop at the market of Tessaly.")
    assert "material" not in rec.call_args.kwargs["quality_signal"]


def test_the_background_prompt_forbids_trip_claims_numbers_and_new_names():
    p = generate._EXTEND_BACKGROUND_SYSTEM
    for rule in ("third person", "never say what travellers do", "No number of any kind", "superlative",
                 "never a place, person, group or thing", "Never add an activity, service, meal, time of day"):
        assert rule in p
