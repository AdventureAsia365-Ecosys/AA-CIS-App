"""AA-728 — Review Queue: per-row failure_class + retryable + hint, and the endpoint row shape.

`_review_failure_class` is a pure classifier that decides whether Regenerate (a retry on the same
harness) can fix a held row. These tests pin every class, the first-match ordering (raw thin beats
needs_human; a Sonnet-retried row beats a plain hard code; transient detection), and that the
endpoint attaches the 3 fields to each row.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from api.routers import admin_pipeline as ap


# ── Pure classifier: one test per class ──────────────────────────────────────
def test_raw_insufficient_when_source_too_thin():
    c = ap._review_failure_class(8.0, [], [], "pass", "low_quality", src_itinerary_chars=120)
    assert c["failure_class"] == "raw_insufficient"
    assert c["retryable"] is False
    assert "thin" in c["hint"].lower()


def test_needs_human_for_fact_check():
    c = ap._review_failure_class(8.0, ["FACT_CHECK_MANUAL_CHECK"], [], "manual_check",
                                 "codes=FACT_CHECK_MANUAL_CHECK", src_itinerary_chars=2000)
    assert c["failure_class"] == "needs_human"
    assert c["retryable"] is False
    assert "person" in c["hint"].lower()


def test_needs_human_for_brand_manual_check_without_code():
    c = ap._review_failure_class(8.0, [], [], "manual_check", "brand_audit=manual_check",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "needs_human"
    assert c["retryable"] is False


def test_writer_tone_when_sonnet_already_retried():
    c = ap._review_failure_class(6.5, ["FORBIDDEN_WORD"],
                                 [{"code": "FORBIDDEN_WORD", "field": "aa_summary"}], "flagged",
                                 "retried with sonnet, still FORBIDDEN_WORD in aa_summary",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "writer_tone"
    assert c["retryable"] is False
    assert "sonnet" in c["hint"].lower()


def test_transient_timeout():
    c = ap._review_failure_class(0.0, [], [], None, "model call failed: Read timeout after 60s",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "transient"
    assert c["retryable"] is True


def test_transient_throttling_and_429():
    for summary in ("ThrottlingException from Bedrock", "HTTP 429 Too Many Requests",
                    "503 ServiceUnavailable", "upstream 500 error"):
        c = ap._review_failure_class(0.0, [], [], None, summary, src_itinerary_chars=2000)
        assert c["failure_class"] == "transient", summary
        assert c["retryable"] is True


def test_hard_code_not_yet_retried_is_retryable():
    c = ap._review_failure_class(8.0, ["FORBIDDEN_WORD"],
                                 [{"code": "FORBIDDEN_WORD", "field": "aa_summary"}], "flagged",
                                 "codes=FORBIDDEN_WORD", src_itinerary_chars=2000)
    assert c["failure_class"] == "hard"
    assert c["retryable"] is True
    assert "sonnet" in c["hint"].lower()


def test_low_quality_when_no_hard_code():
    c = ap._review_failure_class(6.0, [], [], "pass", "low_quality(score=6.0<7.0)",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "low_quality"
    assert c["retryable"] is True


def test_other_fallback():
    c = ap._review_failure_class(7.5, ["DFS_INTENT_UNDERUSED"], [], "pass",
                                 "codes=DFS_INTENT_UNDERUSED", src_itinerary_chars=2000)
    assert c["failure_class"] == "other"
    assert c["retryable"] is True


# ── Ordering: first match wins ───────────────────────────────────────────────
def test_raw_insufficient_beats_needs_human():
    # A thin source AND a manual-check code: raw_insufficient wins (fixing the source comes first).
    c = ap._review_failure_class(8.0, ["FACT_CHECK_MANUAL_CHECK"], [], "manual_check",
                                 "codes=FACT_CHECK_MANUAL_CHECK", src_itinerary_chars=100)
    assert c["failure_class"] == "raw_insufficient"


def test_sonnet_retried_beats_hard():
    # A surviving hard code that was already retried with Sonnet is writer_tone, not hard.
    c = ap._review_failure_class(8.0, ["FORBIDDEN_WORD"],
                                 [{"code": "FORBIDDEN_WORD", "field": "aa_summary"}], "flagged",
                                 "retried with sonnet, still FORBIDDEN_WORD in aa_summary",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "writer_tone"
    assert c["retryable"] is False


def test_needs_human_beats_writer_tone_and_hard():
    # Manual-check code present alongside a hard code + sonnet text → needs_human wins.
    c = ap._review_failure_class(8.0, ["FACT_CHECK_MANUAL_CHECK", "FORBIDDEN_WORD"], [],
                                 "manual_check", "retried with sonnet, still FORBIDDEN_WORD",
                                 src_itinerary_chars=2000)
    assert c["failure_class"] == "needs_human"


def test_none_src_chars_does_not_trigger_raw_insufficient():
    # A null length (no raw row joined) must not be treated as "< 500".
    c = ap._review_failure_class(6.0, [], [], "pass", "low_quality", src_itinerary_chars=None)
    assert c["failure_class"] == "low_quality"


# ── Endpoint row shape carries the 3 fields ──────────────────────────────────
def _row(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tour_id": "22222222-2222-2222-2222-222222222222",
        "generated_content_id": "33333333-3333-3333-3333-333333333333",
        "review_status": "pending", "score_overall": 6.0,
        "failure_summary": "low_quality(score=6.0<7.0)", "created_at": None,
        "aa_name": "Tour", "aa_subtitle": "", "aa_summary": "", "aa_description": "",
        "aa_highlights": None, "aa_itineraries": "", "mobile_card_text": "",
        "seo_title": "", "seo_meta": "", "seo_keywords_used": None, "og_tags": {},
        "human_edited": False, "reviewed_by": None, "edited_at": None,
        "revalidate_passed": None, "requested_tier": None, "status": "hitl", "version_num": 1,
        "failure_codes": None, "brand_audit_codes": None, "brand_audit_status": "pass",
        "src_name": "Raw", "country": "India", "duration": 7, "raw_tours_batch_id": None,
        "src_itinerary_chars": 2000,
    }
    base.update(over)
    return base


def _run(rows, **query):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[rows, [{"country": "India", "n": len(rows)}]])
    conn.fetchval = AsyncMock(side_effect=[len(rows), '[]'])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    request = MagicMock()
    request.app.state.pool.acquire = MagicMock(return_value=cm)
    with patch.object(ap, "verify_admin_secret", lambda _s: None):
        return asyncio.run(ap.admin_review_queue(request, x_admin_secret="x", **query))


def test_endpoint_row_includes_failure_class_fields():
    out = _run([_row()])
    row = out["data"][0]
    assert set(("failure_class", "retryable", "hint")) <= set(row.keys())
    assert row["failure_class"] == "low_quality"
    assert row["retryable"] is True
    assert isinstance(row["hint"], str) and row["hint"]


def test_endpoint_marks_thin_source_not_retryable():
    out = _run([_row(src_itinerary_chars=100)])
    row = out["data"][0]
    assert row["failure_class"] == "raw_insufficient"
    assert row["retryable"] is False


def test_endpoint_elephant_riding_is_needs_human_not_retryable():
    out = _run([_row(score_overall=8.0, brand_audit_status="manual_check",
                     failure_codes='["FACT_CHECK_MANUAL_CHECK"]',
                     failure_summary="codes=FACT_CHECK_MANUAL_CHECK")])
    row = out["data"][0]
    assert row["failure_class"] == "needs_human"
    assert row["retryable"] is False
    # the headline block still classifies it as needs_human too (unchanged behaviour)
    assert row["block"]["kind"] == "needs_human"
