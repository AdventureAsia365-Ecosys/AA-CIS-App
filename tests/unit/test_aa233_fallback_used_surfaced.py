"""AA-233 — _execute_run_tour surfaces `fallback_used` in its return dict.

The DB column generated_content.fallback_used has been written correctly since
AA-224 (admin_pipeline persists bool(result.get("fallback_used", False))), but the
API return dict of _execute_run_tour omitted the key entirely → the HTTP response
reported fallback_used=None regardless of the real value. AA-233 adds the key to
the return dict, mirroring result.get("fallback_used", False).

These tests exercise the REAL wired return dict in _execute_run_tour (not a copy),
using the same all-I/O-patched harness as AA-237:
  * asyncpg.connect → fake AsyncMock conn (fetchrow returns a raw_tours dict).
  * _resolve_brand_rule → None.  process_seo / build_seed → patched (no network).
  * _rewrite_tour → AsyncMock returning a result dict.
generated={} → version_id stays None → DB-insert / export / accounting skipped,
leaving a clean path to the return dict.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from api.routers import admin_pipeline

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("AUTO_UPGRADE_THRESHOLD", "8.5")


def _raw_row():
    return {
        "src_name": "Test Tour", "src_subtitle": "", "src_summary": "",
        "src_description": "", "src_highlights": [], "src_itineraries": "",
        "country": "Vietnam", "duration": "7 days", "price_raw": "1000",
        "inclusions": "", "exclusions": "", "source_status": None,
        "activities": None,
    }


def _result(fallback=None):
    r = {"status": "success", "quality_score": 9.0, "model_used": "haiku-4.5",
         "generated": {}, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    if fallback is not None:
        r["fallback_used"] = fallback
    return r


def _req(**over):
    base = dict(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal")  # AA-1024 A2: no model_tier
    base.update(over)
    return admin_pipeline.TourRunRequest(**base)


def _run(result_dict):
    conn = AsyncMock()
    conn.fetchrow.return_value = _raw_row()
    conn.fetch.return_value = []
    rt = AsyncMock(side_effect=[result_dict])
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect",
                                  AsyncMock(return_value=conn)))
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", rt))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                                  MagicMock(return_value="Vietnam tours")))
        import asyncio
        out = asyncio.run(admin_pipeline._execute_run_tour(_req()))
    return out


def test_fallback_used_key_present():
    # Regression guard: the key must exist (was missing → None pre-AA-233).
    out = _run(_result(fallback=False))
    assert "fallback_used" in out


def test_fallback_used_true_surfaced():
    out = _run(_result(fallback=True))
    assert out["fallback_used"] is True


def test_fallback_used_defaults_false_when_absent():
    # result omits the key → return dict must coerce to False, never None.
    out = _run(_result(fallback=None))
    assert out["fallback_used"] is False
    assert out["fallback_used"] is not None
