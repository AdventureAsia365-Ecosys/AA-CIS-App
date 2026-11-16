"""S282 Sentry triage (AA-CIS-API-18) — a blog reply holding more than one top-level JSON value still parses.

json-repair turns "envelope + a second object" (or a stray ``===SUMMARY===`` line with braces) into a LIST, and the
envelope check refused it as "missing/empty body" although the body was there. The parser now takes the first dict
of the list that has a non-empty body, with a warning; the sibling T9 parsers take the first dict with their key.
A reply with no usable dict still raises, and the error now says why. Invented text only; no model is called.
"""
from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from services.acp_content_writing import generate as gen

ENVELOPE = {"seo_title": "Lantern Road Walk in Velmora", "meta_description": "A slow walk on the lantern road.",
            "slug": "lantern-road-walk", "body": "Picture the lantern road at dusk."}


def test_envelope_followed_by_a_second_object_is_salvaged_with_a_warning():
    raw = json.dumps(ENVELOPE) + '\n{"note": "a second object the model added"}'
    with capture_logs() as logs:
        body, meta, summary = gen._parse_blog_envelope(raw)
    assert body == ENVELOPE["body"] and meta["slug"] == "lantern-road-walk" and summary is None
    warn = [e for e in logs if e["event"] == "t9_reply_json_salvaged"]
    assert len(warn) == 1 and warn[0]["log_level"] == "warning"
    assert warn[0]["parser"] == "blog_envelope" and warn[0]["repaired_type"] == "list" and warn[0]["items"] == 2


def test_envelope_followed_by_a_summary_marker_with_braces_is_salvaged():
    raw = json.dumps(ENVELOPE) + "\n===SUMMARY===\n{a short summary}"
    body, _meta, _summary = gen._parse_blog_envelope(raw)
    assert body == ENVELOPE["body"]


def test_the_first_dict_with_a_body_wins_over_an_earlier_dict_without_one():
    raw = '{"note": "a preface object"}\n' + json.dumps(ENVELOPE)
    assert gen._parse_blog_envelope(raw)[0] == ENVELOPE["body"]


def test_a_list_with_no_body_still_raises_and_says_why():
    raw = '{"seo_title": "T"}\n{"note": "no body anywhere"}'
    with pytest.raises(gen.SeoEnvelopeError, match=r"repaired_type=list, \d+ chars"):
        gen._parse_blog_envelope(raw)


def test_a_clean_envelope_with_no_body_raises_no_body():
    with pytest.raises(gen.SeoEnvelopeError, match=r"\(no_body, "):
        gen._parse_blog_envelope(json.dumps({"seo_title": "T", "body": "  "}))


def test_a_clean_envelope_logs_nothing():
    with capture_logs() as logs:
        assert gen._parse_blog_envelope(json.dumps(ENVELOPE))[0] == ENVELOPE["body"]
    assert logs == []


@pytest.mark.parametrize("data, key, picked", [
    ([{"x": 1}, {"replacements": ["a"]}], "replacements", {"replacements": ["a"]}),
    ([{"additions": ["b"]}, {"additions": ["c"]}], "additions", {"additions": ["b"]}),
    ([["loose"], {"faq": []}], "faq", {"faq": []}),
])
def test_salvage_takes_the_first_dict_the_parser_wants(data, key, picked):
    assert gen._salvage_list(data, lambda d: key in d, parser="p", raw="r") == picked


@pytest.mark.parametrize("data", [{"replacements": []}, "text", None, [{"other": 1}], []])
def test_salvage_leaves_anything_else_to_the_callers_own_check(data):
    assert gen._salvage_list(data, lambda d: "replacements" in d, parser="p", raw="r") == data


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, level", [
    (gen.SeoEnvelopeError("Could not parse blog JSON envelope (no_body, 2 chars)"), "warning"),
    (RuntimeError("provider throttled"), "error"),
])
async def test_background_write_logs_an_envelope_miss_as_a_warning(exc, level):
    """The piece still ends 'failed' with the reason; only the log level of a model output miss changes."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from services.acp_content_writing import service
    from tests.unit.test_aa450_content_writing_service import PIECE_ID, REQUEST_ID, _context, _finalized_row

    with patch.object(service, "write_content", side_effect=exc), \
         patch.object(service, "_finalize_piece",
                      new=AsyncMock(return_value=_finalized_row(status="failed"))) as fin, \
         capture_logs() as logs:
        await service.run_write_background(REQUEST_ID, PIECE_ID, _context(), pool=MagicMock())
    assert fin.call_args.kwargs["status"] == "failed"
    assert type(exc).__name__ in fin.call_args.kwargs["held_reason"]
    failed = [e for e in logs if e["event"] == "t9_write_background_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == level
