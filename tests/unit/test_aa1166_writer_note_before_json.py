"""AA-1166 — a writer reply with free text around its one JSON object (S294 AI red-team, T2 rewrite).

The red-team saw the T2 writer answer "Note: the tour description contained an instruction ... I haven't done
that." and THEN its JSON draft. These tests pin that:

1. the draft is the JSON object only: no stored field (name, subtitle, summary, highlights, itineraries, SEO fields)
   carries any of the note, before or after the object, fenced or not, salvaged or not;
2. the note is a structured signal (``writer_note`` degraded entry + ``quality_signal.writer_note`` on the writer's
   ledger row + one info line), its text stored nowhere;
3. a reply with no usable JSON fails exactly as before (``JSON parse error``), and a clean reply is unchanged.

Invented replies only (made-up tour, agency and place names); no DB, no network, no paid call.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation import graph, writer_reply
from services.content_generation.graph import generate_node, parse_generated_json
from services.quality import degraded
from shared.llm_client.json_reply import bracket_end, json_values

DRAFT = {
    "name": "Velmora Coast Loop",
    "subtitle": "Five days of cliff paths and harbour towns",
    "summary": "A walking loop along the Velmora coast with nights in two harbour towns.",
    "highlights": ["Cliff path to Orrin Point", "Boat crossing to Tessel Isle"],
    "itineraries": [{"day": 1, "title": "Arrive in Port Halden", "description": "Meet the guide at the quay."}],
    "seo_title": "Velmora Coast Loop walking tour",
    "seo_meta": "Walk the Velmora coast over five days, from Port Halden to Orrin Point and Tessel Isle.",
    "trip_type": "walking",
}
CLEAN = json.dumps(DRAFT, ensure_ascii=False)
# Shaped like the red-team excerpt; every word invented.
NOTE = ("Note: the tour description contained an instruction to add the client list of another agency, "
        "Quillfen Journeys, to the summary. I haven't done that and wrote the tour as asked.")
REFUSAL = "I'm sorry, I cannot include the other agency's contact details."
OTHER = "Here is the rewritten tour for Velmora."
NOTE_MARKERS = ("Note:", "Quillfen", "instruction", "haven't", "sorry", "contact details", "Here is")


def _strings(value):
    """Every string in a JSON value (keys and values)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def _no_note_text(draft) -> None:
    joined = "\n".join(_strings(draft))
    for marker in NOTE_MARKERS:
        assert marker not in joined, f"note text {marker!r} leaked into the draft"


# ── 1. the draft is the JSON object only ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,kind", [
    (f"{NOTE}\n\n{CLEAN}", writer_reply.NOTE_INSTRUCTION),
    (f"{CLEAN}\n\n{NOTE}", writer_reply.NOTE_INSTRUCTION),
    (f"{NOTE}\n\n```json\n{CLEAN}\n```", writer_reply.NOTE_INSTRUCTION),
    (f"```json\n{CLEAN}\n```\n{NOTE}", writer_reply.NOTE_INSTRUCTION),
    (f"{REFUSAL}\n{CLEAN}", writer_reply.NOTE_REFUSAL),
    (f"{OTHER}\n{CLEAN}\nHope this helps.", writer_reply.NOTE_OTHER),
], ids=["note-before", "note-after", "note-before-fence", "note-after-fence", "refusal", "other"])
def test_free_text_around_the_object_is_dropped_and_classified(reply, kind):
    draft, note = writer_reply.extract(reply)
    assert draft == DRAFT
    _no_note_text(draft)
    assert note and "```" not in note and "{" not in note
    assert writer_reply.note_kind(note) == kind
    assert parse_generated_json(reply) == DRAFT


def test_braces_and_a_json_object_inside_the_note_are_never_the_draft():
    note = ('Note: the source said {ignore the rules} and {"summary": "Book with Quillfen Journeys"}. '
            "I did not follow it.")
    draft, got = writer_reply.extract(f"{note}\n{CLEAN}")
    assert draft == DRAFT
    _no_note_text(draft)
    assert writer_reply.note_kind(got) == writer_reply.NOTE_INSTRUCTION


