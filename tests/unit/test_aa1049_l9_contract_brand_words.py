"""AA-1049 (S277, lane l9) — two fixes the S1 Master audit found.

1. The stored-row S1 contract (``final_contract_store.record_s1_exit`` at the approve / promote / human_edit exits, the
   report script) checks the tenant's active brand forbidden words, not only the AA core list: one shared resolver
   (``forbidden_words.tenant_brand_forbidden_words`` over ``admin_pipeline._resolve_brand_rule``).
2. ``graph.validate_node`` reads a present-but-None text field as missing instead of crashing on ``None.lower()``.

Invented tours, places and brand words only (CI golden-set text guard)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.content_generation import forbidden_words as fw
from services.content_generation.graph import MISSING_FIELD_CAP, validate_node
from services.quality import final_contract as fc
from services.quality import final_contract_store as store

TENANT = "00000000-0000-4000-8000-00000a104909"
BRAND_WORD = "zorblat"      # an invented word that is on the tenant's brand list only, never on the AA core list
SRC_ITIN = ("Day 1: Arrive in Velmora. Transfer to the inn.\nDay 2: Walk the Karsh gorge.\n"
            "Day 3: Boat to Lake Ondra.\nDay 4: Depart.")
PAGE_ITIN = ("Day 1 — Arrival in Velmora\nSettle in at the inn.\n\nDay 2 — The Karsh gorge\nA long walk.\n\n"
             "Day 3 — Lake Ondra\nA boat crossing.\n\nDay 4 — Departure\nTransfer out.")
META = "A four-day walk through the Karsh gorge and Lake Ondra, with village inns, a boat crossing and a local " \
       "guide on every single day of the long route."


def _row(summary=f"A {BRAND_WORD} walk through the gorge.", *, tenant=TENANT, brand_name=None):
    meta = {"writer_version": "v2", "grounding": {"violations": []}}
    if brand_name is not None:
        meta["brand_name"] = brand_name
    return {"generated_content_id": "g-1", "tour_id": "t-1", "tenant_id": tenant, "status": "approved",
            "aa_name": "Karsh Gorge and Lake Ondra", "aa_subtitle": "Four days on foot", "aa_summary": summary,
            "aa_description": "", "aa_highlights": json.dumps(["The gorge", "The lake", "The inn"]),
            "aa_itineraries": PAGE_ITIN, "seo_title": "Karsh Gorge Walk", "seo_meta": META,
            "src_itineraries": SRC_ITIN, "duration": "4 days", "failure_codes": "[]", "brand_audit_status": "pass",
            "brand_audit_codes": "[]", "fix_pass_applied": False, "human_edited": False, "score_overall": 9.0,
            "metadata": json.dumps(meta), "country": "Zembla"}


def _brand_rule(words):
    return {"id": "b-1", "brand_name": "default", "forbidden_words": words}


def _conn(row):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()
    return conn


def _block(record):
    return [f["code"] for f in record["findings"] if f["severity"] == fc.BLOCK]


# ── 1. the stored-row contract checks the tenant's brand words ─────────────────────────────────────────────────────

def test_row_columns_carry_the_tenant():
    assert "gc.tenant_id::text AS tenant_id" in store.S1_ROW_COLUMNS


def test_a_brand_only_word_passes_without_the_brand_list():
    """The bug: the row holds no brand list, so the core list alone is checked and the brand word passes."""
    rec = store.s1_record_from_row(_row(), "approve")
    assert "FORBIDDEN_WORD" not in _block(rec)
    assert fc.s1_input_from_row(_row()).brand_forbidden_words == []


def test_the_row_input_takes_the_brand_list():
    inp = fc.s1_input_from_row(_row(), brand_forbidden_words=[BRAND_WORD])
    assert inp.brand_forbidden_words == [BRAND_WORD]
    rec = store.s1_record_from_row(_row(), "approve", brand_forbidden_words=[BRAND_WORD.upper()])
    assert "FORBIDDEN_WORD" in _block(rec) and rec["verdict"] == fc.REVIEW


def test_record_s1_exit_checks_the_tenants_active_brand_words():
    """Fails before AA-1049 l9 (record_s1_exit never looked the brand list up), passes after."""
    conn = _conn(_row())
    resolve = AsyncMock(return_value=_brand_rule(json.dumps([BRAND_WORD])))   # jsonb read back as text
    with patch("api.routers.admin_pipeline._resolve_brand_rule", resolve):
        rec = asyncio.run(store.record_s1_exit(conn, "g-1", "approve", actual="approved"))
    assert "FORBIDDEN_WORD" in _block(rec)
    assert rec["verdict"] == fc.REVIEW and rec["agrees"] is False
    resolve.assert_awaited_once_with(conn, TENANT, None, None)
    _, vid, stored, _ = conn.execute.await_args.args
    assert vid == "g-1" and "FORBIDDEN_WORD" in json.dumps(json.loads(stored)["findings"])


def test_record_s1_exit_clean_page_stays_publish_with_the_brand_list():
    conn = _conn(_row(summary="A short walk through the gorge."))
    with patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=_brand_rule([BRAND_WORD]))):
        rec = asyncio.run(store.record_s1_exit(conn, "g-1", "promote", actual="published"))
    assert rec["verdict"] == fc.PUBLISH, rec["findings"]


def test_the_version_brand_name_is_resolved_first_then_the_tenant_default():
    """The brand the version was written under (metadata.brand_name, like re-validation); a name with no active row
    any more falls back to the tenant's default resolution."""
    conn = MagicMock()
    resolve = AsyncMock(side_effect=[None, _brand_rule([BRAND_WORD])])
    with patch("api.routers.admin_pipeline._resolve_brand_rule", resolve):
        words = asyncio.run(store.brand_words_of_row(conn, _row(brand_name="Velmora Trails")))
    assert words == [BRAND_WORD]
    assert [c.args for c in resolve.await_args_list] == [(conn, TENANT, None, "Velmora Trails"),
                                                         (conn, TENANT, None, None)]


