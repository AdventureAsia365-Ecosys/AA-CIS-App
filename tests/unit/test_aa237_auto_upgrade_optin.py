"""AA-237 — haiku→sonnet auto-upgrade is opt-in (default OFF).

The Sonnet re-run in _execute_run_tour previously fired for ANY haiku run scoring
0 < score < AUTO_UPGRADE_THRESHOLD, silently charging Sonnet. AA-237 gates it behind
`req.allow_auto_upgrade` (default False) and surfaces `auto_upgraded` in the response.

These tests exercise the REAL wired gate in _execute_run_tour (not a copy):
  * asyncpg.connect → fake AsyncMock conn (fetchrow returns a raw_tours dict).
  * _resolve_brand_rule → None (brand_rules stays {}).
  * process_seo / build_seed → patched (no network).
  * _rewrite_tour → AsyncMock; first return = haiku, optional second = sonnet.
Generated is {} so version_id stays None → all DB-insert / export / accounting paths
are skipped, leaving a clean path through the upgrade gate to the return dict.
We assert the Sonnet branch call-count (0 vs 1 extra) + the new auto_upgraded flag.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    # Pin the threshold so 7.0 < 8.5 deterministically.
    monkeypatch.setenv("AUTO_UPGRADE_THRESHOLD", "8.5")


def _raw_row():
    """A non-trashed raw_tours row (dict supports both [] and .get like a Record)."""
    return {
        "src_name": "Test Tour", "src_subtitle": "", "src_summary": "",
        "src_description": "", "src_highlights": [], "src_itineraries": "",
        "country": "Vietnam", "duration": "7 days", "price_raw": "1000",
        "inclusions": "", "exclusions": "", "source_status": None,
        "activities": None,
    }


def _result(score, model):
    # generated={} → version_id stays None → DB/export/accounting all skipped.
    return {"status": "success", "quality_score": score, "model_used": model,
            "generated": {}, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}


def _req(**over):
    base = dict(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal")  # AA-1024 A2: no model_tier
    base.update(over)
    return admin_pipeline.TourRunRequest(**base)


def _run(rewrite_returns, **req_over):
    """Run _execute_run_tour with all I/O patched; return (result_dict, rewrite_mock)."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _raw_row()
    conn.fetch.return_value = []          # no lessons
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
                                  MagicMock(return_value="Vietnam tours")))
        import asyncio
        out = asyncio.run(admin_pipeline._execute_run_tour(_req(**req_over)))
    return out, rt


# ── default OFF: no upgrade even when haiku < threshold ─────────────────────────

def test_default_off_no_upgrade():
    out, rt = _run([_result(7.0, "haiku-4.5")], allow_auto_upgrade=False)
    assert rt.call_count == 1                      # sonnet branch NOT taken
    assert out["auto_upgraded"] is False
    assert out["model_used"] == "haiku-4.5"        # haiku result kept, no silent sonnet


def test_default_is_off_when_field_omitted():
    # allow_auto_upgrade not passed at all → defaults False → no upgrade.
    out, rt = _run([_result(7.0, "haiku-4.5")])
    assert rt.call_count == 1
    assert out["auto_upgraded"] is False


# ── opt-in ON: upgrade fires and is surfaced ───────────────────────────────────

def test_opt_in_upgrade_taken():
    out, rt = _run([_result(7.0, "haiku-4.5"), _result(8.2, "sonnet-4.5")],
                   allow_auto_upgrade=True)
    assert rt.call_count == 2                       # sonnet re-run fired
    assert rt.call_args.kwargs["model_tier"] == "sonnet"
    assert out["auto_upgraded"] is True
    assert out["model_used"] == "sonnet-4.5"        # upgraded result kept + surfaced


def test_opt_in_but_sonnet_not_better_keeps_haiku():
    # Sonnet ran but scored lower → keep haiku, auto_upgraded stays False.
    out, rt = _run([_result(7.0, "haiku-4.5"), _result(6.0, "sonnet-4.5")],
                   allow_auto_upgrade=True)
    assert rt.call_count == 2                       # re-run attempted
    assert out["auto_upgraded"] is False            # but not kept
    assert out["model_used"] == "haiku-4.5"


def test_opt_in_but_already_above_threshold_no_upgrade():
    # haiku already >= threshold → gate false regardless of opt-in.
    out, rt = _run([_result(8.6, "haiku-4.5")], allow_auto_upgrade=True)
    assert rt.call_count == 1
    assert out["auto_upgraded"] is False
    assert out["model_used"] == "haiku-4.5"
