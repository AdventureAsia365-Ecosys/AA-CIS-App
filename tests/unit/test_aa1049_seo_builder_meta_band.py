"""AA-1049 W3 follow-up (S273 trial, tech lead decision "keep-off: fix the builder first") — with T9_SEO_BUILDER=on a
meta under the policy band floor (trial: 119 chars, band 140-155) must not survive the builder and the one fit call:

- code first: the meta (or a whole-sentence prefix of it) completed by ONE whole post sentence that cites a source line
  of this piece ([R:<atom or moment id>] / [F:<fact id>], tag removed), accepted only through the caller's predicates;
- else one fit call that states the band (`meta_band`), its answer re-checked through the same predicates and builder;
- else the meta stays out of band, a warning names why, and F4_seo_surface holds it (as on main).

The default (switch off) is main: no completion, no `meta_band`, the fit and its one retry. Invented places only."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import generate, service
from services.acp_content_writing import seo_builder as sb
from services.acp_content_writing.quality_gates import gate_seo_surface
from services.quality import writing_policy as wp

META_LO, META_HI = wp.band("meta")
ATOM = "atom_orv01"
FACT = "fact_7"
FACTS_TEXT = f"[Fact id={FACT}]\nBoat sheds: The boat sheds at Dunmere mend nets by hand."
LAKE = "The lake path leaves Orvine at the old ferry steps and runs north under the pines."     # 82
CORRAN = "Corran Monastery opens its library hall to walkers."                                   # 51
SHEDS = "The boat sheds at Dunmere mend nets by hand."                                           # 44
BODY = (
    "# Walking the Orvine Lake Shore\n\n"
    f"{LAKE} [R:{ATOM}]\n\n"
    f"It climbs gently for the first hour. [R:{ATOM}]\n\n"
    f"{CORRAN} [R:{ATOM}] Some say the view is the best in the region.\n\n"
    f"The ferry runs twice a day from the far shore. [R:atom_zz9]\n\n"
    f"{SHEDS} [F:{FACT}]\n\n"
    f"- Bring water and a light jacket. [R:{ATOM}]\n\n"
    "## FAQ\n\n"
    "**Q: How long is the walk?**\n"
    f"A: The lake path takes about three hours. [R:{ATOM}]\n"
)
CONTEXT = {"atom_id": ATOM, "atom_text": f"{LAKE} {CORRAN}", "facts_text": FACTS_TEXT}
KEYWORD = "orvine lake walk"
TITLE = "Orvine Lake Walk to Corran Monastery and Its Library"                                  # in the title band
SHORT_105 = ("Walk the Orvine lake walk from the old ferry steps to Corran Monastery and its library hall "
             "above the bay.")
SHORT_92 = "Walk the Orvine lake walk from the old ferry steps to Corran Monastery and its library hall."
SHORT_121 = ("Walk the Orvine lake walk from the old ferry steps, past quiet coves and pines, to Corran Monastery "
             "and its library hall.")


def _meta_ok(v):
    return isinstance(v, str) and META_LO <= len(v) <= META_HI and v.endswith((".", "!", "?"))


def _title_ok(v):
    return True


def test_lengths_of_the_fixture():
    assert 100 <= len(SHORT_105) <= 130 and 100 <= len(SHORT_121) <= 130 and len(SHORT_92) < META_LO
    assert (len(LAKE), len(CORRAN), len(SHEDS)) == (82, 51, 44)


# ── source ids and cited sentences ────────────────────────────────────────────────────────────────────────────────

def test_source_ids_are_the_atom_the_route_moments_and_the_facts():
    ctx = {"atom_id": ATOM, "route_segments": [("atom_r1", "seed one"), ["atom_r2", "seed two"]],
           "facts_text": FACTS_TEXT + "\n\n[Fact id=f-12]\nPier: The pier."}
    assert sb.source_ids(ctx) == {ATOM, "atom_r1", "atom_r2", FACT, "f-12"}
    assert sb.source_ids({}) == frozenset() and sb.source_ids(None) == frozenset()


def test_cited_sentences_are_tagged_prose_sentences_of_this_piece_only():
    got = sb.cited_sentences(BODY, sb.source_ids(CONTEXT))
    # not: the heading, "It climbs" (leans on the sentence before), the untagged sentence, the unknown atom id, the
    # bullet, the FAQ answer
    assert got == [LAKE, CORRAN, SHEDS]
    assert sb.cited_sentences(BODY, []) == []                                    # no known id: nothing is anchored
    assert sb.cited_sentences(BODY, {FACT}) == [SHEDS]
    assert sb.cited_sentences(None, {ATOM}) == []


def test_a_question_or_a_lowercase_start_never_completes_a_meta():
    body = f"Is the lake path steep? [R:{ATOM}]\n\nthe sheds mend nets by hand. [R:{ATOM}]\n\n{SHEDS} [R:{ATOM}]"
    assert sb.cited_sentences(body, {ATOM}) == [SHEDS]


def test_meta_extensions_whole_meta_first_then_its_prefixes():
    meta = SHORT_92 + " Then rest."
    ext = sb.meta_extensions(meta, [CORRAN, SHEDS])
    assert ext[:2] == [f"{meta} {CORRAN}", f"{meta} {SHEDS}"]
    assert ext[2:] == [f"{SHORT_92} {CORRAN}", f"{SHORT_92} {SHEDS}"]
    assert sb.meta_extensions("No end mark here", [SHEDS]) == []                 # a base must end a sentence
    assert sb.meta_extensions(f"Walk on. {SHEDS}", [SHEDS]) == []                 # never the same sentence twice
    assert sb.meta_extensions(None, [SHEDS]) == []


# ── build_seo: short, in band, long ───────────────────────────────────────────────────────────────────────────────

def test_a_short_meta_is_completed_by_the_first_cited_sentence_that_lands_in_the_band():
    sources = sb.cited_sentences(BODY, sb.source_ids(CONTEXT))
    out, log = sb.build_seo({"meta_description": SHORT_105}, title_ok=_title_ok, meta_ok=_meta_ok,
                            meta_sources=sources)
    # LAKE (82) and CORRAN (51) overshoot 155; SHEDS (44) lands at 151
    assert out["meta_description"] == f"{SHORT_105} {SHEDS}" and META_LO <= len(out["meta_description"]) <= META_HI
    assert log == [f"meta_description completed in code to 151 chars (was {len(SHORT_105)}) with one cited sentence "
                   "of the post"]


def test_a_short_meta_with_no_cited_sentence_that_fits_is_left_for_the_fit():
    sources = sb.cited_sentences(BODY, sb.source_ids(CONTEXT))
    out, log = sb.build_seo({"meta_description": SHORT_121}, title_ok=_title_ok, meta_ok=_meta_ok,
                            meta_sources=sources)
    assert out["meta_description"] == SHORT_121 and log == []
    out, log = sb.build_seo({"meta_description": SHORT_105}, title_ok=_title_ok, meta_ok=_meta_ok)
    assert out["meta_description"] == SHORT_105 and log == []                     # no sources: nothing added


def test_an_in_band_meta_is_never_touched():
    meta = f"{SHORT_92} {CORRAN}"
    assert META_LO <= len(meta) <= META_HI
    out, log = sb.build_seo({"meta_description": meta}, title_ok=_title_ok, meta_ok=_meta_ok, meta_sources=[SHEDS])
    assert out["meta_description"] == meta and log == []


def test_a_long_meta_is_shortened_first_and_a_too_short_prefix_is_completed():
    in_band = f"{SHORT_92} {CORRAN}"
    out, _ = sb.build_seo({"meta_description": in_band + " Then take the ferry back."}, title_ok=_title_ok,
                          meta_ok=_meta_ok, meta_sources=[SHEDS])
    assert out["meta_description"] == in_band                                     # the prefix wins over a completion
    long_meta = SHORT_92 + " Then follow the shore past the boat sheds and the pier to the far coves and back."
    assert len(long_meta) > META_HI
    out, log = sb.build_seo({"meta_description": long_meta}, title_ok=_title_ok, meta_ok=_meta_ok,
                            meta_sources=[LAKE, CORRAN])
    assert out["meta_description"] == f"{SHORT_92} {CORRAN}" and "completed" in log[0]


def test_the_caller_predicate_still_decides_a_completion():
    def meta_ok(v):
        return _meta_ok(v) and "Dunmere" not in v                                 # e.g. a place outside the scope
    out, log = sb.build_seo({"meta_description": SHORT_105}, title_ok=_title_ok, meta_ok=meta_ok,
                            meta_sources=[LAKE, CORRAN, SHEDS])
    assert out["meta_description"] == SHORT_105 and log == []


# ── service._fit_seo_meta end to end (real band and predicates) ───────────────────────────────────────────────────

def _run(meta, monkeypatch, mode, fit_return=None, body=BODY, context=None):
    if mode is None:
        monkeypatch.delenv(service.SEO_BUILDER_ENV, raising=False)
    else:
        monkeypatch.setenv(service.SEO_BUILDER_ENV, mode)
    kw = {"return_value": fit_return} if fit_return is not None else {"side_effect": RuntimeError("fit outage")}
    with patch.object(service, "fit_seo_fields", **kw) as fit:
        out, cost = asyncio.run(service._fit_seo_meta({"seo_title": TITLE, "meta_description": meta}, KEYWORD,
                                                      body=body, context=dict(context or CONTEXT),
                                                      request_id=uuid4()))
    return out, cost, fit


def test_switch_on_a_short_meta_ships_completed_with_no_fit_call(monkeypatch):
    assert service._seo_title_ok(TITLE, KEYWORD)
    out, cost, fit = _run(SHORT_105, monkeypatch, "on")
    assert fit.call_count == 0 and cost == 0.0
    assert out["meta_description"] == f"{SHORT_105} {SHEDS}"
    assert gate_seo_surface(out["seo_title"], out["meta_description"], "orvine-lake-walk", KEYWORD)["passed"]


def test_switch_off_is_main_no_completion_and_no_band_in_the_request(monkeypatch):
    out, cost, fit = _run(SHORT_105, monkeypatch, None)
    assert fit.call_count == 1 and out["meta_description"] == SHORT_105           # fit outage: the writer's own
    assert "meta_band" not in fit.call_args.kwargs
    still_short = ({"seo_title": None, "meta_description": SHORT_121}, 0.001)
    out, cost, fit = _run(SHORT_105, monkeypatch, None, fit_return=still_short)
    assert fit.call_count == 2 and all("meta_band" not in c.kwargs for c in fit.call_args_list)   # main's retry
    assert out["meta_description"] == SHORT_105


def test_switch_on_the_one_fit_call_states_the_band_and_its_answer_is_rechecked(monkeypatch):
    in_band = ("Walk the Orvine lake walk from the old ferry steps, past quiet coves and pines, to Corran Monastery "
               "and its library hall for the long afternoon.")
    assert META_LO <= len(in_band) <= META_HI
    out, cost, fit = _run(SHORT_121, monkeypatch, "on", fit_return=({"seo_title": None, "meta_description": in_band},
                                                                      0.002))
    assert fit.call_count == 1 and fit.call_args.kwargs["meta_band"] == (META_LO, META_HI)
    assert out["meta_description"] == in_band and cost == 0.002


def test_switch_on_a_short_fitted_answer_is_completed_by_the_builder(monkeypatch):
    out, _, fit = _run(SHORT_121, monkeypatch, "on", fit_return=({"seo_title": None, "meta_description": SHORT_105},
                                                                   0.002))
    assert fit.call_count == 1 and out["meta_description"] == f"{SHORT_105} {SHEDS}"


def test_switch_on_still_short_after_the_fit_holds_on_f4_with_a_reason(monkeypatch):
    with patch.object(service.logger, "warning") as warn:
        out, _, fit = _run(SHORT_121, monkeypatch, "on",
                           fit_return=({"seo_title": None, "meta_description": SHORT_121}, 0.002))
    assert fit.call_count == 1 and out["meta_description"] == SHORT_121           # no second call per attempt
    event, kw = warn.call_args.args[0], warn.call_args.kwargs
    assert event == "t9_seo_meta_outside_band" and kw["chars"] == len(SHORT_121) and kw["band_stated"] is True
    assert kw["band"] == [META_LO, META_HI] and "F4_seo_surface holds it" in kw["reason"]
    f4 = gate_seo_surface(out["seo_title"], out["meta_description"], "orvine-lake-walk", KEYWORD)
    assert not f4["passed"] and any(f"meta description is {len(SHORT_121)} characters" in v for v in f4["violations"])


def test_switch_on_a_long_meta_gets_no_band_line(monkeypatch):
    long_meta = SHORT_121 + " Then take the ferry back across the bay to the old steps."
    out, _, fit = _run(long_meta, monkeypatch, "on")
    assert fit.call_count == 1 and "meta_band" not in fit.call_args.kwargs       # over the band: the aim works


def test_switch_on_without_source_ids_nothing_is_completed(monkeypatch):
    out, _, fit = _run(SHORT_105, monkeypatch, "on", context={"atom_text": LAKE})
    assert fit.call_count == 1 and out["meta_description"] == SHORT_105
    assert fit.call_args.kwargs["meta_band"] == (META_LO, META_HI)


# ── generate.fit_seo_fields: the band line is in the user prompt only when asked ──────────────────────────────────

def _llm(content):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="haiku", cost_usd=0.001, input_tokens=10,
                                           output_tokens=10, stop_reason="end_turn", satellite_account="acc3",
                                           fallback_used=False)
    return fake


@pytest.mark.parametrize("band", [None, (META_LO, META_HI)])
def test_fit_seo_fields_states_the_band_only_when_asked(band):
    reply = '{"seo_title": "T", "meta_description": "M."}'
    kw = {"meta_band": band} if band else {}
    with patch.object(generate, "LLMClient", return_value=_llm(reply)) as m, \
            patch.object(generate, "record_call_sync"):
        generate.fit_seo_fields(body="b", seo_title=TITLE, meta_description=SHORT_121, keyword=KEYWORD, **kw)
    req = m.return_value.generate.call_args.args[0]
    assert req.system_prompt == generate._SEO_FIT_SYSTEM                          # the registered prompt, unchanged
    line = f"META LENGTH FOR THIS POST: {META_LO}-{META_HI} characters, never under {META_LO}."
    assert (line in req.user_prompt) is bool(band)
    assert f"CURRENT META ({len(SHORT_121)} chars — too short: must be {META_LO}-{META_HI})" in req.user_prompt
