"""AA-645 step 0 — review round (S248): fixes found by the independent review of the step-0 PR.

1. A reviewer edit / admin revalidate of a v2 version keeps d02: the revalidation state carries the version's
   writer_version, so the judge does not min() brand fit into the score (and validate keeps the two v2 codes
   report-only).
2. The T13 brand-fit cache clear is best effort: a failure is recorded and the platform recompute is still enqueued.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline
from tests.unit import test_aa293_revalidate_jsonb_codec as rv
from tests.unit.test_aa645_writer_v2_live import _judge


def _revalidate_state(metadata: dict) -> dict:
    row = rv._gc_row()
    row["metadata"] = json.dumps(metadata)
    conn = rv._fake_conn([row, rv._seo_row()])
    captured = {}
    fake_graph = MagicMock()
    fake_graph.invoke.side_effect = lambda state: (captured.update(state=state), rv._fake_graph_result())[1]
    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)), \
         patch("services.content_generation.graph.build_revalidation_graph", MagicMock(return_value=fake_graph)):
        asyncio.run(admin_pipeline._revalidate_tour(rv.FAKE_UUID))
    return captured["state"]


def test_revalidate_state_carries_the_versions_writer_version(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    assert _revalidate_state({"brand_name": "AA", "writer_version": "v2"})["writer_version"] == "v2"
    assert _revalidate_state({"brand_name": "AA"})["writer_version"] is None


def test_revalidating_a_v2_version_does_not_gate_on_brand_fit(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    state = _revalidate_state({"brand_name": "AA", "writer_version": "v2"})
    keep = {k: state[k] for k in ("writer_version", "is_tenant_rewrite")}
    out, signal, tie = _judge(keep)
    assert out["quality_score"] == 9.0                    # validate's score, brand fit 5.0 not min()'d
    assert signal["gate"] == "brand_fit_report_only"
    tie.assert_not_called()
    v1 = {k: _revalidate_state({"brand_name": "AA"})[k] for k in ("writer_version", "is_tenant_rewrite")}
    assert _judge(v1)[0]["quality_score"] == 5.0          # a v1 version is still gated


@pytest.mark.asyncio
async def test_a_failed_brand_fit_cache_clear_still_enqueues_the_recompute():
    from services.export import handler as export_handler
    conn = AsyncMock()
    conn.close = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"segment_id": "seg_a"}])
    rewritten = {"name": "Tour", "summary": "s", "highlights": "[]", "itineraries": "Day 1..."}
    with patch("services.export.handler.open_job_pool", AsyncMock(return_value=conn)), \
         patch("services.acp_contract.a3_atomize.run_a3_atomize",
               AsyncMock(return_value={"status": "success", "atom_count": 3})), \
         patch("services.acp_contract.segment_matching.run_segment_matching", AsyncMock(return_value={})), \
         patch("services.export.handler.clear_brand_fit_cache", AsyncMock(side_effect=RuntimeError("lock timeout"))), \
         patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("job-9", True))) as enq, \
         patch("services.places.enqueue.after_atomize", AsyncMock(return_value={})):
        out = await export_handler._run_a3_atomize_background(
            tour_id="00000000-0000-0000-0000-0000000000aa", rewritten=rewritten, country="Laos",
            version_id="00000000-0000-0000-0000-0000000000bb", reraise=True)
    enq.assert_awaited_once()
    assert enq.await_args.kwargs["segment_ids"] == ["seg_a"]
    assert out["brand_fit_cache_cleared"].startswith("failed: RuntimeError: lock timeout")
    assert out["segment_score_route"] == "ok" and out["score_route_job"] == "job-9"


# ── 3. brand_audit: the AA voice rubric is report-only on an A1 v2 draft (d02) ─────────────────────────────────────
def _audit(state: dict, *, codes, publish_ready=False, status="flagged"):
    from services.content_generation import brand_audit_node as ba
    reply = {"brand_audit": {"status": status, "publish_ready": publish_ready, "fields_to_fix": ["summary"],
                             "failure_codes": codes, "issues": ["reads salesy"],
                             "scores": {"brand_fit": 0, "human_read": 1, "seo_fit": 1, "trip_type_accuracy": 1,
                                        "publish_readiness": 0},
                             "notes": "", "lessons_extracted": []}}
    resp = MagicMock(content=json.dumps(reply), input_tokens=10, output_tokens=5, cost_usd=0.001,
                     stop_reason="end_turn", model_used="m", satellite_account=None, fallback_used=False,
                     provider="bedrock")
    client = MagicMock()
    client.generate.return_value = resp
    base = {"generated": {"name": "Hidden Valleys of Laos", "subtitle": "Rivers, caves and a slow pace",
                          "summary": "s", "highlights": ["h"], "itineraries": "Day 1 — Luang Prabang\nWalk.",
                          "seo_title": "t", "seo_meta": "m"},
            "tour": {"itineraries": "Day 1: Luang Prabang"}, "is_tenant_rewrite": False,
            "quality_shadow": [{"code": "ITINERARY_STILL_COMPRESSED", "detector": "s1_writer_v2", "mode": "report"}]}
    with patch.object(ba, "LLMClient", return_value=client), patch.object(ba, "record_call_sync"):
        return ba.brand_audit_node({**base, **state})


def test_v1_draft_keeps_the_voice_rubric_gating():
    out = _audit({}, codes=["BRAND_TONE_HYPE"])
    assert out["brand_audit_status"] == "flagged" and out["brand_audit_codes"] == ["BRAND_TONE_HYPE"]


def test_v2_draft_voice_codes_are_report_only():
    out = _audit({"writer_version": "v2"}, codes=["BRAND_TONE_HYPE"])
    assert out["brand_audit_status"] == "pass" and out["brand_audit_codes"] == []
    reported = [f for f in out["quality_shadow"] if f["detector"] == "s1_writer_v2_brand_audit"]
    assert [f["code"] for f in reported] == ["BRAND_TONE_HYPE"]
    assert any(f["code"] == "ITINERARY_STILL_COMPRESSED" for f in out["quality_shadow"])   # validate's kept


def test_v2_draft_a_bare_not_publish_ready_is_recorded_not_gated():
    out = _audit({"writer_version": "v2"}, codes=[])
    assert out["brand_audit_status"] == "pass"
    assert [f["code"] for f in out["quality_shadow"] if f["detector"] == "s1_writer_v2_brand_audit"] == [
        "BRAND_AUDIT_FLAGGED"]


def test_v2_draft_product_truth_and_fact_check_still_gate():
    truth = _audit({"writer_version": "v2"}, codes=["PRODUCT_TRUTH_RISK", "BRAND_TONE_HYPE"])
    assert truth["brand_audit_status"] == "manual_check" and truth["brand_audit_codes"] == ["PRODUCT_TRUTH_RISK"]
    model_says = _audit({"writer_version": "v2"}, codes=[], status="manual_check")
    assert model_says["brand_audit_status"] == "manual_check"
    elephant = _audit({"writer_version": "v2", "generated": {
        "name": "Laos", "subtitle": "s", "summary": "s", "highlights": ["An elephant ride at dawn"],
        "itineraries": "Day 1 — Luang Prabang\nWalk.", "seo_title": "t", "seo_meta": "m"}}, codes=[])
    assert elephant["brand_audit_status"] == "manual_check"
    assert "FACT_CHECK_MANUAL_CHECK" in elephant["brand_audit_codes"]


def test_a_tenant_rewrite_marked_v2_keeps_its_rubric_gating():
    with patch("services.content_generation.brand_audit_node._tenant_rubric", return_value="Tenant voice"):
        out = _audit({"writer_version": "v2", "is_tenant_rewrite": True, "tenant_id": "ten-1"},
                     codes=["BRAND_TONE_HYPE"])
    assert out["brand_audit_status"] == "flagged"


# ── 4. admin counts read the last change (updated_at), not the first publish (published_at kept by d06) ───────────
def test_overview_published_7d_counts_republished_tours():
    import re
    from api.routers import admin_overview
    pub = re.search(r"\bpub_c AS \((.*?)\n\),?", admin_overview._FUNNEL_SQL, re.S).group(1)
    assert "pt.updated_at >= now() - interval '7 days') AS published_7d" in pub
    assert "pt.published_at" not in pub


def test_library_metrics_recent_and_stale_use_updated_at():
    import inspect
    src = inspect.getsource(admin_pipeline.get_library_metrics)
    assert "WHEN updated_at >= NOW() - INTERVAL '30 days' THEN 1 END) AS published_last_30d" in src
    assert "WHEN updated_at < NOW() - INTERVAL '180 days' THEN 1 END) AS stale_count" in src
    assert "WHEN published_at >= NOW() - INTERVAL '30 days' THEN 1 END) AS first_published_last_30d" in src
    assert "MAX(pt.updated_at)" in src and "MAX(pt.published_at)" not in src