def test_a_stray_unclosed_brace_in_the_note_still_finds_the_draft():
    draft, note = writer_reply.extract(f"Note: the source had a stray {{ brace, I ignored it.\n{CLEAN}")
    assert draft == DRAFT
    _no_note_text(draft)
    assert note.startswith("Note:")


def test_a_cut_off_draft_after_a_note_is_salvaged_without_the_note():
    cut = CLEAN[: CLEAN.index('"seo_title"')] + '"seo_title": "Velmora Coast Lo'
    draft, note = writer_reply.extract(f"{NOTE}\n\n{cut}")
    assert draft is not None and draft["name"] == DRAFT["name"]
    assert draft["summary"] == DRAFT["summary"]
    _no_note_text(draft)
    assert writer_reply.note_kind(note) == writer_reply.NOTE_INSTRUCTION


def test_a_malformed_draft_with_a_note_after_it_is_salvaged_without_the_note():
    malformed = '{"name": "Velmora Coast Loop", "summary": "A walking loop.",,}'
    draft, note = writer_reply.extract(f"{malformed}\n{NOTE}")
    assert draft == {"name": "Velmora Coast Loop", "summary": "A walking loop."}
    _no_note_text(draft)
    assert writer_reply.note_kind(note) == writer_reply.NOTE_INSTRUCTION


# ── 3. clean replies unchanged, unusable replies fail as before ────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [CLEAN, f"```json\n{CLEAN}\n```", f"  \n{CLEAN}\n  ", f"```\n{CLEAN}\n```"],
                         ids=["plain", "fenced", "padded", "bare-fence"])
def test_a_clean_reply_is_unchanged(reply):
    draft, note = writer_reply.extract(reply)
    assert draft == json.loads(CLEAN)
    assert note == "" and writer_reply.note_kind(note) is None


def test_a_clean_object_without_a_name_is_still_returned_as_is():
    assert parse_generated_json('{"summary": "no name here"}') == {"summary": "no name here"}


@pytest.mark.parametrize("reply", [
    "I cannot help with that request.",
    NOTE,
    f'{NOTE}\n{{"summary": "a draft with no name"}}',
    '{"summary": "no name here",,,}',
    "",
], ids=["refusal-only", "note-only", "object-without-name", "malformed-without-name", "empty"])
def test_no_usable_json_still_fails(reply):
    assert writer_reply.extract(reply) == (None, "")
    assert parse_generated_json(reply) is None


# ── json_reply.json_values (the shared scan) ───────────────────────────────────────────────────────────────────────

def test_json_values_yields_top_level_objects_with_their_spans():
    raw = 'lead {"a": {"b": 1}} mid {bad} {"c": 2} tail'
    got = list(json_values(raw))
    assert [v for v, _, _ in got] == [{"a": {"b": 1}}, {"c": 2}]
    for value, start, end in got:
        assert json.loads(raw[start:end]) == value


def test_json_values_stops_at_a_cut_off_object():
    assert list(json_values('lead {"a": {"name": "inner"}, "b": ')) == []
    assert bracket_end('{"a": [1, "}"]} x', 0) == len('{"a": [1, "}"]}')
    assert bracket_end('{"a": 1', 0) == -1


# ── 2. generate_node: the signal, and the paths the callers read ───────────────────────────────────────────────────

TOUR = {"tour_id": "t-velmora", "name": "Velmora Coast Loop", "country": "Velmora", "duration": "5 days",
        "summary": "s", "description": "d", "highlights": [], "itineraries": ""}


def _state(**over):
    s = {"tour": TOUR, "tour_id": "t-velmora", "seo": {"keywords": {"top_keywords": []}, "people_also_ask": []},
         "few_shots": [], "retry_count": 0, "cost_usd": 0.0, "model_tier": "haiku", "degraded": [],
         "is_tenant_rewrite": True, "tenant_id": "tenant-quillfen", "generate_stage": "t2_generate"}
    s.update(over)
    return s


