"""AA-776 — an F4-only ledger (SEO title/meta out of band) is the SEO fit's: sentence_repair.plan
returns [] (repair_mode 'seo_fields', no rewrite call) instead of None (full Sonnet rewrite). S234:
3 F4-only blogs took a full rewrite at attempt 1 and 5 were held at attempt 2 without the
attempt-3 fit. A slug violation still rewrites — the fit never writes a new slug."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service
from services.acp_content_writing.quality_gates import gate_seo_surface

TEXT = "The lake is cold and very deep in the north. Book with us."
KW = "Khovsgol and gobi tour mongolia price"


def _f4(*violations: str) -> dict:
    return {"gate": "F4_seo_surface", "passed": False, "blocking": True, "repairable": True,
            "violations": list(violations)}


# The S234 F4-only ledgers (s234_fr_dump.json repair_log), title/meta only.
F4_TITLE_META = _f4(f"SEO title does not contain the keyword '{KW}'",
                    "meta description is 164 characters — outside 120-158")


@pytest.mark.parametrize("violations", [
    [f"SEO title does not contain the keyword '{KW}'"],
    ["meta description is 160 characters — outside 120-158"],
    ["no SEO title — the search headline is not optional", "meta description is not a complete sentence"],
    ["SEO title is the same as an earlier piece of this brand — search results would show two identical headlines"],
])
def test_title_meta_only_f4_is_the_fits(violations):
    assert sr.plan(TEXT, [_f4(*violations)]) == []


@pytest.mark.parametrize("slug", [None, "mongolia-gobi-khövsgöl", "a-" * 60 + "b"])
def test_slug_violation_still_needs_the_rewrite(slug):
    """The violation strings come from the real gate, so a reworded slug message breaks this test."""
    f4 = gate_seo_surface("Lake Khovsgol in Winter: A Field Guide to the Ice", "x" * 130 + ".", slug, None)
    assert f4["violations"] and all(v.startswith(("no slug", "slug ")) for v in f4["violations"])
    assert sr.plan(TEXT, [f4]) is None
    assert sr.plan(TEXT, [f4, F4_TITLE_META]) is None


def test_slug_with_title_only_f12_still_rewrites():
    f12 = {"gate": "F12_scope", "passed": False, "blocking": True,
           "violations": ["title names 'Gobi', outside this piece's scope: 'Gobi Guide'"]}
    assert sr.plan(TEXT, [f12, F4_TITLE_META]) == []
    assert sr.plan(TEXT, [f12, _f4("no slug")]) is None


def test_non_repairable_gate_with_f4_still_rewrites():
    f3 = {"gate": "F3_structure", "passed": False, "blocking": True, "violations": ["no H2"]}
    assert sr.plan(TEXT, [F4_TITLE_META, f3]) is None


def test_contract_gate_alone_unchanged_and_with_f4_takes_the_fit():
    f16 = {"gate": "F16_faq", "passed": False, "blocking": True, "repairable": False,
           "violations": ["FAQ has 2 pairs — the blog needs 4"]}
    assert sr.plan(TEXT, [f16]) is None  # alone it holds at once (AA-768 round 2)
    assert sr.plan(TEXT, [F4_TITLE_META, f16]) == []  # F4 is the fit's; F16 holds after it if still short


def test_non_blocking_f4_is_ignored():
    assert sr.plan(TEXT, [{**F4_TITLE_META, "blocking": False}]) is None


def _ctx():
    return {"atom_text": "seed", "goal": {"key": "promotion"}, "channel_style": {"channel": "linkedin"},
            "brand_audience": {}, "chosen": {}, "cta": "Book", "destination": None, "trip_name": None,
            "brand_rubric_text": "", "channel": "linkedin", "atom_id": "a1"}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


@pytest.mark.asyncio
async def test_attempt_one_f4_only_takes_the_seo_fields_mode_not_a_rewrite():
    row = {"piece_id": str(uuid4()), "attempt_number": 2, "status": "approved"}
    with patch.object(service, "write_content", return_value=(TEXT, 0.01, {}, None)), \
         patch.object(service, "repair_sentences") as rep, \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "run_quality_gates", side_effect=[_outcome([F4_TITLE_META]), _outcome([])]) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rw.assert_not_called()
    rep.assert_not_called()
    assert gates.call_args_list[1].kwargs["content_text"] == gates.call_args_list[0].kwargs["content_text"]
    assert fin.call_args.kwargs["repair_log"][0]["repair_mode"] == "seo_fields"


@pytest.mark.asyncio
async def test_attempt_two_f4_only_is_not_the_last_try():
    """Before AA-776 plan() was None at attempt 2, so last_try held the piece without the attempt-3 fit."""
    row = {"piece_id": str(uuid4()), "attempt_number": 3, "status": "approved"}
    with patch.object(service, "write_content", return_value=(TEXT, 0.01, {}, None)), \
         patch.object(service, "rewrite_with_feedback") as rw, \
         patch.object(service, "run_quality_gates",
                      side_effect=[_outcome([F4_TITLE_META]), _outcome([F4_TITLE_META]), _outcome([])]) as gates, \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rw.assert_not_called()
    assert gates.call_count == 3
    log = fin.call_args.kwargs["repair_log"]
    assert [e.get("repair_mode") for e in log] == ["seo_fields", "seo_fields"]
    assert fin.call_args.kwargs["status"] == "approved"


@pytest.mark.asyncio
async def test_slug_only_f4_still_takes_the_full_rewrite():
    row = {"piece_id": str(uuid4()), "attempt_number": 2, "status": "approved"}
    with patch.object(service, "write_content", return_value=(TEXT, 0.01, {}, None)), \
         patch.object(service, "rewrite_with_feedback", return_value=(TEXT, 0.01, {}, None)) as rw, \
         patch.object(service, "run_quality_gates", side_effect=[_outcome([_f4("no slug")]), _outcome([])]), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=row)):
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rw.assert_called_once()
