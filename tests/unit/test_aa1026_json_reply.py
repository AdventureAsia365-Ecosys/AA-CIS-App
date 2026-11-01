"""AA-1026 — the stage parsers of the acc2 contract smoke read the reply's FIRST complete JSON value.

The S263 live smoke on Dev passed 11 of 12 stages; `t9_faq_filter` failed on both acc2 models:
  nova-lite     'invalid JSON: Extra data: line 7 column 6 (char 134)'
  llama4-scout  'invalid JSON: Extra data: line 1 column 89 (char 88)'
Both numbers are exactly what the old parser (first "{" to last "}", then json.loads) says about a BARE list of
verdict items: Nova Lite indented it by 4 (json.dumps(indent=4)), Llama 4 Scout wrote it on one line; the slice
starts at the first item, so json.loads sees item 1 and then ", {...}" as extra data. The tests below rebuild both
replies, check that the old parser fails on them with those exact messages, and that the new one reads them.

The other parsers of the smoke CASES move onto the same helper (shared/llm_client/json_reply.py): faq_answer_fit,
places.days, places.locate, the atomize reply, and (before their json_repair salvage) source_facts and place_cards.
Invented replies only; no DB, no network, no paid call.
"""
from __future__ import annotations

import json
import re

import pytest

from services.acp_content_writing import faq_answer_fit as fit
from services.acp_content_writing import faq_filter as ff
from services.acp_shared import place_cards
from services.acp_shared.atom_extraction import atoms_from_reply
from services.content_generation import source_facts
from services.places import days, locate
from shared.llm_client.json_reply import JsonReplyError, first_json_value

# The smoke's three questions; item 1 is the one whose length fixes the error position (empty note).
ITEMS = [{"n": 1, "label": "KEEP", "reason_code": "on_subject", "confidence": "high", "note": ""},
         {"n": 2, "label": "KEEP", "reason_code": "on_subject", "confidence": "medium", "note": "hut comfort"},
         {"n": 3, "label": "DROP", "reason_code": "word_meaning", "confidence": "high", "note": "name as a word"}]
NOVA_LIKE = json.dumps(ITEMS, indent=4)
LLAMA_LIKE = json.dumps(ITEMS)


def _old_parse(content: str):
    """The parser before AA-1026 (faq_filter / faq_answer_fit): first "{" to last "}", then json.loads."""
    m = re.search(r"\{.*\}", (content or "").strip(), re.S)
    return json.loads(m.group(0))


# ── the two observed failures, reproduced ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,error", [
    (NOVA_LIKE, "Extra data: line 7 column 6 (char 134)"),
    (LLAMA_LIKE, "Extra data: line 1 column 89 (char 88)"),
], ids=["nova-lite", "llama4-scout"])
def test_the_old_parser_failed_the_acc2_replies_with_the_observed_errors(reply, error):
    with pytest.raises(json.JSONDecodeError) as e:
        _old_parse(reply)
    assert str(e.value) == error


@pytest.mark.parametrize("reply", [NOVA_LIKE, LLAMA_LIKE], ids=["nova-lite", "llama4-scout"])
def test_faq_filter_reads_the_acc2_replies(reply):
    out = ff.parse_verdicts(reply, 3)
    assert [v["label"] for v in out] == ["KEEP", "KEEP", "DROP"]
    assert [v["reason_code"] for v in out] == ["on_subject", "on_subject", "word_meaning"]


@pytest.mark.parametrize("reply", [
    '{"items": ' + LLAMA_LIKE + '}\n\nNote: question 3 asks about the {name}, not the place.',     # trailing prose
    '{"items": ' + LLAMA_LIKE + '}\n{"items": []}',                                               # a second value
    "```json\n" + NOVA_LIKE + "\n```\nI judged each question on its own.",                        # fence + note
    "Verdicts for {3} questions:\n" + LLAMA_LIKE,                                                 # brace in prose
])
def test_faq_filter_ignores_text_around_the_first_value(reply):
    assert [v["label"] for v in ff.parse_verdicts(reply, 3)] == ["KEEP", "KEEP", "DROP"]


@pytest.mark.parametrize("reply,err", [
    (LLAMA_LIKE[:-40], "invalid JSON"),                                       # cut off: never an inner item
    (json.dumps(ITEMS[:2]), "no verdict for question"),                        # one answer missing
    (json.dumps([dict(ITEMS[0], label="MAYBE")] + ITEMS[1:]), "bad item"),     # item checks unchanged
    (json.dumps(["KEEP", "KEEP", "DROP"]), "bad item"),
    ('{"verdicts": ' + LLAMA_LIKE + "}", "no items"),
])
def test_faq_filter_item_checks_are_not_loosened(reply, err):
    with pytest.raises(ff.FaqFilterReplyError, match=err):
        ff.parse_verdicts(reply, 3)