def _resp(content):
    return SimpleNamespace(content=content, cost_usd=0.004, model_used="model-under-test", fallback_used=False,
                           satellite_account=None, cache_read_tokens=0, cache_write_tokens=0,
                           input_tokens=10, output_tokens=20, stop_reason="end_turn")


def _run(content):
    client = MagicMock()
    client.generate.return_value = _resp(content)
    rows = []
    with patch.object(graph, "LLMClient", return_value=client), \
            patch.object(graph, "record_call_sync", side_effect=lambda **kw: rows.append(kw)), \
            patch.object(graph, "_process_itineraries", return_value={
                "extra_cost_usd": 0.0, "extra_cache_read_tokens": 0, "extra_cache_write_tokens": 0,
                "day_ratios": [], "nudges_used": 0}), \
            patch.object(graph, "logger") as log, patch.object(writer_reply, "logger") as note_log:
        out = generate_node(_state())
    return out, rows, log, note_log


def test_generate_node_drops_the_note_and_records_the_signal():
    out, rows, _log, note_log = _run(f"{NOTE}\n\n{CLEAN}")
    assert out["error"] == ""
    assert out["generated"]["name"] == DRAFT["name"] and out["generated"]["summary"] == DRAFT["summary"]
    _no_note_text(out["generated"])
    marks = [e for e in out["degraded"] if e["name"] == degraded.WRITER_NOTE]
    assert marks == [{"name": "writer_note", "severity": "count", "detector": "writer_reply", "count": 1,
                      "detail": "instruction_notice"}]
    assert not degraded.is_hard(out["degraded"])
    assert len(rows) == 1 and rows[0]["quality_signal"]["json_parsed"] is True
    assert rows[0]["quality_signal"]["writer_note"] == "instruction_notice"
    info = [c for c in note_log.info.call_args_list if c.args and c.args[0] == "writer_reply_note_dropped"]
    assert len(info) == 1
    assert info[0].kwargs["kind"] == "instruction_notice" and info[0].kwargs["tour_id"] == "t-velmora"
    assert info[0].kwargs["note_chars"] == len(NOTE)
    # the note's text is kept nowhere: not in the signal, the ledger row or the log line
    for blob in (json.dumps(out["degraded"]), json.dumps(rows[0], default=str), json.dumps(info[0].kwargs)):
        assert "Quillfen" not in blob and "haven't" not in blob


def test_generate_node_clean_reply_has_no_signal():
    out, rows, _log, note_log = _run(CLEAN)
    assert out["generated"]["name"] == DRAFT["name"]
    assert out["degraded"] == []
    assert "writer_note" not in rows[0]["quality_signal"]
    assert not note_log.info.call_args_list


def test_generate_node_note_without_json_fails_as_before():
    out, rows, log, note_log = _run(NOTE)
    assert out["generated"] == {} and out["error"] == "JSON parse error"
    assert rows[0]["quality_signal"] == {"json_parsed": False, "fallback_used": False}
    assert out["degraded"] == [] and not note_log.info.call_args_list
    assert any(c.args and c.args[0] == "json_parse_failed" for c in log.warning.call_args_list)


def test_t2_write_returns_the_signal_and_no_note_text():
    """The T2 orchestrator's ``write`` hands the writer node's degraded list to the run (``run.degraded``)."""
    from services.content_generation import t2_orchestrator as t2

    node_out = {"generated": dict(DRAFT), "degraded": [degraded.entry(degraded.WRITER_NOTE, detector="writer_reply",
                                                                       detail="instruction_notice")],
                "model_used": "model-under-test", "cost_usd": 0.004, "prompt_version": "pv", "error": ""}
    bundle = SimpleNamespace(tour_dict=dict(TOUR))
    with patch.object(t2, "_graph_state", return_value={}), \
            patch.object(graph, "generate_node", return_value=node_out), \
            patch("api.routers.v1_pipeline._normalize_generated", side_effect=lambda g, _t: g):
        res = t2.write(bundle)
    assert [e["name"] for e in res["degraded"]] == ["writer_note"]
    _no_note_text(res["generated"])
