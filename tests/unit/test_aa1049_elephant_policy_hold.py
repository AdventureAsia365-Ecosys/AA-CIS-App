"""AA-1049 (tech lead decision S277, root ADR 0020 D8) — the elephant-riding policy hold, without a DB.

The DB side (the hold, the publish / Set Active guard, the approve lifting the hold, _enqueue_review, the script, the
Review Queue read) runs on Postgres in tests/integration/test_aa1049_elephant_policy_hold.py. Here: the pure review
marker (codes only, never page text), the publish refusal of process_export (version back to 'hitl', a pending policy
review, a typed error every caller catches), the S1 run treating that refusal as expected, the approve of a held
Master's own version, and the Set Active refusal filing the review. Invented names and places only.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.quality import elephant_policy, policy_review
from services.tour_lifecycle import LifecycleError
from services.tour_lifecycle import policy as lp

HITS = [{"where": "master", "field": "highlights", "mention": "elephant ride"},
        {"where": "master", "field": "highlights", "mention": "riding an elephant"},
        {"where": "source", "field": "itineraries", "mention": "ride an elephant"}]


# ── the review marker ──────────────────────────────────────────────────────────────────────────────────────────────

def test_detail_is_codes_only_one_item_per_field():
    d = policy_review.detail(HITS)
    assert [(i["where"], i["field"]) for i in d] == [("master", "highlights"), ("source", "itineraries")]
    assert all(i["check_id"] == policy_review.CHECK_ID and i["source_span"] is None for i in d)
    assert "elephant ride" not in json.dumps(d) and "riding an elephant" not in json.dumps(d)


def test_detail_always_carries_the_marker():
    d = policy_review.detail([])
    assert len(d) == 1 and policy_review.is_policy_detail(d)


def test_is_policy_detail_reads_a_list_or_its_json_text():
    d = policy_review.detail(HITS)
    assert policy_review.is_policy_detail(d) and policy_review.is_policy_detail(json.dumps(d))
    assert not policy_review.is_policy_detail(None) and not policy_review.is_policy_detail("not json")
    assert not policy_review.is_policy_detail([{"check_id": "grounding:x"}])
    assert policy_review.evidence(json.dumps(d)) == [{"where": "master", "field": "highlights"},
                                                     {"where": "source", "field": "itineraries"}]


def test_the_sql_marker_is_the_same_literal_and_only_on_s1_rows():
    sql = policy_review.row_is_policy_sql("rq")
    assert "rq.generated_content_id IS NOT NULL" in sql and policy_review.MARKER_JSON in sql
    assert policy_review.CHECK_ID == "policy:" + elephant_policy.POLICY_HOLD


def test_summary_keeps_the_reason_after_the_prefix():
    assert policy_review.summary("LOW_SCORE") == policy_review.SUMMARY_PREFIX + "; LOW_SCORE"
    assert policy_review.summary(None) == policy_review.SUMMARY_PREFIX


def test_hits_from_row_labels_master_and_version_text():
    row = {"aa_highlights": '["A morning elephant ride by Quillmere lake"]', "country": "Thailand"}
    assert [h["where"] for h in lp.hits_from_row(row, master=True)] == ["master"]
    assert [h["where"] for h in lp.hits_from_row(row)] == ["version"]
    assert lp.hits_from_row({"aa_highlights": '["Walk by Quillmere lake"]'}) == []


# ── process_export: the publish refusal ────────────────────────────────────────────────────────────────────────────

GC_ROW = {"id": "gc-1", "tour_id": "t-1", "tenant_id": "t-1", "batch_id": None, "aa_name": "Quillmere Ridge",
          "aa_subtitle": "s", "aa_summary": "sum", "aa_description": "d", "aa_highlights": "[]",
          "aa_itineraries": "Day 1...", "mobile_card_text": None, "seo_title": "t", "seo_meta": "m" * 150,
          "seo_keywords_used": "[]", "og_tags": "{}", "quality_score_id": None, "quality_score": 9.0,
          "country": "Thailand", "duration": "3 days", "src_source_status": "active", "src_deleted_at": None}


@pytest.mark.asyncio
async def test_process_export_refuses_a_ride_without_approval(monkeypatch):
    from services.export import handler
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [dict(GC_ROW)]

    async def _refuse(*_a, **_k):
        raise LifecycleError(409, lp.POLICY_REVIEW_REQUIRED, lp.refusal_message("publishing it"))

    monkeypatch.setattr("services.tour_lifecycle.publish", _refuse)
    monkeypatch.setattr(lp, "version_hits", AsyncMock(return_value=HITS[:1]))
    ensure = AsyncMock(return_value="rv-1")
    monkeypatch.setattr(lp, "ensure_policy_review", ensure)
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)):
        with pytest.raises(lp.PolicyReviewRequiredError) as e:
            await handler.process_export("gc-1")
    assert e.value.review_id == "rv-1" and e.value.version_id == "gc-1"
    hitl = [c for c in conn.execute.call_args_list if "SET status = 'hitl'" in c.args[0]]
    assert len(hitl) == 1 and hitl[0].args[1] == "gc-1"
    ensure.assert_awaited_once_with(conn, "t-1", "gc-1", HITS[:1])


@pytest.mark.asyncio
async def test_process_export_refusal_stands_when_filing_the_review_fails(monkeypatch):
    from services.export import handler
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [dict(GC_ROW)]

    async def _refuse(*_a, **_k):
        raise LifecycleError(409, lp.POLICY_REVIEW_REQUIRED, "x")

    monkeypatch.setattr("services.tour_lifecycle.publish", _refuse)
    monkeypatch.setattr(lp, "version_hits", AsyncMock(side_effect=RuntimeError("db gone")))
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)):
        with pytest.raises(lp.PolicyReviewRequiredError) as e:
            await handler.process_export("gc-1")
    assert e.value.review_id is None


def test_the_refusal_names_the_review_queue():
    err = lp.PolicyReviewRequiredError("gc-1", "rv-1")
    assert str(err).startswith(lp.POLICY_REVIEW_REQUIRED) and "Review Queue" in str(err)
    assert "Review Queue" in lp.refusal_message("setting it active")


# ── the approve of a held Master's own version ─────────────────────────────────────────────────────────────────────

REVIEW_ID = "33333333-3333-3333-3333-333333333333"
GCID = "22222222-2222-2222-2222-222222222222"


def _pool(conn):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _approve(conn):
    from api.routers import v1_pipeline
    req = MagicMock()
    req.app.state.pool = _pool(conn)
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        try:
            return asyncio.run(v1_pipeline.approve_review(REVIEW_ID, req, None, None)), export
        except HTTPException as e:
            return e, export


def _blocked_gate_row():
    # an old Haiku Master's version: no grounding record, a score under the floor (blocked as a new version)
    return {"generated_content_id": GCID, "human_edited": False, "fix_pass_applied": False, "metadata": "{}",
            "score_overall": 5.0, "failure_codes": "[]", "brand_audit_status": None, "brand_audit_codes": "[]"}


def test_the_held_masters_own_version_is_approved_as_the_policy_review():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_blocked_gate_row(),
                                           {"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.fetchval = AsyncMock(return_value=True)      # held_master_version
    conn.execute = AsyncMock(return_value="UPDATE 1")
    out, export = _approve(conn)
    assert not isinstance(out, HTTPException)
    assert out["policy_hold_review"] is True and out["policy_keep"] is True and out["exported"] is True
    export.assert_awaited_once()
    assert export.await_args.kwargs["contract_exit"] == "approve"


def test_another_version_keeps_the_approve_gate():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_blocked_gate_row()])
    conn.fetchval = AsyncMock(return_value=False)
    out, export = _approve(conn)
    assert isinstance(out, HTTPException) and out.status_code == 409
    export.assert_not_called()


# ── the S1 run: the refusal is expected, not an error ──────────────────────────────────────────────────────────────

def test_the_s1_run_catches_the_policy_refusal():
    import inspect

    from api.routers import admin_pipeline
    src = inspect.getsource(admin_pipeline._execute_run_tour)
    assert "except PolicyReviewRequiredError" in src
    assert src.index("except PolicyReviewRequiredError") < src.index("except SourceNotLiveError")


def test_promote_answers_409_policy_review_required():
    import inspect

    from api.routers import admin_pipeline
    src = inspect.getsource(admin_pipeline.promote_tour_version)
    assert "except PolicyReviewRequiredError" in src and "POLICY_REVIEW_REQUIRED" in src


# ── Set Active refused: the review is filed ────────────────────────────────────────────────────────────────────────

def test_set_active_refusal_files_the_policy_review(monkeypatch):
    from api.routers import admin

    async def _refuse(conn, tour_id, **kw):
        raise LifecycleError(409, lp.POLICY_REVIEW_REQUIRED, lp.refusal_message("setting it active"))

    held = MagicMock()
    hold = AsyncMock(return_value=held)
    after = AsyncMock()
    monkeypatch.setattr("services.tour_lifecycle.policy_hold", hold)
    monkeypatch.setattr("services.tour_lifecycle.run_after_commit", after)
    req = MagicMock()
    req.app.state.pool = _pool(AsyncMock())
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin._run_lifecycle(req, _refuse, "t-1", actor="staff"))
    assert e.value.status_code == 409 and e.value.headers == {"X-Error-Code": lp.POLICY_REVIEW_REQUIRED}
    assert e.value.detail.endswith("Its review is in the Review Queue.")
    assert hold.await_args.args[1] == "t-1" and hold.await_args.kwargs == {"actor": "staff"}
    after.assert_awaited_once()


def test_set_active_refusal_without_a_hit_adds_nothing(monkeypatch):
    from api.routers import admin

    async def _refuse(conn, tour_id, **kw):
        raise LifecycleError(409, lp.POLICY_REVIEW_REQUIRED, "x")

    monkeypatch.setattr("services.tour_lifecycle.policy_hold",
                        AsyncMock(side_effect=LifecycleError(409, "no_policy_hit", "none")))
    req = MagicMock()
    req.app.state.pool = _pool(AsyncMock())
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin._run_lifecycle(req, _refuse, "t-1"))
    assert e.value.detail == f"{lp.POLICY_REVIEW_REQUIRED}: x"


# ── the one-time script ────────────────────────────────────────────────────────────────────────────────────────────

def test_the_script_pin_and_report_never_print_text():
    from scripts.quality import hold_elephant_masters as h
    ids = ["b2a4c6e8-0000-4000-8000-000000000002", "a1b2c3d4-0000-4000-8000-000000000001",
           "d4000000-0000-4000-8000-000000000004"]
    rows = [{"tour_id": ids[0], "generated_content_id": "v0", "master_status": "active", "any_approved": True,
             "aa_highlights": '["An elephant ride at dawn"]'},
            {"tour_id": ids[1], "generated_content_id": "v1", "master_status": "inactive",
             "src_itineraries": "Day 2: ride an elephant"},
            {"tour_id": "c0000000-0000-4000-8000-000000000003", "master_status": "active",
             "aa_highlights": '["A quiet walk"]'},
            {"tour_id": ids[2], "generated_content_id": "v2", "master_status": "active", "policy_approved": True,
             "any_approved": True, "aa_highlights": '["Elephant riding by the lake"]'}]
    found = h.scan_rows(rows)
    assert [(f["tour_id"], f["state"], f["fields"]) for f in found] == [
        (ids[0], "active", ["master.highlights"]), (ids[1], "held", ["source.itineraries"]),
        (ids[2], "approved", ["master.highlights"])]
    assert [f["approved_other"] for f in found] == [True, False, False]
    # the pin covers the tours to hold only (id + served version), in any order
    assert h.pin_of(found) == h.pin_of(list(reversed(found))) and h.pin_of(found).startswith("2:")
    assert h.pin_of(found) != h.pin_of([{**found[0], "version_id": "v9"}, found[1]])
    out = "\n".join(h.report(found))
    assert "to_hold=1 already_held=1 approved=1 approved_other=1 total=3" in out and f"PIN={h.pin_of(found)}" in out
    assert "elephant ride" not in out and ids[0][:8] in out and ids[0] not in out


def test_the_script_never_holds_an_approved_tour():
    from scripts.quality import hold_elephant_masters as h
    found = [{"tour_id": "e1000000-0000-4000-8000-000000000001", "state": "approved", "fields": []},
             {"tour_id": "e2000000-0000-4000-8000-000000000002", "state": "held", "fields": []}]
    ok = MagicMock()
    ok.extra = {"held": False, "review_id": "r"}
    hold = AsyncMock(return_value=ok)
    done = asyncio.run(h.hold_all(_pool(AsyncMock()), found, hold=hold, after_commit=AsyncMock()))
    assert done == {"kept": 1} and [c.args[1] for c in hold.await_args_list] == [found[1]["tour_id"]]


def test_the_policy_supersede_closes_only_the_master_tenants_s1_rows():
    sql = " ".join(lp._SUPERSEDE_OTHERS_SQL.split())
    assert "generated_content_id IS NOT NULL" in sql and "tenant_id = $3::uuid" in sql
    pending = " ".join(lp._PENDING_POLICY_SQL.split())
    assert "rq.tenant_id = $2::uuid" in pending and policy_review.row_is_policy_sql("rq") in pending
    approved = lp.policy_approved_sql("pt.generated_content_id")
    assert "review_status = 'approved'" in approved and policy_review.row_is_policy_sql("a") in approved


def test_ensure_policy_review_passes_the_master_tenant_to_the_supersede():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[None, "new-review"])
    conn.execute = AsyncMock()
    assert asyncio.run(lp.ensure_policy_review(conn, "t-1", "v-1", HITS)) == "new-review"
    assert conn.fetchval.await_args_list[0].args[1:] == ("t-1", lp.AA_TENANT)
    assert conn.execute.await_args.args[1:] == ("t-1", "new-review", lp.AA_TENANT)


def test_the_script_apply_needs_the_pin(monkeypatch, capsys):
    from scripts.quality import hold_elephant_masters as h
    monkeypatch.setenv("APPLY", "1")
    monkeypatch.delenv("PIN", raising=False)
    assert asyncio.run(h.main()) == 2
    assert "PIN" in capsys.readouterr().out


def test_the_script_counts_refusals_and_errors_and_goes_on():
    from scripts.quality import hold_elephant_masters as h
    found = [{"tour_id": f"{n}0000000-0000-4000-8000-00000000000{n}", "state": "active", "fields": []}
             for n in (1, 2, 3)]
    ok = MagicMock()
    ok.extra = {"held": True, "review_id": "r"}
    hold = AsyncMock(side_effect=[LifecycleError(409, "master_trashed", "x"), RuntimeError("boom"), ok])
    after = AsyncMock()
    done = asyncio.run(h.hold_all(_pool(AsyncMock()), found, hold=hold, after_commit=after))
    assert done == {"refused_master_trashed": 1, "error": 1, "held": 1} and after.await_count == 1
