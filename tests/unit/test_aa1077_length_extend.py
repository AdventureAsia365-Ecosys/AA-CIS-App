"""AA-1077 — the extend repair of a too-short blog (services/acp_content_writing/length_extend.py,
generate.extend_sections, the attempt-2 branch of service.run_write_background).

- plan_for: only a blog whose F4 failure is the SHORT side, with body sections and untold seed facts; the F4 entry is
  set aside so the other failures route as before; the kill switch gives the old path.
- extend: one paragraph per thinnest section, spliced at the section's end (before a closing CTA paragraph); each
  checked in code (plain prose, size, no specific the source lacks, clean seam); a call error keeps the text.
- extend_sections: one gateway call, one llm_call_log row with the billed fields, a bad reply raises.
- the loop: attempt 1 short only → the extend repair, never the full rewrite; F4 + a gate that needs the rewrite →
  the rewrite as before.

Invented places only (Vellmar Gorge, Lake Orrin, Tessaly). The model is always mocked.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import generate, length_contract as lc, length_extend as le
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service
from services.quality import registry as qr
from shared.llm_client.models import LLMResponse

CTA = "Write to us to plan your days at Lake Orrin."
SOURCE = (
    "Vellmar Gorge is a limestone canyon cut by the Ast river over many thousands of years.\n"
    "The shepherd huts on the ridge are built from flat river stones without any mortar.\n"
    "Lake Orrin freezes from the northern shore first and the ice stays until late spring.\n"
    "Tessaly market sells smoked trout and sheep cheese from the villages around the lake.\n"
)
INTRO = "Vellmar Gorge rewards a slow walk."
GORGE = ("The trail climbs above the gorge through pine and juniper. The river noise fades as the path turns towards "
         "the ridge.")
LAKE = "Lake Orrin lies still below the ridge. Its shore is quiet in the morning."
MARKET = "Tessaly sits at the end of the lake road. Its stalls open early."
TEXT = (f"{INTRO}\n\n## Above Vellmar Gorge\n\n{GORGE}\n\n## Lake Orrin\n\n{LAKE}\n\n## Tessaly\n\n{MARKET}\n\n{CTA}"
        "\n\n## FAQ\n\n**Q: Where is Tessaly?**\nA: Tessaly sits at the end of the lake road.\n")


def _short_ledger(extra=()):
    f4 = qg.gate_extreme_length(qg.strip_citation_tags(TEXT), "blog")
    assert not f4["passed"]
    return [f4, {"gate": qr.F6_CTA_PRESENT, "passed": True, "violations": [], "blocking": True}, *extra]


@pytest.fixture(autouse=True)
def _extend_on(monkeypatch):
    """The extend repair ships off (= main); these tests measure it switched on."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")


def test_the_default_is_main(monkeypatch):
    monkeypatch.delenv(le.EXTEND_ENV)
    assert le.mode() == "off" and le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE) is None
    monkeypatch.setenv(le.EXTEND_ENV, "yes")
    assert le.mode() == "off"


# ---------------------------------------------------------------- sections / plan

def test_sections_are_the_body_headings_before_the_faq():
    secs = le.sections(TEXT)
    assert [s["heading"] for s in secs] == ["Above Vellmar Gorge", "Lake Orrin", "Tessaly"]
    assert secs[1]["text"] == LAKE and secs[1]["chars"] == len(LAKE)


def test_plan_picks_the_thinnest_sections_and_sets_f4_aside():
    plan = le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE)
    assert plan is not None and plan["only_length"]
    assert [s["heading"] for s in plan["sections"]] == ["Above Vellmar Gorge", "Lake Orrin", "Tessaly"]
    assert all(le.WORDS_MIN <= s["words"] <= le.WORDS_MAX for s in plan["sections"])
    assert plan["deficit_chars"] == lc.extend_target_chars() - lc.measure(TEXT)
    f4 = next(g for g in plan["ledger"] if g["gate"] == qr.F4_EXTREME_LENGTH)
    assert f4["passed"] and f4["set_aside"] == le.EXTEND_MODE
    assert any("Tessaly market sells smoked trout" in f for f in plan["facts"])


def test_plan_reports_other_failures():
    other = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
             "violations": ["x: 'Its stalls open early.'"]}
    plan = le.plan_for(TEXT, _short_ledger([other]), channel="blog", source=SOURCE)
    assert plan is not None and not plan["only_length"]