def test_the_smoke_passes_the_faq_filter_on_both_reply_shapes():
    from shared.llm_client.models import LLMResponse
    from services.jobs import llm_contract_smoke_job as smoke

    for reply in (NOVA_LIKE, LLAMA_LIKE):
        def call(_request, content=reply):
            return LLMResponse(content=content, model_used="nova-lite", provider="bedrock-native")
        assert smoke.CASES["t9_faq_filter"].run(call) == {"labels": ["KEEP", "KEEP", "DROP"]}


# ── the other parsers of the smoke CASES ───────────────────────────────────────────────────────────────────────────

FIT_ITEMS = [{"n": 1, "verdict": "ANSWERS", "reason": ""}, {"n": 2, "verdict": "ANSWERS", "reason": ""}]


@pytest.mark.parametrize("reply", [json.dumps(FIT_ITEMS, indent=4), json.dumps(FIT_ITEMS),
                                   '{"items": ' + json.dumps(FIT_ITEMS) + '} Both answers fit.'])
def test_faq_answer_fit_reads_the_list_and_ignores_trailing_text(reply):
    assert [v["verdict"] for v in fit.parse_fit_verdicts(reply, 2)] == ["ANSWERS", "ANSWERS"]


def test_faq_answer_fit_item_checks_are_not_loosened():
    with pytest.raises(fit.FitReplyError, match="bad item"):
        fit.parse_fit_verdicts(json.dumps([{"n": 1, "verdict": "PARTLY"}, FIT_ITEMS[1]]), 2)


DAYS = {"days": [{"day": 1, "start": "Kestrel Bay", "end": "Tamsin Lodge", "overnight": "Tamsin Lodge"}]}


@pytest.mark.parametrize("reply", [
    json.dumps(DAYS) + "\nThe overnight on day 1 is the {lodge} named in the text.",
    "Here is the JSON:\n```json\n" + json.dumps(DAYS, indent=2) + "\n```",
    json.dumps(DAYS["days"]) + "\n" + json.dumps(DAYS["days"]),
])
def test_place_days_reads_the_first_value(reply):
    assert list(days.parse_llm_days(reply)) == [1]


def test_place_days_still_refuses_a_reply_without_json():
    with pytest.raises(ValueError):
        days.parse_llm_days("Day 1 starts in Kestrel Bay.")


def test_atomize_reply_reads_the_first_object_and_keeps_its_shape_checks():
    body = {"atoms": [{"place": "Varro hut", "action": "sleep"}]}
    assert atoms_from_reply(json.dumps(body) + "\nI kept {one} atom.") == body["atoms"]
    assert atoms_from_reply("```json\n" + json.dumps(body) + "\n```") == body["atoms"]
    with pytest.raises(KeyError):
        atoms_from_reply('{"not_atoms": []}')
    with pytest.raises(TypeError):
        atoms_from_reply('{"atoms": "none"}')
    with pytest.raises(ValueError):
        atoms_from_reply('{"atoms": [{"place": "Varro')


def test_locate_reads_the_first_object_after_a_second_value():
    reply = '{"Varro hut": [12.34, 98.76, "Thailand"]}\n{"Varro hut": [0, 0, "Laos"]}'
    assert locate.parse_answer(reply, ["Varro hut"]) == {"Varro hut": locate.Proposal(12.34, 98.76, "Thailand")}


@pytest.mark.parametrize("parse", [source_facts._parse_json, place_cards._parse_json])
def test_repair_based_parsers_read_the_first_object_before_any_repair(parse):
    obj = {"days": [{"day": 1, "facts": ["Walk 12 km"]}], "display_name": "Varro hut"}
    # json_repair alone turns "object + note holding an object" into a list, which these parsers read as nothing
    assert parse(json.dumps(obj) + '\nNote: {"day": 2} has no facts.') == obj
    # a reply json_repair can fix still goes to json_repair (a trailing comma, a cut-off reply)
    assert parse('{"days": [{"day": 1, "facts": ["Walk 12 km"]},]}') == {"days": [{"day": 1, "facts": ["Walk 12 km"]}]}
    assert parse('{"display_name": "Varro hut", "facts": ["The hut')["display_name"] == "Varro hut"