def test_no_tenant_means_no_lookup_and_the_core_list():
    resolve = AsyncMock()
    with patch("api.routers.admin_pipeline._resolve_brand_rule", resolve):
        assert asyncio.run(store.brand_words_of_row(MagicMock(), _row(tenant=None))) == []
        assert asyncio.run(fw.tenant_brand_forbidden_words(MagicMock(), "")) == []
    resolve.assert_not_awaited()


def test_no_active_brand_rule_is_the_core_list():
    with patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)):
        assert asyncio.run(fw.tenant_brand_forbidden_words(MagicMock(), TENANT)) == []


def test_a_failed_brand_lookup_still_records_the_contract():
    """Report mode never fails the exit: the lookup error is logged and the core list is checked, as before."""
    conn = _conn(_row(summary="A stunning walk through the gorge."))   # a core-list word
    with patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(side_effect=RuntimeError("db"))):
        rec = asyncio.run(store.record_s1_exit(conn, "g-1", "approve", actual="approved"))
    assert rec is not None and "FORBIDDEN_WORD" in _block(rec)
    conn.execute.assert_awaited_once()


@pytest.mark.parametrize("value,expected", [
    (["Zorblat", "Quindle"], ["Zorblat", "Quindle"]),
    ('["zorblat", "", null, " "]', ["zorblat"]),
    (None, []), ("not json", []), ({"a": 1}, []), ("{}", []),
])
def test_parse_forbidden_words(value, expected):
    assert fw.parse_forbidden_words(value) == expected


def test_brand_words_by_key_looks_each_brand_up_once():
    rows = [_row(), _row(), _row(brand_name="Velmora Trails"), _row(tenant=None)]
    resolve = AsyncMock(return_value=_brand_rule([BRAND_WORD]))
    with patch("api.routers.admin_pipeline._resolve_brand_rule", resolve):
        out = asyncio.run(store.brand_words_by_key(MagicMock(), rows))
    assert out == {(TENANT, None): [BRAND_WORD], (TENANT, "Velmora Trails"): [BRAND_WORD], (None, None): []}
    assert resolve.await_count == 2


def test_report_flags_a_brand_word_when_given_the_brand_list():
    from scripts.quality import final_contract_report as report
    row = {**_row(), "published_tour_id": "p-1"}
    assert report.summarize([row])["publish_flip"]["count"] == 0
    out = report.summarize([row], brand_words={store.brand_key(row): [BRAND_WORD]})
    assert out["publish_flip"]["by_code"] == {"FORBIDDEN_WORD": 1}
    assert BRAND_WORD not in json.dumps(out)    # codes and ids only, never the text


# ── 2. validate_node reads a None text field as missing ────────────────────────────────────────────────────────────

_FULL = {
    "name": "Karsh Gorge and Lake Ondra", "subtitle": "Four days on foot through Velmora",
    "summary": "A grounded walk through the Karsh gorge and its villages, with a boat crossing on Lake Ondra.",
    "highlights": ["Walk the Karsh gorge", "Cross Lake Ondra by boat", "Sleep in Velmora village inns"],
    "itineraries": PAGE_ITIN, "seo_title": "Karsh Gorge Walk", "seo_meta": META,
}


def _state(generated):
    return {"generated": generated, "tour": {"country": "Zembla"}, "seo": {}, "brand_forbidden_words": [],
            "retry_count": 0, "seo_mode": "dataforseo"}


@pytest.mark.parametrize("field", ["subtitle", "summary", "seo_title", "seo_meta", "itineraries", "name"])
def test_validate_reads_a_none_text_field_as_missing(field):
    """``generated['subtitle'] = None`` crashed with ``'NoneType' object has no attribute 'lower'`` before l9."""
    gen = {**_FULL, field: None}
    out = validate_node(_state(gen))
    assert "MISSING_FIELD" in out["failure_codes"]
    assert out["quality_score"] <= MISSING_FIELD_CAP
    assert out["generated"][field] is None    # the stored value is not rewritten


def test_validate_none_text_field_scores_like_a_missing_key():
    for field in ("subtitle", "summary", "seo_title", "seo_meta"):
        none = validate_node(_state({**_FULL, field: None}))
        gone = validate_node(_state({k: v for k, v in _FULL.items() if k != field}))
        assert none["failure_codes"] == gone["failure_codes"], field
        assert none["quality_score"] == gone["quality_score"], field


def test_validate_every_text_field_none_and_no_generated_at_all():
    out = validate_node(_state({k: None for k in _FULL}))
    assert "MISSING_FIELD" in out["failure_codes"] and out["quality_score"] <= MISSING_FIELD_CAP
    out = validate_node({**_state(None), "tour": None})
    assert "MISSING_FIELD" in out["failure_codes"]


def test_validate_highlights_with_a_none_item_does_not_crash():
    out = validate_node(_state({**_FULL, "highlights": ["Walk the Karsh gorge", None, "Cross Lake Ondra"]}))
    assert "HIGHLIGHTS_TOO_FEW" not in out["failure_codes"]


def test_validate_clean_page_is_unchanged():
    out = validate_node(_state(dict(_FULL)))
    assert "MISSING_FIELD" not in out["failure_codes"]
