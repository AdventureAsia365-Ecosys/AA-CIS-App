"""AA-1077 (S281, CIS 422) — the two causes of paid trial 5's length hold, each behind a setting.

Trial 5: a repair round shrank a blog from above the floor to under it, then the lengthening repair asked two sections
and the writer left both empty ("no fitting fact"): the repair may only retell untold source facts.

- cis.t9.blog_extend_shrink_sections (default 3): after a repair round that SHRANK the blog under the floor, the plan
  asks that many sections (each another chance that a paragraph fits), the same words shared between them; 0 = the
  deficit alone (the plan before S281).
- cis.t9.blog_extend_material (default source_facts = the repair before S281): with_background lets a paragraph give
  general background about what the post or its source names, never a trip claim. Each background paragraph is
  checked in code: no reader / group address, no traveller / trip / service wording, no new name, no number or time
  the source lacks, no new F17 trip-fact or F18 rarity finding on the spliced text; the gates then run on the whole
  text as for any attempt.
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
    "- Lake Orrin is fed by snowmelt from the plateau and its water stays cold through the year.\n"
    "- The market in Tessaly sells smoked trout and sheep cheese from the villages around the lake.\n"
    "- The rim path is lined with old juniper trees bent low by the wind from the plateau.\n"
)
SOURCE = SEED + FACTS

# Distinct invented sentences per section, made of invented words that never repeat (so the writing loop's own
# near-duplicate and echo checks leave them alone, as they would a real post).
SYL = ["ba", "ko", "ri", "tu", "le", "mo", "sa", "ve", "di", "na", "po", "gu"]
VERBS = ["shades", "faces", "guards", "borders", "hides", "frames", "crowns", "shelters", "flanks", "overlooks",
         "fronts", "screens"]
HEADS = ["The Rim Path", "Above the Ast", "Tessaly Lanes", "The Lake Road", "Stone Huts"]
PER_SECTION = 15


def _word(i: int) -> str:
    return SYL[i % 12] + SYL[(i // 12) % 12] + SYL[(i // 144 + 3) % 12] + "n"


def _sentences(k: int, n: int = PER_SECTION) -> list[str]:
    out = []
    for j in range(n):
        b = (k * 16 + j) * 5
        out.append(f"The {_word(b)} {_word(b + 1)} {VERBS[(k + j) % 12]} a {_word(b + 2)} {_word(b + 3)} near the "
                   f"{_word(b + 4)}.")
    return out


def _post(cut: int = 0) -> str:
    """A blog over the floor (before the repair); `cut` drops that many sentences of 'Above the Ast' (the repair)."""
    secs = []
    for k, head in enumerate(HEADS):
        sents = _sentences(k)
        if head == "Above the Ast" and cut:
            sents = sents[:-cut]
        secs.append(f"## {head}\n\n" + " ".join(sents[:7]) + "\n\n" + " ".join(sents[7:]))
    intro = "Vellmar Gorge rewards a slow walk. Tessaly waits at the end of the lake road."
    return (f"{intro}\n\n" + "\n\n".join(secs) + f"\n\n{CTA}\n\n## FAQ\n\n**Q: Where is Tessaly?**\n"
            "A: Tessaly sits at the end of the lake road.\n")


def _trial_shape() -> tuple[str, str]:
    """(before, after): the repair took a blog over the floor to one just under it, the deficit to the target being
    the 81-160 words for which the deficit alone asks TWO sections (trial 5: 4,988 → 4,669 chars)."""
    before = _post()
    for cut in range(1, PER_SECTION):
        after = _post(cut)
        if lc.measure(after) < lc.floor_chars():
            return before, after
    raise AssertionError("fixture never went under the floor")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
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
    ("You can join the shepherd families at the market and taste the smoked trout from the lake.", "addresses"),
    ("Our guide explains how the gorge was carved by the river through the soft limestone.", "addresses"),
    ("Travellers stop here for lunch at noon before the walk back along the rim to the village.", "new specifics"),
    ("The gorge was first mapped in 1620 by surveyors who followed the river up from the plain.", "new specifics"),
    ("The Orvane monks once kept a small shrine above the gorge where the river bends towards the plain.",
     "names what neither"),
    ("Vellmar Gorge is the oldest canyon in the world and unlike any other gorge anywhere on earth today.",
     "trip fact"),
    # S281 review: trip activity / service claims in the third person, which no gate of the loop catches in code
    ("Visitors can take a boat trip across the lake to the shepherd huts and enjoy a picnic lunch on the shore.",
     "speaks of the trip"),
    ("Travellers on this walk visit the shepherd families and taste the sheep cheese at a private tasting session.",
     "speaks of the trip"),
    ("A private guide leads the walk along the rim and explains how the river carved the soft pale limestone.",
     "speaks of the trip"),
    ("At the market one can sample the sheep cheese and smoked trout brought down from the lake villages.",
     "speaks of the trip"),
])
def test_a_background_paragraph_with_a_trip_claim_number_or_new_name_is_refused(bad, why):
    adds = dict(BACKGROUND, **{"Tessaly": bad})
    text, _, entry = le.extend(_told(), _background_plan(), call=_call(adds), source=SOURCE, cta=CTA,
                               trip_ctx=_ctx())
    assert entry["added"] == 1
    (refused,) = entry["refused"]
    assert why in refused["why"] and bad[:30] not in text


@pytest.mark.parametrize("ok", [
    "Shepherd families on the plateau have long moved their flocks to the high meadows when the snow melts.",
    "The Ast river carries fine pale silt from the gorge walls, which is why its water looks milky after rain.",
    "Markets of this kind grew where upland villages traded wool and cheese for fish from the lakeside.",
])
def test_plain_place_background_is_not_read_as_a_trip_claim(ok):
    assert le._TRIP_RE.search(ok) is None and le._READER_RE.search(ok) is None


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
                 "never a place, person, group or thing", "Never add an activity, service, meal, time of day",
                 "a guide or the trip, tour or walk the subject", "never name a meal, picnic, tasting"):
        assert rule in p


# ---------------------------------------------------------------- the loop: the trial 5 shape end to end

def _loop_ctx():
    from tests.unit.test_aa1077_s278_round2 import _ctx as round2_ctx
    return {**round2_ctx(), "atom_text": SOURCE, "cta": CTA}


def _cut_sentences(before: str, after: str) -> list[str]:
    return [s for s in _sentences(HEADS.index("Above the Ast")) if s in before and s not in after]


def _loop_gates(calls: list, cuts: list[str]):
    """run_quality_gates stand-in: attempt 1 = F4 in band + F17 quoting the sentences the repair will cut; later = F4
    measured on the real text."""
    from tests.unit.test_aa1077_s278_round2 import _f17

    def run(**kw):
        calls.append(kw["content_text"])
        ledger = [{**qg.gate_extreme_length(qg.strip_citation_tags(kw["content_text"]), "blog"), "blocking": True}]
        if len(calls) == 1:
            ledger.append({"gate": qg.qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
                           "violations": [_f17(c) for c in cuts]})
        first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
        return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("shrink,status", [(3, "approved"), (0, "held")])
async def test_the_loop_re_extends_a_blog_its_repair_shrank_under_the_floor(monkeypatch, shrink, status):
    """Trial 5 in the loop: attempt 1 is over the floor with F17 findings; the sentence repair cuts them and leaves the
    blog under the floor; the expand then runs on what is left. The writer has nothing for the two sections the
    deficit alone asks (empty, as in the trial) and a fact paragraph for one more: with the shrink setting (3) that
    section is asked and the piece is approved over the floor; with 0 it is held on length (the trial's outcome)."""
    import uuid
    from unittest.mock import AsyncMock

    from services.acp_content_writing import service

    before, after = _trial_shape()
    cuts = _cut_sentences(before, after)
    assert cuts
    monkeypatch.setattr(le.trial_flags, "active", lambda: {le.EXTEND_FLAG: "on"})   # CIS 424: no env path
    _values(monkeypatch, shrink__platform=shrink)
    deficit_plan, _ = le.plan_after_repair(after, before=before, channel="blog", source=SOURCE, switch="on",
                                           opts={"material": "source_facts", "shrink_sections": 0})
    deficit_heads = {s["heading"] for s in deficit_plan["sections"]}
    para = ("The shepherd huts on the ridge are built from flat river stones laid without any mortar, and the market "
            "in Tessaly sells smoked trout and sheep cheese brought in from the villages around the lake.")
    seen: list = []

    def ext(*, sections, facts, voice=None, **kw):
        seen.append([s["heading"] for s in sections])
        return [("" if s["heading"] in deficit_heads else para) for s in sections], 0.004, "writer-model"

    calls: list[str] = []
    with patch.object(service.hygiene, "ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch.object(service.text_integrity, "final_pass", side_effect=lambda text, hyg, **kw: (text, [], [])), \
            patch.object(service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda seo, kw, **k: (seo, 0.0))), \
            patch.object(service, "_enforce_faq_contract",
                         new=AsyncMock(side_effect=lambda text, **k: (text, 0.0, []))), \
            patch.object(service, "_apply_faq_answer_fit",
                         new=AsyncMock(side_effect=lambda text, flags, **k: (text, flags, 0.0))), \
            patch.object(service, "_recheck_length", side_effect=lambda t, led, st, hr, **kw: (led, st, hr, {})), \
            patch.object(service, "write_content", return_value=(before, 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "repair_sentences", return_value=([""] * len(cuts), 0.01)), \
            patch.object(service, "extend_sections", side_effect=ext), \
            patch.object(service, "run_quality_gates", side_effect=_loop_gates(calls, cuts)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(uuid.UUID("00000000-0000-4000-8000-000000001281"),
                                           uuid.UUID("00000000-0000-4000-8000-000000002281"), _loop_ctx(),
                                           pool=MagicMock())
    rewrite.assert_not_called()
    kw = fin.call_args.kwargs
    entry = next(e for e in kw["repair_log"] if e.get("length_net"))
    net = entry["length_net"]
    assert net["before"] >= lc.floor_chars() > net["after_repair"]               # the repair shrank it under
    assert len(seen) == 1 and len(seen[0]) == (3 if shrink else 2)
    assert kw["status"] == status
    if shrink:
        assert entry[le.EXTEND_MODE]["added"] == 1 and entry[le.EXTEND_MODE]["shrunk"] is True
        assert net["after_expand"] >= lc.floor_chars() and not net["shrank_under_floor"]
        assert para in kw["content_text"] and not any(c in kw["content_text"] for c in cuts)
    else:
        assert entry[le.EXTEND_MODE]["added"] == 0 and net["shrank_under_floor"]


# ---------------------------------------------------------------- the $0 extend-plan replay (scripts/aa1077)

def test_the_replay_shows_what_background_and_the_shrink_plan_unblock():
    """A short piece with no untold fact: no_untold_facts with source facts, expand with background; a piece whose own
    repair round shrank it under the floor is replayed with the shrink sections."""
    from scripts.aa1077 import extend_plan_replay as rp
    told = _told()
    f4 = qg.gate_extreme_length(qg.strip_citation_tags(told), "blog")
    row = {"piece_id": "aaaaaaaa-1", "tenant": "audit-x", "status": "held", "held_reason": None, "content_text": told,
           "gate_ledger": [f4], "repair_log": [], "writer_input": {"seed": SOURCE}}
    dry = rp.replay_piece(row, source_of=lambda wi, r: SOURCE, shrink=3)
    assert dry["plan"] == "no_untold_facts" and dry["plan_background"] == "expand"
    assert dry["names_offered_background"] == 2 and dry["facts_offered_background"] == 0
    before, after = _trial_shape()
    shrunk_row = {**row, "piece_id": "bbbbbbbb-2", "content_text": after,
                  "gate_ledger": [qg.gate_extreme_length(qg.strip_citation_tags(after), "blog")],
                  "repair_log": [{"attempt": 1}, {"attempt": 2, "length_net": {"shrank_under_floor": True}}]}
    shrunk = rp.replay_piece(shrunk_row, source_of=lambda wi, r: SOURCE, shrink=3)
    plain = rp.replay_piece(shrunk_row, source_of=lambda wi, r: SOURCE, shrink=0)
    assert shrunk["shrunk_by_repair"] and shrunk["sections"] == 3 and plain["sections"] == 2
    s = rp.summarize([dry, shrunk])
    assert s["no_material"] == ["aaaaaaaa"] and s["no_material_background"] == []
    assert s["reachable_background"] == ["aaaaaaaa", "bbbbbbbb"] and s["shrunk_by_repair"] == ["bbbbbbbb"]