def test_the_t10_adjudicator_reads_the_bare_items_list_with_a_trailing_note():
    from services.acp_content_writing.seed_align import adjudicate as adj
    items = [{"n": 1, "verdict": "OK", "piece_quote": "", "seed_quote": "", "action": "repair"}]
    assert adj.parse_verdicts(json.dumps(items) + "\nNo {issue} found.", 1)[0]["verdict"] == "OK"
    with pytest.raises(adj.AdjudicateReplyError, match="bad verdict"):
        adj.parse_verdicts(json.dumps([dict(items[0], verdict="MAYBE")]), 1)


def test_the_t10_judge_reader_takes_the_first_value_unrepaired():
    from shared.llm_client.judge import parse_judge_json_checked
    verdict = {"status": "pass", "failure_codes": []}
    # json_repair alone turned "verdict + note holding an object" into a list, and the reader raised
    assert parse_judge_json_checked(json.dumps(verdict) + '\nNote: {"x": 1} was not checked.') == (verdict, False)
    # a cut-off verdict is still salvaged by json_repair (and marked repaired)
    assert parse_judge_json_checked('{"status": "flagged", "failure_codes": [')[1] is True


def test_the_brand_fit_judge_reads_the_first_value_and_still_refuses_a_list():
    from unittest.mock import MagicMock

    from services.content_generation import brand_fit
    from shared.llm_client.models import LLMResponse

    def client(content):
        c = MagicMock()
        c.generate.return_value = LLMResponse(content=content, model_used="nova-lite", provider="bedrock-native")
        return c
    reply = '{"brand_fit_score": 8, "cross_brand_distinct": 7, "feedback": "calm"}\nI scored {two} axes.'
    out = brand_fit.score_candidate_fit({}, {"place": "Varro hut"}, client=client(reply))
    assert (out.brand_fit_score, out.cross_brand_distinct) == (8.0, 7.0)
    with pytest.raises(ValueError, match="not an object"):
        brand_fit.score_candidate_fit({}, {"place": "Varro hut"}, client=client("[8, 7]"))


# ── the helper ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,kinds,expected", [
    ('{"a": 1}', (dict,), {"a": 1}),
    ('  {"a": 1}  \n', (dict,), {"a": 1}),
    ('```json\n{"a": 1}\n```', (dict,), {"a": 1}),
    ('Sure. {"a": 1} and {"b": 2}', (dict,), {"a": 1}),
    ('I think {maybe} this: {"a": {"b": [1, 2]}} done', (dict,), {"a": {"b": [1, 2]}}),
    ('Note "{quoted}" first: {"a": "x}y"}', (dict,), {"a": "x}y"}),
    ('[1, 2] then {"a": 1}', (dict,), {"a": 1}),
    ('[1, 2] then {"a": 1}', (dict, list), [1, 2]),
    ('Days [one to three]: [{"day": 1}]', (dict, list), [{"day": 1}]),
])
def test_first_json_value(raw, kinds, expected):
    assert first_json_value(raw, kinds=kinds) == expected


@pytest.mark.parametrize("raw,match", [
    (None, "no JSON object"),
    ("", "no JSON object"),
    ("No JSON at all.", "no JSON object"),
    ("[1, 2]", "no JSON object"),                                          # a list where an object is wanted
    ('{"items": [{"n": 1}, {"n": 2}, {"n": 3', "invalid JSON"),             # cut off: no nested object comes back
    ('{"a": 1,}', "invalid JSON"),
])
def test_first_json_value_refusals(raw, match):
    with pytest.raises(JsonReplyError, match=match):
        first_json_value(raw)


def test_scan_off_tries_the_first_bracket_only():
    assert first_json_value('{"a": 1} {"b": 2}', scan=False) == {"a": 1}
    with pytest.raises(JsonReplyError, match="invalid JSON"):
        first_json_value('For {n} items: {"a": 1}', scan=False)
    assert first_json_value('For {n} items: {"a": 1}') == {"a": 1}


def test_json_reply_error_is_a_value_error():
    # callers that catch ValueError (place days, the smoke, a3 atomize) keep catching the parse failure
    assert issubclass(JsonReplyError, ValueError)


def test_no_smoke_parser_slices_first_brace_to_last_brace():
    """The old slice is what failed live; the smoke CASES parsers must not bring it back."""
    import inspect
    for module in (ff, fit, days, locate):
        assert 're.search(r"\\{.*\\}"' not in inspect.getsource(module), module.__name__
