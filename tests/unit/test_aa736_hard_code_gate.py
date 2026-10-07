"""AA-736 — hard failure codes block Master; one-shot haiku→sonnet upgrade, else review queue.

Two layers:
  * Pure-function tests for _is_publishable / _surviving_hard_codes / _build_failure_summary
    (no DB) — cover the gate decision and the review-queue reason text.
  * Wired-gate tests through _execute_run_tour (all I/O patched, same harness as AA-237) —
    cover that a surviving REWRITE-class hard code triggers exactly one sonnet re-run, and a
    deterministic-fixable hard code does NOT.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


# ── pure gate helpers ──────────────────────────────────────────────────────────

def _res(score=8.0, model="haiku-4.5", codes=None, **extra):
    r = {"status": "success", "quality_score": score, "model_used": model,
         "failure_codes": codes or [], "brand_audit_status": "pass",
         "fix_pass_applied": True, "generated": {}, "cost_usd": 0.0,
         "input_tokens": 0, "output_tokens": 0}
    r.update(extra)
    return r


def test_surviving_hard_codes_detects_forbidden_word():
    assert admin_pipeline._surviving_hard_codes(_res(codes=["FORBIDDEN_WORD"])) == {"FORBIDDEN_WORD"}


def test_surviving_hard_codes_ignores_soft_codes():
    # ITINERARY_STILL_COMPRESSED stays soft (Nghiep S217) — not a hard-block code.
    assert admin_pipeline._surviving_hard_codes(
        _res(codes=["ITINERARY_STILL_COMPRESSED", "DFS_INTENT_UNDERUSED"])) == set()


def test_not_publishable_when_hard_code_remains():
    # AA-736 core: score 8.0, brand pass, but a leftover FORBIDDEN_WORD must block Master.
    assert admin_pipeline._is_publishable(_res(score=8.0, codes=["FORBIDDEN_WORD"])) is False


def test_publishable_when_only_soft_codes_remain():
    assert admin_pipeline._is_publishable(_res(score=8.0, codes=["ITINERARY_STILL_COMPRESSED"])) is True


def test_publishable_clean_run():
    assert admin_pipeline._is_publishable(_res(score=8.0, codes=[])) is True


def test_failure_summary_notes_sonnet_retry_with_field():
    # A forbidden word survived into aa_summary after a sonnet retry → summary must say so.
    r = _res(score=8.0, codes=["FORBIDDEN_WORD"], _sonnet_retried=True,
             generated={"name": "Nepal Trek", "summary": "A breathtaking Himalayan journey."})
    summary = admin_pipeline._build_failure_summary(r)
    assert "retried with sonnet" in summary
    assert "FORBIDDEN_WORD" in summary
    assert "aa_summary" in summary


def test_failure_summary_no_sonnet_note_when_not_retried():
    r = _res(score=8.0, codes=["FORBIDDEN_WORD"])  # no _sonnet_retried flag
    summary = admin_pipeline._build_failure_summary(r)
    assert "retried with sonnet" not in summary


# ── wired gate through _execute_run_tour (AA-237 harness) ────────────────────────

@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("AUTO_UPGRADE_THRESHOLD", "8.5")


def _raw_row():
    return {
        "src_name": "Test Tour", "src_subtitle": "", "src_summary": "",
        "src_description": "", "src_highlights": [], "src_itineraries": "",
        "country": "Nepal", "duration": "7 days", "price_raw": "1000",
        "inclusions": "", "exclusions": "", "source_status": None, "activities": None,
    }


def _result(score, model, codes=None):
    # generated={} → version_id stays None → DB/export/accounting all skipped.
    return {"status": "success", "quality_score": score, "model_used": model,
            "failure_codes": codes or [], "generated": {}, "cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0}


def _req(**over):
    base = dict(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal", model_tier="haiku")
    base.update(over)
    return admin_pipeline.TourRunRequest(**base)


def _run(rewrite_returns, **req_over):
    conn = AsyncMock()
    conn.fetchrow.return_value = _raw_row()
    conn.fetch.return_value = []
    rt = AsyncMock(side_effect=list(rewrite_returns))
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect",
                                  AsyncMock(return_value=conn)))
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", rt))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                                  MagicMock(return_value="Nepal tours")))
        import asyncio
        out = asyncio.run(admin_pipeline._execute_run_tour(_req(**req_over)))
    return out, rt


def test_surviving_rewrite_hard_code_triggers_one_sonnet_retry():
    # Haiku leaves a FORBIDDEN_WORD; sonnet clears it. Exactly one extra rewrite, sonnet kept.
    out, rt = _run([_result(8.0, "haiku-4.5", ["FORBIDDEN_WORD"]),
                    _result(8.0, "sonnet-4.5", [])])
    assert rt.call_count == 2
    assert rt.call_args.kwargs["model_tier"] == "sonnet"
    assert out["auto_upgraded"] is True
    assert out["model_used"] == "sonnet-4.5"
    assert out["failure_codes"] == []


def test_sonnet_kept_when_it_clears_more_hard_codes_even_if_score_equal():
    # Hard-code trigger keeps sonnet on FEWER surviving hard codes, not on a higher score.
    out, rt = _run([_result(8.0, "haiku-4.5", ["FORBIDDEN_WORD"]),
                    _result(8.0, "sonnet-4.5", [])])
    assert out["auto_upgraded"] is True


def test_sonnet_still_failing_keeps_haiku_flag_false_but_marks_retried():
    # Sonnet also leaves the forbidden word → neither run is publishable; we keep the first
    # (no improvement) but the run is marked retried for the review-queue reason.
    out, rt = _run([_result(8.0, "haiku-4.5", ["FORBIDDEN_WORD"]),
                    _result(8.0, "sonnet-4.5", ["FORBIDDEN_WORD"])])
    assert rt.call_count == 2
    assert admin_pipeline._surviving_hard_codes(out) == {"FORBIDDEN_WORD"}
    assert admin_pipeline._is_publishable(out) is False


def test_deterministic_fixable_hard_code_does_not_trigger_upgrade():
    # SEO_META_TOO_LONG is hard but deterministic-fixable (AA-641 fit) → no sonnet run.
    out, rt = _run([_result(8.0, "haiku-4.5", ["SEO_META_TOO_LONG"])])
    assert rt.call_count == 1
    assert out["auto_upgraded"] is False


def test_clean_haiku_run_no_upgrade():
    out, rt = _run([_result(8.0, "haiku-4.5", [])])
    assert rt.call_count == 1
    assert out["auto_upgraded"] is False