@pytest.mark.parametrize("case", ["not_blog", "long", "no_facts", "off", "passed", "old_unreadable"])
def test_plan_is_none_and_the_old_path_runs(case, monkeypatch):
    ledger, channel, source = _short_ledger(), "blog", SOURCE
    if case == "not_blog":
        channel = "linkedin"
    elif case == "long":
        ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True,
                   "violations": ["content is 14000 chars — far beyond any real channel example"]}]
    elif case == "no_facts":
        source = ""
    elif case == "off":
        monkeypatch.setenv(le.EXTEND_ENV, "off")
    elif case == "passed":
        ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": []}]
    else:
        ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True,
                   "violations": ["F4_extreme_length violation"]}]
    assert le.plan_for(TEXT, ledger, channel=channel, source=source) is None


def test_an_old_short_wording_still_plans():
    ledger = [{"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True,
               "violations": ["content is only 500 chars — effectively empty"]}]
    assert le.plan_for(TEXT, ledger, channel="blog", source=SOURCE) is not None


# ---------------------------------------------------------------- extend

def _plan():
    return le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE)


GOOD = {
    "Above Vellmar Gorge": "The canyon below was cut by the Ast river over many thousands of years, and the shepherd "
                           "huts on the ridge are built from flat river stones laid without mortar.",
    "Lake Orrin": "In the cold months the lake freezes from its northern shore first, and the ice stays until late "
                  "spring.",
    "Tessaly": "The market sells smoked trout and sheep cheese brought in from the villages around the lake.",
}


def _call(adds, cost=0.012, model="writer-model"):
    def call(*, sections, facts, voice=None, **kw):
        call.seen = {"sections": sections, "facts": facts, "voice": voice, **kw}
        return [adds.get(s["heading"], "") if isinstance(adds, dict) else adds[i] for i, s in enumerate(sections)], \
            cost, model
    return call


def test_extend_adds_one_paragraph_per_section_and_keeps_the_rest_byte_for_byte():
    text, cost, entry = le.extend(TEXT, _plan(), call=_call(GOOD), source=SOURCE, cta=CTA)
    assert cost == 0.012 and entry["added"] == 3 and entry["refused"] == [] and entry["model"] == "writer-model"
    for para in (INTRO, GORGE, LAKE, MARKET, CTA, "## FAQ"):
        assert para in text
    assert text.index(GOOD["Lake Orrin"]) < text.index("## Tessaly")
    # the closing CTA paragraph stays last in its section
    assert text.index(GOOD["Tessaly"]) < text.index(CTA) < text.index("## FAQ")
    assert entry["chars_after"] == lc.measure(text) > entry["chars_before"]


@pytest.mark.parametrize("bad,why", [
    ("The huts sleep 12 walkers on 3 floors.", "new specifics"),
    ("## A new heading\nThe lake is still.", "not plain prose"),
    ("Why does the lake freeze so early in the year?", "not plain prose"),
    ("- the huts\n- the river", "not plain prose"),
    ("The gorge is wide. " * 200, "words for"),
    ("", "empty"),
    (CTA, "call to action"),
])
def test_extend_refuses_a_paragraph_that_breaks_a_rule(bad, why):
    adds = dict(GOOD, **{"Lake Orrin": bad})
    text, _, entry = le.extend(TEXT, _plan(), call=_call(adds), source=SOURCE, cta=CTA)
    assert entry["added"] == 2
    (refused,) = entry["refused"]
    assert refused["section"] == 1 and why in refused["why"]
    assert GOOD["Lake Orrin"] not in text and (not bad.strip() or bad.strip()[:30] not in text.replace(CTA, ""))


def test_a_paragraph_that_repeats_the_section_is_refused_at_the_seam():
    adds = dict(GOOD, **{"Lake Orrin": LAKE})
    text, _, entry = le.extend(TEXT, _plan(), call=_call(adds), source=SOURCE, cta=CTA)
    assert any(r["why"].startswith("seam") for r in entry["refused"]) and text.count(LAKE) == 1


def test_a_call_error_keeps_the_text_and_costs_nothing():
    def boom(**kw):
        raise generate.ExtendRepairError("bad extend reply")
    text, cost, entry = le.extend(TEXT, _plan(), call=boom, source=SOURCE, cta=CTA)
    assert (text, cost) == (TEXT, 0.0) and "ExtendRepairError" in entry["error"]


# ---------------------------------------------------------------- the gateway call

def _resp(content, **kw):
    return LLMResponse(content=content, model_used="writer-model", provider="bedrock", input_tokens=900,
                       output_tokens=200, cost_usd=0.011, satellite_account="acc3", **kw)


def _sections():
    return [{"heading": "Lake Orrin", "text": LAKE, "words": 60}, {"heading": "Tessaly", "text": MARKET, "words": 60}]


