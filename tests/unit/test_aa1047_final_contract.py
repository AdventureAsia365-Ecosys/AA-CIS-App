"""AA-1047 W1 — the REPORT-mode final contract (services/quality/final_contract.py) and its stores.

Invented tours and places only (CI golden-set text guard)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.quality import final_contract as fc
from services.quality import final_contract_store as store

SRC_ITIN = ("Day 1: Arrive in Velmora. Transfer to the inn.\nDay 2: Walk the Karsh gorge.\n"
            "Day 3: Boat to Lake Ondra.\nDay 4: Depart.")
PAGE_ITIN = ("Day 1 — Arrival in Velmora\nSettle in at the inn.\n\nDay 2 — The Karsh gorge\nA long walk.\n\n"
             "Day 3 — Lake Ondra\nA boat crossing.\n\nDay 4 — Departure\nTransfer out.")
META = "A four-day walk through the Karsh gorge and Lake Ondra, with village inns, a boat crossing and a local " \
       "guide on every single day of the long route."


def _content(**over):
    base = {"name": "Karsh Gorge and Lake Ondra", "subtitle": "Four days on foot", "summary": "A short walking trip.",
            "description": "", "highlights": ["The Karsh gorge", "A boat on Lake Ondra"], "itineraries": PAGE_ITIN,
            "seo_title": "Karsh Gorge Walk", "seo_meta": META}
    return {**base, **over}


def _s1(**over) -> fc.S1Input:
    base = dict(content=_content(), source={"itineraries": SRC_ITIN, "duration": "4 days", "country": "Nowhere"},
                failure_codes=[], brand_audit_status="pass", brand_audit_codes=[], fix_pass_applied=False,
                writer_version="v2", grounding_ran=True, grounding_violations=[], degraded=[], quality_score=9.5)
    base.update(over)
    return fc.S1Input(**base)


def test_meta_fixture_is_in_band():
    lo, hi = fc.policy_value("meta_band")
    assert lo <= len(META) <= hi


def test_s1_clean_page_publishes_and_agrees_with_an_approved_status():
    c = fc.check_s1(_s1(), exit="done", actual="approved")
    assert c.verdict == fc.PUBLISH, c.findings
    assert c.agrees is True and c.flip is None
    rec = c.record()
    assert rec["mode"] == "report" and rec["text_sha"] == fc.tour_text_sha(_content())


def test_s1_day_loss_against_the_stated_duration_is_a_review_flip():
    """The 12 -> 4 kind: the source parser found no day markers, the page has fewer days than the duration."""
    short = "Day 1 — Arrival\nIn.\n\nDay 2 — Gorge\nWalk."
    inp = _s1(content=_content(itineraries=short), source={"itineraries": "We walk, sail and rest.",
                                                           "duration": "12 days"})
    c = fc.check_s1(inp, exit="done", actual="approved")
    assert "DAY_COUNT_VS_DURATION" in c.codes(fc.BLOCK)
    assert c.verdict == fc.REVIEW and c.flip == "unpublish"


def test_s1_missing_source_day_is_a_review():
    three = PAGE_ITIN.split("\n\nDay 4")[0]
    c = fc.check_s1(_s1(content=_content(itineraries=three)), exit="done", actual="approved")
    assert "ITINERARY_DAY_COUNT_MISMATCH" in c.codes(fc.BLOCK)
    assert c.verdict == fc.REVIEW


def test_s1_source_problems_are_a_source_hold():
    c = fc.check_s1(_s1(source={"itineraries": "", "duration": "4 days"}), exit="done", actual="hitl")
    assert c.verdict == fc.SOURCE_HOLD and c.agrees is True


def test_s1_grounding_not_run_or_stale_is_a_review():
    assert "GROUNDING_NOT_RUN" in fc.check_s1(_s1(grounding_ran=False), exit="done").codes()
    assert "GROUNDING_NOT_RUN_ON_EDIT" in fc.check_s1(_s1(grounding_stale=True), exit="approve").codes()
    assert "UNSUPPORTED_SENTENCE" in fc.check_s1(_s1(grounding_violations=[{"code": "X"}]), exit="done").codes()


def test_s1_elephant_riding_alone_is_a_policy_hold():
    page = _content(highlights=["An elephant ride along the river"])
    c = fc.check_s1(_s1(content=page, brand_audit_status="manual_check",
                        brand_audit_codes=["FACT_CHECK_MANUAL_CHECK"]), exit="hitl", actual="hitl")
    assert c.verdict == fc.POLICY_HOLD and c.agrees is True


def test_s1_elephant_with_another_manual_check_is_a_review():
    page = _content(highlights=["An elephant ride along the river"])
    c = fc.check_s1(_s1(content=page, brand_audit_status="manual_check",
                        brand_audit_codes=["FACT_CHECK_MANUAL_CHECK", "PRODUCT_TRUTH_RISK"]),
                    exit="done", actual="hitl")
    assert c.verdict == fc.REVIEW and "MANUAL_CHECK" in c.codes()


def test_s1_score_and_meta_are_report_columns_only():
    c = fc.check_s1(_s1(quality_score=3.0, content=_content(seo_meta="Too short.")), exit="done", actual="hitl")
    assert {"SCORE_BELOW_FLOOR", "META_OUT_OF_BAND"} <= set(c.codes(fc.WARN))
    assert c.verdict == fc.PUBLISH and c.flip == "publish"


def test_s1_hard_degraded_detector_is_degraded():
    deg = [{"name": "jev_unavailable", "severity": "hard", "detector": "grounding", "count": 3}]
    c = fc.check_s1(_s1(degraded=deg), exit="done", actual="hitl")
    assert c.verdict == fc.DEGRADED


def test_s1_forbidden_word_on_the_stored_text_is_rechecked():
    c = fc.check_s1(_s1(content=_content(summary="A stunning walk.")), exit="human_edit", actual="hitl")
    assert "FORBIDDEN_WORD" in c.codes(fc.BLOCK)


def test_s1_input_from_row_reads_a_stored_version():
    row = {"aa_name": "Karsh Gorge and Lake Ondra", "aa_subtitle": "Four days on foot", "aa_summary": "A walk.",
           "aa_description": "", "aa_highlights": json.dumps(["The gorge"]), "aa_itineraries": PAGE_ITIN,
           "seo_title": "Karsh Gorge Walk", "seo_meta": META, "src_itineraries": SRC_ITIN, "duration": "4 days",
           "failure_codes": "[]", "brand_audit_status": "pass", "brand_audit_codes": "[]",
           "fix_pass_applied": False, "human_edited": False, "score_overall": 9.0,
           "metadata": json.dumps({"writer_version": "v2", "grounding": {"violations": []}}), "status": "approved"}
    rec = store.s1_record_from_row(row, "auto_publish")
    assert rec["verdict"] == fc.PUBLISH and rec["actual"] == "approved" and rec["agrees"] is True
    # the jsonb list read back as JSON text and the list itself hash the same
    assert fc.tour_text_sha({"highlights": json.dumps(["a"])}) == fc.tour_text_sha({"highlights": ["a"]})


def test_stamp_stored_flags_a_post_gate_edit():
    rec = fc.check_s1(_s1(), exit="done").record()
    ok = fc.stamp_stored(rec, rec["text_sha"], actual="approved")
    assert ok["sha_match"] is True and ok["verdict"] == fc.PUBLISH
    bad = fc.stamp_stored(rec, fc.tour_text_sha(_content(name="Edited")), actual="approved")
    assert bad["sha_match"] is False and "POST_GATE_EDIT" in [f["code"] for f in bad["findings"]]
    assert bad["verdict"] == fc.REVIEW and bad["flip"] == "unpublish"


def test_t2_contract_verdicts():
    master = _content()
    ok = fc.check_t2(fc.T2Input(content=_content(), master=master), exit="done", actual="ai_generated")
    assert ok.verdict == fc.AI_GENERATED and ok.agrees is True
    lost = fc.check_t2(fc.T2Input(content=_content(itineraries=PAGE_ITIN.split("\n\nDay 3")[0]), master=master),
                       exit="done", actual="ai_generated")
    assert "T2_DAY_COUNT_VS_MASTER" in lost.codes(fc.BLOCK) and lost.flip == "unpublish"
    held = fc.check_t2(fc.T2Input(content=_content(), master=master, policy_hold=True), exit="policy_hold",
                       actual="needs_review")
    assert held.verdict == fc.POLICY_HOLD and held.agrees is True
    edited = fc.check_t2(fc.T2Input(content=_content(), master=master, gated_sha=fc.tour_text_sha(master) + "x"),
                         exit="done")
    assert "POST_GATE_EDIT" in edited.codes(fc.WARN)


def test_t9_contract_checks_the_delivered_text():
    text = ("Walk the Karsh gorge with us.\n\n## FAQ\n\n**Q: How long is it?**\nA: Four days.\n\n"
            "**Q: Is there a boat?**\nA: Yes, on day three.\n\n**Q: Where does it end?**\nA: At Lake Ondra.\n\n"
            "Book with Ondra Trails today.")
    inp = fc.T9Input(content_text=text, channel="blog", cta="Book with Ondra Trails today.",
                     seo_title="Karsh Gorge Walk", meta_description=META, slug="karsh-gorge-walk")
    c = fc.check_t9(inp, exit="done", actual="approved")
    assert c.verdict == fc.APPROVED, c.findings
    assert c.text_sha == fc.text_sha(text)
    two_ctas = fc.check_t9(fc.T9Input(content_text=text + "\nBook with Ondra Trails today.", channel="linkedin",
                                      cta="Book with Ondra Trails today."), exit="done", actual="approved")
    assert "CTA_COUNT" in two_ctas.codes(fc.BLOCK) and two_ctas.flip == "unpublish"
    tagged = fc.check_t9(fc.T9Input(content_text="A walk [F:f1] by the lake."), exit="done")
    assert "CITATION_TAG_LEFT" in tagged.codes(fc.BLOCK)
    cut = fc.check_t9(fc.T9Input(content_text=text, channel="blog", seo_title="Karsh Gorge Walk",
                                 slug="karsh-gorge-wa"), exit="done")
    assert "SLUG_NOT_ON_WORD_BOUNDARY" in cut.codes(fc.WARN)


def test_t9_blocking_gate_left_is_held_and_degraded_is_degraded():
    ledger = [{"gate": "F1_grounding", "passed": False, "blocking": True, "violations": ["a number"]}]
    c = fc.check_t9(fc.T9Input(content_text="A walk by the lake.", gate_ledger=ledger), exit="done", actual="held")
    assert c.verdict == fc.HELD and c.agrees is True
    d = fc.check_t9(fc.T9Input(content_text="A walk.", held_reason="degraded: f9 judge"), exit="done", actual="held")
    assert d.verdict == fc.DEGRADED


def test_log_tenant_edit_never_raises_and_stores_nothing():
    assert fc.log_tenant_edit(fc.T2, "tenant_edit", content=_content(), actual="pending")["exit"] == "tenant_edit"
    assert fc.log_tenant_edit(fc.T9, "tenant_edit", content="A walk.", actual="approved", channel="blog",
                              unknown_field=1)["workflow"] == fc.T9
    assert fc.log_tenant_edit(fc.T2, "tenant_edit", content=object(), actual=None) is not None


# ── S1 graph node ────────────────────────────────────────────────────────────────────────────────────────────────

def _state(**over):
    st = {"tour": {"itineraries": SRC_ITIN, "duration": "4 days"}, "generated": _content(name="KARSH GORGE"),
          "failure_codes": [], "brand_audit_status": "pass", "brand_audit_codes": [], "grounding_ran": True,
          "grounding_violations": [], "quality_score": 9.0, "writer_version": "v2"}
    st.update(over)
    return st


def test_final_contract_node_normalizes_then_records():
    from services.content_generation import graph
    out = graph.final_contract_node(_state())
    assert out["generated"]["name"] == "Karsh Gorge"           # the normalizer ran inside the graph
    rec = out["final_contract"]
    assert rec["exit"] == "done" and rec["text_sha"] == fc.tour_text_sha(out["generated"])
    assert graph.final_contract_node(_state(exit_path="hitl"), graph.ENTRY_RESUMED)["final_contract"]["exit"] == \
        "resumed_hitl"
    reval = graph.final_contract_node(_state(), graph.ENTRY_REVALIDATION)
    assert reval["generated"]["name"] == "KARSH GORGE"         # a re-validation checks the stored text as it is


def test_final_contract_node_passes_a_tenant_rewrite_through():
    from services.content_generation import graph
    st = _state(is_tenant_rewrite=True)
    assert graph.final_contract_node(st) is st


def test_final_contract_node_records_an_error_instead_of_failing():
    from services.content_generation import graph
    with patch.object(fc, "check_s1", side_effect=RuntimeError("boom")):
        out = graph.final_contract_node(_state())
    assert out["final_contract"]["verdict"] is None and "boom" in out["final_contract"]["error"]


def test_store_time_record_matches_the_normalized_text():
    """_rewrite_tour's _normalize_generated is idempotent on the graph's normalized text: stored sha == gated sha."""
    from api.routers import admin_pipeline as ap
    from api.routers.v1_pipeline import _normalize_generated
    from services.content_generation import graph
    out = graph.final_contract_node(_state())
    stored = _normalize_generated(dict(out["generated"]), {})
    rec = ap._final_contract_at_store({"final_contract": out["final_contract"]}, stored, "approved")
    assert rec["sha_match"] is True and rec["actual"] == "approved"
    assert ap._final_contract_at_store({}, stored, "approved") is None


# ── store ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_record_s1_exit_appends_to_the_exit_log():
    row = {"aa_name": "Karsh", "aa_itineraries": PAGE_ITIN, "src_itineraries": SRC_ITIN, "duration": "4 days",
           "metadata": json.dumps({"final_contract_log": [{"exit": "done"}]}), "status": "approved",
           "failure_codes": "[]", "brand_audit_codes": "[]", "brand_audit_status": "pass"}
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()
    rec = asyncio.run(store.record_s1_exit(conn, "v-1", "promote", actual="published"))
    assert rec["exit"] == "promote" and rec["sha_match"] is True
    sql, vid, record, log = conn.execute.await_args.args
    assert "final_contract_log" in sql and vid == "v-1"
    assert [e["exit"] for e in json.loads(log)] == ["done", "promote"]


def test_record_s1_exit_never_raises():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    assert asyncio.run(store.record_s1_exit(conn, "v-1", "approve")) is None
    conn.fetchrow = AsyncMock(return_value=None)
    assert asyncio.run(store.record_s1_exit(conn, "v-1", "approve")) is None


def test_attach_s1_ignores_a_missing_record_and_swallows_errors():
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("x"))
    conn.fetchval = AsyncMock(return_value="{}")
    asyncio.run(store.attach_s1(conn, "v-1", None))
    asyncio.run(store.attach_s1(conn, "v-1", {"exit": "revalidation", "findings": []}))
    conn.execute.assert_awaited_once()


@pytest.mark.parametrize("status,workflow,ok", [
    ("approved", fc.S1, True), ("published", fc.S1, True), ("hitl", fc.S1, False),
    ("ai_generated", fc.T2, True), ("needs_review", fc.T2, False), ("approved", fc.T9, True), ("held", fc.T9, False),
])
def test_is_pass_status(status, workflow, ok):
    assert fc.is_pass_status(workflow, status) is ok


def test_compact_keeps_the_block_codes():
    rec = fc.check_s1(_s1(grounding_ran=False), exit="done", actual="approved").record()
    entry = fc.compact(rec, "2026-11-07T00:00:00+00:00")
    assert entry["codes"] == ["GROUNDING_NOT_RUN"] and entry["flip"] == "unpublish" and entry["at"]


# ── report script ────────────────────────────────────────────────────────────────────────────────────────────────

def test_report_reads_live_v2_masters_and_counts_flips_without_text():
    from scripts.quality import final_contract_report as report
    sql = report.masters_sql(False)
    assert "writer_version' = 'v2'" in sql and "master_status = 'active'" in sql and "deleted_at IS NULL" in sql
    assert "writer_version" not in report.masters_sql(True).split("WHERE pt.")[1]
    base = {"aa_name": "Karsh", "aa_summary": "A walk.", "seo_meta": META, "aa_itineraries": PAGE_ITIN,
            "src_itineraries": SRC_ITIN, "duration": "4 days",
            "metadata": json.dumps({"writer_version": "v2", "grounding": {}}), "failure_codes": "[]",
            "brand_audit_codes": "[]", "brand_audit_status": "pass", "score_overall": 9.0, "country": "Zembla",
            "tour_id": "t-1", "generated_content_id": "g-1", "published_tour_id": "p-1"}
    lost = {**base, "aa_itineraries": "Day 1 — A\nx.\n\nDay 2 — B\ny.", "src_itineraries": "No markers.",
            "duration": "12 days", "tour_id": "t-2", "generated_content_id": "g-2", "published_tour_id": "p-2"}
    out = report.summarize([base, lost])
    assert out["masters"] == 2 and out["errors"] == 0 and out["publish_flip"]["count"] == 1
    assert out["day_loss"] == [{"tour_id": "t-2", "country": "Zembla", "page_days": 2, "duration_days": 12}]
    assert out["by_country"]["Zembla"]["flips"] == 1 and "Karsh" not in json.dumps(out)
    assert report.summarize([lost, lost], sample=1)["publish_flip"]["count"] == 2
    assert len(report.summarize([lost, lost], sample=1)["flips"]) == 1


def test_report_counts_a_bad_row_and_goes_on():
    from scripts.quality import final_contract_report as report
    with patch.object(store, "s1_record_from_row", side_effect=ValueError("bad")):
        out = report.summarize([{"country": None, "generated_content_id": "g-3"}])
    assert out["errors"] == 1 and out["masters"] == 0


# ── the S1 store sites (regression: a local `_fc` in these functions once shadowed the module alias) ──────────────

def test_execute_run_tour_stores_the_contract_with_the_version(monkeypatch):
    from contextlib import ExitStack
    from api.routers import admin_pipeline
    from services.content_generation import graph
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    st = graph.final_contract_node(_state(generated=_content()))
    result = {"status": "success", "quality_score": 9.0, "model_used": "writer-x", "generated": st["generated"],
              "failure_codes": [], "brand_audit_status": "pass", "writer_version": "v2",
              "final_contract": st["final_contract"], "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    conn = AsyncMock()
    conn.fetchrow.return_value = {"src_name": "Karsh", "src_subtitle": "", "src_summary": "", "src_description": "",
                                  "src_highlights": [], "src_itineraries": SRC_ITIN, "country": "Zembla",
                                  "duration": "4 days", "price_raw": "", "inclusions": "", "exclusions": "",
                                  "source_status": None, "activities": None}
    conn.fetch.return_value = []
    conn.fetchval.return_value = "11111111-1111-1111-1111-111111111111"
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)))
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)))
        stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", AsyncMock(return_value=result)))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                                  MagicMock(return_value="Zembla tours")))
        export = stack.enter_context(patch("services.export.handler.process_export", AsyncMock()))
        asyncio.run(admin_pipeline._execute_run_tour(admin_pipeline.TourRunRequest(
            tour_id="11111111-1111-1111-1111-111111111111", batch_id="b-1", tenant_id="aa_internal")))
    [insert] = [c for c in conn.fetchval.await_args_list if "INSERT INTO silver_aa_internal.generated_content"
                in c.args[0]]
    metadata = json.loads(insert.args[15])
    rec = metadata["final_contract"]
    assert rec["sha_match"] is True and rec["actual"] == "approved" and rec["exit"] == "done"
    assert [e["exit"] for e in metadata["final_contract_log"]] == ["done"]
    export.assert_awaited_once()
    assert export.await_args.kwargs == {"contract_exit": "auto_publish"}


def test_revalidate_tour_attaches_the_graph_contract(monkeypatch):
    from tests.unit import test_aa983_revalidate_keeps_quality_score_id as h
    from api.routers import admin_pipeline
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = h._Recorder()
    rec = fc.check_s1(_s1(content={"name": "T"}), exit="revalidation").record()
    graph_mock = MagicMock()
    graph_mock.invoke.return_value = {**h._graph_result(), "final_contract": rec}
    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)), \
         patch("services.content_generation.graph.build_revalidation_graph", MagicMock(return_value=graph_mock)):
        out = asyncio.run(admin_pipeline._revalidate_tour(h.CONTENT_ID))
    assert out["revalidate_passed"] is True
    [upd] = [c for c in conn.calls if c[0] == "execute" and "final_contract_log" in c[1]]
    stored = json.loads(upd[2][1])
    assert stored["exit"] == "revalidation" and stored["actual"] == "approved" and "sha_match" in stored