def test_extend_sections_is_one_logged_call_with_the_billed_fields():
    client = MagicMock()
    client.generate.return_value = _resp(json.dumps({"additions": ["One.", "Two."]}))
    with patch.object(generate, "LLMClient", return_value=client), \
            patch.object(generate, "record_call_sync") as rec:
        adds, cost, model = generate.extend_sections(sections=_sections(), facts=["A fact sentence."],
                                                     voice="warm", tenant_id="t-1", content_piece_id="p-1")
    assert adds == ["One.", "Two."] and model == "writer-model" and cost == pytest.approx(0.011)
    req = client.generate.call_args.args[0]
    assert req.stage == "t9_write" and req.system_prompt == generate._EXTEND_SYSTEM
    assert "SECTION 2 (add about 60 words)" in req.user_prompt and "VOICE: warm" in req.user_prompt
    kw = rec.call_args.kwargs
    assert kw["role"] == "writer" and kw["quality_signal"]["mode"] == "length_extend"
    assert kw["content_piece_id"] == "p-1" and "pricing_mode" in kw


def test_extend_sections_raises_on_a_reply_with_the_wrong_shape():
    client = MagicMock()
    client.generate.return_value = _resp(json.dumps({"additions": ["Only one."]}))
    with patch.object(generate, "LLMClient", return_value=client), patch.object(generate, "record_call_sync"):
        with pytest.raises(generate.ExtendRepairError):
            generate.extend_sections(sections=_sections(), facts=["A fact."])


# ---------------------------------------------------------------- the loop

REQUEST_ID, PIECE_ID = uuid.uuid4(), uuid.uuid4()


def _ctx():
    return {"atom_text": SOURCE, "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA",
                                          "marketing_term": "AIDA"},
            "channel_style": {"key": "blog", "tone": "warm"}, "brand_audience": {"voice": ["warm", "plain"]},
            "chosen": {"idx": 0, "name": "A", "why_it_works": "w", "formula_fit": "AIDA",
                       "best_final_style": "warm", "recommended": True, "chosen": True},
            "cta": CTA, "destination": None, "trip_name": None, "brand_rubric_text": "rubric", "channel": "blog",
            "atom_id": "atom_x"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.fixture
def _loop_stubs():
    with patch.object(service.hygiene, "ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch.object(service.text_integrity, "final_pass", side_effect=lambda text, hyg, **kw: (text, [], [])), \
            patch.object(service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda seo, kw, **k: (seo, 0.0))), \
            patch.object(service, "_enforce_faq_contract",
                         new=AsyncMock(side_effect=lambda text, **k: (text, 0.0, []))), \
            patch.object(service, "_apply_faq_answer_fit",
                         new=AsyncMock(side_effect=lambda text, flags, **k: (text, flags, 0.0))):
        yield


@pytest.mark.asyncio
async def test_a_short_only_attempt_one_gets_the_extend_repair_not_the_rewrite(_loop_stubs):
    passing = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": [], "blocking": True}]
    with patch.object(service, "write_content", return_value=(TEXT, 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "extend_sections", side_effect=_call(GOOD)) as ext, \
            patch.object(service, "run_quality_gates", side_effect=[_outcome(_short_ledger()), _outcome(passing)]), \
            patch.object(service, "_recheck_length", side_effect=lambda t, led, st, hr, trace: (led, st, hr, {})), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    rewrite.assert_not_called()
    ext.assert_called_once()
    assert ext.call_args.kwargs["voice"] == "warm, plain" and ext.call_args.kwargs["content_piece_id"] == str(PIECE_ID)
    kw = fin.call_args.kwargs
    assert kw["status"] == "approved" and sum(p in kw["content_text"] for p in GOOD.values()) >= 2
    entry = next(e for e in kw["repair_log"] if e.get("repair_mode") == le.EXTEND_MODE)
    ext_log = entry["length_extend"]
    assert ext_log["added"] == ext_log["sections"] >= 2 and ext_log["refused"] == [] and ext_log["cost_usd"] == 0.012


@pytest.mark.asyncio
async def test_f4_short_with_a_gate_that_needs_the_rewrite_takes_the_rewrite(_loop_stubs):
    cta_fail = {"gate": qr.F6_CTA_PRESENT, "passed": False, "blocking": True, "repairable": True,
                "violations": ["the CTA is missing"]}
    a1 = [_short_ledger()[0], cta_fail]
    passing = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": [], "blocking": True}]
    with patch.object(service, "write_content", return_value=(TEXT, 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=(TEXT + "\nMore.", 0.05, {}, None)) as rw, \
            patch.object(service, "extend_sections") as ext, \
            patch.object(service, "run_quality_gates", side_effect=[_outcome(a1), _outcome(passing)]), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})):
        await service.run_write_background(REQUEST_ID, PIECE_ID, _ctx(), pool=MagicMock())
    rw.assert_called_once()
    ext.assert_not_called()
