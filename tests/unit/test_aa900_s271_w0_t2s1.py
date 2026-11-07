"""S271 W0 (root ADR 0020, AA-900 lane l16) — the S1 + T2 safety interim.

1. elephant-riding policy: the detector matches the policy wording over every field and the source; S1 keeps the tour
   as it is for a human (manual_check, no flag_fix edit); T2 is policy_hold v0 (Master verbatim, 0 LLM calls, a
   review row, the agency still receives it);
2. the hitl exit goes through brand_audit, grounding and the final checks (day guard, degraded hold);
3. the server-side approve gate: an unedited version must pass the auto-publish checks (409 otherwise);
4. the T2 interim profile: the judge is report-only for status, no tie-break / median-of-3 / revalidate judge /
   T3 full-graph rewrite, status from validate + T3 hard codes, no master score fallback, dashes before the check;
5. the S1 judge off the live path for a writer-v2 draft (D9);
6. the S1 Bedrock Batch submit refused while writer v2 is live.

Fixtures use invented tours and places only. No DB, no LLM: every model entry point is patched to raise.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.quality import approve_gate, elephant_policy


@pytest.fixture(autouse=True)
def _w0_defaults(monkeypatch):
    for env in ("T2_INTERIM", "S1_JUDGE_LIVE", "A1_V2_BRAND_FIT_GATE", "T2_JUDGE_REPEAT", "T2_BEST_DRAFT",
                "T2_TARGETED_REPAIR", "T3_BACKGROUND_FACTS"):
        monkeypatch.delenv(env, raising=False)


def _no_llm(*_a, **_k):
    raise AssertionError("no LLM call expected on this path")


# ── 1. the elephant-riding detector ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Enjoy elephant riding in the hills above Varnel.",
    "Spend the morning riding an elephant through the teak forest.",
    "Guests may ride an elephant along the river bank.",
    "An optional elephant ride is offered at the camp.",
    "Two elephant rides and a bamboo raft trip.",
    "A short elephant trek to the waterfall.",
    "Elephant trekking in the Morrow valley.",
    "An elephant-back safari at dawn.",
    "Explore the grassland on elephant back.",
    "The jeep or elephant safari starts at six.",
    "You rode a gentle elephant to the lake.",
    "Ride on elephants to the viewpoint.",
    "A ride on the back of an elephant.",
    # marketing idioms that read as negations but sell the ride (fail safe: a miss breaks the policy)
    "Don't miss the elephant ride through the jungle",
    "No trip is complete without an elephant ride",
    "You will never forget riding an elephant",
    "Enjoy non-stop fun with elephant rides",
    "You cannot leave without riding an elephant",
    "An elephant ride is not to be missed.",
    # elephant back before a ride noun, one-word elephantback, a bare safari outside a jeep-safari country
    "An elephant back ride to the ridge.",
    "Elephantback trek at dawn.",
    "Morning elephant safari in the Varnel grasslands.",
    "A morning elephant safari, riding through the tall grass.",
])
def test_the_detector_matches_the_policy_wording(text):
    assert elephant_policy.mentions_riding(text), text


@pytest.mark.parametrize("text", [
    "No elephant riding at this sanctuary: guests feed and bathe the elephants.",
    "A visit to an elephant sanctuary near Varnel.",
    "Watch wild elephants from the hide.",
    "We never ride elephants here.",
    "Walk beside the herd without riding the elephants.",
    "Elephant riding is not offered at this camp.",
    "The Elephant Terrace of the old city.",
    "We do not offer elephant rides.",
    "A forest walk without an elephant ride.",
    "An elephant-riding-free camp.",
    "Instead of an elephant ride, walk with the herd.",
    # a jeep safari to watch the herds; the elephants coming back is not a ride
    "Afternoon elephant safari in Morrow National Park by 4x4 jeep.",
    "A jeep elephant safari at dusk.",
    "Watch the mahouts bring the elephants back to the river.",
    "",
])
def test_the_detector_skips_negated_and_ride_free_mentions(text):
    assert not elephant_policy.mentions_riding(text), text


def test_a_bare_elephant_safari_in_a_jeep_safari_country_is_not_a_ride():
    """In Sri Lanka "elephant safari" is a jeep safari to watch the herds (reviewer probe: every such tour was a
    policy hold); a riding cue in the clause still makes it a ride there."""
    assert elephant_policy.riding_mentions("Afternoon elephant safari in Varnel park.", "Sri Lanka") == []
    assert elephant_policy.riding_mentions("Elephant safari in Varnel park, then bike riding.", "Sri Lanka") == []
    assert elephant_policy.mentions_riding("Elephant safari on the back of a trained elephant.", "Sri Lanka")
    assert elephant_policy.mentions_riding("Afternoon elephant safari in Varnel park.", "Testland")
    # scan takes the country from the source row
    assert elephant_policy.scan({"summary": "An elephant safari at Varnel."}, {"country": "Sri Lanka"}) == []
    assert elephant_policy.scan({"summary": "An elephant safari at Varnel."}, {"country": "Testland"})


def test_policy_only_hold_needs_a_detector_hit_not_just_the_shared_code():
    """FACT_CHECK_MANUAL_CHECK is also the LLM audit's code for other fact checks (rare wildlife + location)."""
    code = [elephant_policy.POLICY_CODE]
    ride = {"summary": "Ride an elephant at Morrow lake."}
    assert elephant_policy.is_policy_only_hold("manual_check", code, ride, None)
    assert elephant_policy.is_policy_only_hold("manual_check", code, {"summary": "Lake walk."},
                                               {"inclusions": "One elephant ride"})
    assert not elephant_policy.is_policy_only_hold("manual_check", code, {"summary": "Snow leopards at the lake."}, {})
    assert not elephant_policy.is_policy_only_hold("manual_check", code + ["PRODUCT_TRUTH_RISK"], ride, None)
    assert not elephant_policy.is_policy_only_hold("flagged", code, ride, None)


def test_the_old_regex_missed_the_policy_wording():
    """Red on base: the pre-S271 rule (highlights only, elephant back / ride / trek) missed "elephant riding"."""
    import re
    old = re.compile(r'elephant[-\s]?back|elephant\s+ride|elephant\s+trek', re.IGNORECASE)
    assert not old.search("Enjoy elephant riding in the hills") and not old.search("riding an elephant")
    assert elephant_policy.mentions_riding("Enjoy elephant riding in the hills")


def test_scan_reads_every_field_and_the_source_but_not_exclusions_or_keywords():
    generated = {"name": "Varnel Hills", "highlights": ["Tea estates"], "summary": "Quiet villages.",
                 "itineraries": "Day 2 — Morrow\nIn the afternoon, elephant riding by the lake.",
                 "seo_keywords_used": ["elephant riding tour"]}
    hits = elephant_policy.scan(generated, {"exclusions": "Elephant rides", "itineraries": "Day 2 Morrow lake"})
    assert hits == [{"where": "generated", "field": "itineraries", "mention": "elephant riding"}]
    src = elephant_policy.scan({"summary": "Quiet villages."}, {"src": "x", "inclusions": "One elephant ride"})
    assert src == [{"where": "source", "field": "inclusions", "mention": "elephant ride"}]


def test_s1_pre_audit_flags_an_itinerary_ride_and_a_source_only_ride():
    from services.content_generation.brand_audit_node import pre_audit_checks
    page = {"name": "Varnel Hills", "highlights": ["Tea estates at dawn"],
            "itineraries": "Day 2 — Morrow\nIn the afternoon, elephant riding by the lake."}
    assert elephant_policy.POLICY_CODE in pre_audit_checks(page, None)        # base: highlights only → missed
    clean = {"name": "Varnel Hills", "highlights": ["Tea estates at dawn"], "itineraries": "Day 2 — Morrow"}
    assert elephant_policy.POLICY_CODE not in pre_audit_checks(clean, {"itineraries": "Day 2 Morrow walk"})
    assert elephant_policy.POLICY_CODE in pre_audit_checks(clean, {"itineraries": "Day 2 ride an elephant"})


def test_s1_flag_fix_makes_no_edit_on_an_elephant_tour():
    """S1 keeps the tour as it is: a deterministic SEO code would normally send flagged fields to the LLM fixer."""
    from services.content_generation import flag_fix_node as ff
    state = {"generated": {"itineraries": "Day 1 — elephant riding", "seo_meta": "x" * 300},
             "brand_audit_status": "manual_check",
             "brand_audit_codes": [elephant_policy.POLICY_CODE, "ITINERARY_MEAL_TIME_INVENTED"],
             "failure_codes": ["SEO_META_TOO_LONG"], "tour": {}}
    with patch.object(ff, "LLMClient", side_effect=_no_llm):
        out = ff.flag_fix_node(state)
    assert out["fix_pass_applied"] is False and out["generated"] == state["generated"]


def test_s1_flag_fix_still_runs_on_a_non_elephant_fact_check():
    """The policy skip needs a detector hit: an LLM FACT_CHECK_MANUAL_CHECK on a tour with no ride is not skipped."""
    from services.content_generation import flag_fix_node as ff
    state = {"generated": {"itineraries": "Day 1 — snow leopards by the lake", "seo_meta": "x" * 300},
             "brand_audit_status": "manual_check", "brand_audit_codes": [elephant_policy.POLICY_CODE],
             "failure_codes": ["SEO_META_TOO_LONG"], "tour": {}}
    client = MagicMock(side_effect=RuntimeError("fixer reached"))
    with patch.object(ff, "LLMClient", client):
        try:
            ff.flag_fix_node(state)
        except RuntimeError:
            pass
    assert client.called                      # the fixer was reached (on the elephant tour above it is not)


# ── 1b. T2 policy_hold v0 ───────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


def _pool(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


_MASTER = {"id": "pt-1", "tour_id": "11111111-1111-1111-1111-111111111111", "aa_name": "Varnel Hills — 4 Days",
           "aa_subtitle": "Tea country and lakes", "aa_summary": "Four quiet days in the Varnel hills.",
           "aa_description": "", "aa_highlights": '["Tea estates", "Morrow lake"]',
           "aa_itineraries": "Day 1 — Varnel\nArrive and walk the tea rows.\nDay 2 — Morrow\nA lakeside walk.",
           "seo_title": "Varnel Hills Tea Country and Lakes Tour",
           "seo_meta": "Four quiet days in the Varnel hills, with tea estates, village walks and a calm lakeside "
                       "afternoon by Morrow lake before you travel home.",
           "seo_keywords_used": None, "country": "Testland", "duration": "4 days",
           "src_name": "Varnel 4D", "src_subtitle": None, "src_summary": None, "src_description": None,
           "src_highlights": None, "src_itineraries": "Day 2: optional elephant riding at Morrow lake",
           "inclusions": None, "activities": None, "price_raw": None}


def test_t2_policy_hold_master_verbatim_zero_llm_calls_and_a_review_row():
    """'elephant riding' in the itinerary → policy_hold: no writer / judge / T3 / Jev call (base: a full rewrite)."""
    from services.jobs import t2_rewrite_job as job
    conn = _Conn()
    tour_dict = job.source_tour_dict(_MASTER)
    rewrite = AsyncMock(side_effect=AssertionError("the T2 graph must not run on a policy hold"))
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch("shared.llm_client.client.LLMClient.generate", side_effect=_no_llm), \
            patch("shared.llm_client.decide.decide", side_effect=_no_llm):
        diag = asyncio.run(job._rewrite_and_save(_pool(conn), _MASTER, tour_dict, {"forbidden_words": []},
                                                 "ten-1", "ver-1", "pt-1"))
    rewrite.assert_not_called()
    assert diag["policy_hold"]["reason"] == "elephant_riding" and diag["policy_hold"]["llm_calls"] == 0
    update, insert = conn.executed
    assert "UPDATE gold_aa_internal.tenant_tour_versions" in update[0] and "'needs_review'" in update[0]
    saved = json.loads(update[1][0])
    assert saved["summary"] == _MASTER["aa_summary"] and saved["subtitle"] == _MASTER["aa_subtitle"]
    assert saved["highlights"] == ["Tea estates", "Morrow lake"]
    assert saved["itineraries"].replace("Day 1: ", "Day 1 — ").replace("Day 2: ", "Day 2 — ") \
        == _MASTER["aa_itineraries"]                               # verbatim prose (AA-778 day marker only)
    assert saved["name"] == _MASTER["aa_name"]                    # the Master name keeps its dash
    assert json.loads(update[1][1])["policy_hold"]["hits"][0]["where"] == "source"
    assert "INSERT INTO silver_aa_internal.review_queue" in insert[0]
    assert json.loads(insert[1][4])[0]["check_id"] == "policy:elephant_riding"


def test_t2_policy_hold_reads_the_master_text_too():
    from services.jobs import t2_rewrite_job as job
    row = {**_MASTER, "src_itineraries": "Day 2: lake walk", "aa_summary": "Ride an elephant at Morrow lake."}
    hits = job.policy_hold_hits(row, job.source_tour_dict(row))
    assert hits == [{"where": "master", "field": "summary", "mention": "Ride an elephant"}]
    clean = {**_MASTER, "src_itineraries": "Day 2: lake walk"}
    assert job.policy_hold_hits(clean, job.source_tour_dict(clean)) == []


def test_t2_pt_sql_reads_the_raw_source_for_the_detector_only():
    from services.jobs import t2_rewrite_job as job
    for col in ("rt.src_itineraries", "rt.src_highlights", "rt.inclusions", "rt.activities"):
        assert col in job._PT_SQL
    assert set(job.source_tour_dict(_MASTER)) == {"name", "subtitle", "summary", "description", "highlights",
                                                  "itineraries", "seo_title", "seo_meta", "country", "duration"}


# ── 2. the hitl exit reaches the checks ─────────────────────────────────────────────────────────────────────────────

def _run_graph(t2_drafts: bool, calls: list, *, degraded_hard: bool = False):
    from services.content_generation import graph as g

    def gen(state):
        calls.append("generate")
        return {**state, "generated": {"name": "Varnel", "summary": "Quiet days.", "itineraries": ""}}

    def val(state):
        calls.append("validate")
        return {**state, "quality_score": 3.0, "failure_codes": ["MISSING_FIELD"]}

    def judge(state):
        calls.append("judge")
        return state

    def audit(state):
        calls.append("brand_audit")
        return {**state, "brand_audit_status": "flagged", "brand_audit_codes": []}

    def ground(state):
        calls.append("grounding")
        out = {**state, "grounding_ran": True, "grounding_violations": []}
        if degraded_hard:
            out["degraded"] = [{"name": "grounding_not_run", "severity": "hard", "detector": "grounding",
                                "count": 1}]
        return out

    def fix(state):
        calls.append("flag_fix")
        return state

    with patch.object(g, "generate_node", gen), patch.object(g, "validate_node", val), \
            patch.object(g, "judge_node", judge), patch.object(g, "brand_audit_node", audit), \
            patch.object(g, "grounding_node", ground), patch.object(g, "flag_fix_node", fix):
        graph = g.build_graph(t2_drafts=t2_drafts)
        return graph.invoke({"tour": {"name": "Varnel"}, "retry_count": 0, "is_tenant_rewrite": t2_drafts,
                             "tenant_id": "ten-1" if t2_drafts else None})


@pytest.mark.parametrize("t2_drafts", [False, True])
def test_the_hitl_path_reaches_the_checks(t2_drafts):
    """Red on base: the hitl exit went straight to END (no brand audit, no grounding, no final checks)."""
    calls: list = []
    out = _run_graph(t2_drafts, calls)
    assert calls.count("generate") == 3                     # the retries ran out below the line
    assert "brand_audit" in calls and "grounding" in calls  # the checks ran on the hitl draft
    assert "flag_fix" not in calls                          # no source-blind LLM rewrite of a hitl draft
    assert out["exit_path"] == "hitl"


def test_the_hitl_path_applies_the_degraded_hold():
    calls: list = []
    out = _run_graph(False, calls, degraded_hard=True)
    assert "DEGRADED_DETECTOR" in out["failure_codes"] and out["brand_audit_status"] == "manual_check"


def test_resumed_and_seed_graphs_close_the_hitl_exit_too():
    from services.content_generation import graph as g
    for build in (g.build_graph_resumed, g.build_graph_from_generated, lambda: g.build_graph(t2_drafts=True)):
        edges = {(e.source, e.target) for e in build().get_graph().edges}
        assert ("hitl_exit", "brand_audit") in edges
        assert not any(src in ("llm_judge", "restore_best") and tgt == "__end__" for src, tgt in edges)


def test_the_version_metadata_records_the_exit_path():
    from api.routers.admin_pipeline import _build_generated_metadata
    meta = _build_generated_metadata({"exit_path": "hitl", "quality_score": 4.0}, brand_rule_id=None, brand_name="",
                                     seo_mode="none", model_used="m", llm_cost_usd=0.0, dataforseo_used=False)
    assert meta["exit_path"] == "hitl"


# ── 3. the server-side approve gate ─────────────────────────────────────────────────────────────────────────────────

GCID = "22222222-2222-2222-2222-222222222222"
REVIEW_ID = "33333333-3333-3333-3333-333333333333"


def _gate_row(**kw):
    row = {"generated_content_id": GCID, "human_edited": False, "fix_pass_applied": False,
           "metadata": json.dumps({"grounding": {"found": 0, "violations": []}, "writer_version": "v2"}),
           "score_overall": 9.5, "failure_codes": "[]", "brand_audit_status": "pass", "brand_audit_codes": "[]"}
    row.update(kw)
    return row


@pytest.mark.parametrize("kw, code", [
    ({"failure_codes": '["FORBIDDEN_WORD"]'}, "hard_codes"),
    ({"failure_codes": '["DEGRADED_DETECTOR"]', "brand_audit_status": "manual_check"}, "hard_codes"),
    ({"failure_codes": '["ITINERARY_DAY_COUNT_MISMATCH"]'}, "hard_codes"),     # a v2 hard code
    ({"score_overall": 5.0}, "score_below_floor"),
    ({"score_overall": None}, "no_score"),
    ({"metadata": "{}"}, "grounding_not_run"),
    ({"metadata": {"grounding": {"violations": [{"code": "X"}]}}}, "unsupported_sentences"),
    ({"brand_audit_status": "flagged", "brand_audit_codes": '["SUBTITLE_CITY_LIST"]'}, "brand_flagged_unfixed"),
    ({"brand_audit_status": "manual_check", "brand_audit_codes": '["BRAND_AUDIT_UNAVAILABLE"]'}, "manual_check"),
    ({"brand_audit_status": "manual_check",
      "brand_audit_codes": json.dumps([elephant_policy.POLICY_CODE, "ITINERARY_MEAL_TIME_INVENTED"])},
     "manual_check"),
])
def test_approve_gate_blockers(kw, code):
    assert code in [b["code"] for b in approve_gate.blockers(_gate_row(**kw))]


def _keep_row(**kw):
    """An elephant tour held only by the policy: the code AND a ride in the stored text (or the raw source)."""
    return _gate_row(brand_audit_status="manual_check", brand_audit_codes=json.dumps([elephant_policy.POLICY_CODE]),
                     aa_itineraries="Day 1 — Varnel\nDay 2 — Morrow\nAn optional elephant ride by the lake.", **kw)


def test_approve_gate_passes_a_clean_version_and_a_policy_keep():
    assert approve_gate.blockers(_gate_row()) == []
    keep = _keep_row()
    assert approve_gate.blockers(keep) == []
    assert approve_gate.is_policy_keep(approve_gate.stored_result(keep))
    fixed = _gate_row(brand_audit_status="flagged", fix_pass_applied=True)
    assert approve_gate.blockers(fixed) == []


def _approve(conn, body=None):
    from api.routers import v1_pipeline
    req = MagicMock()
    req.app.state.pool = _pool(conn)
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        try:
            return asyncio.run(v1_pipeline.approve_review(REVIEW_ID, req, None, body)), export
        except HTTPException as e:
            return e, export


def test_an_unedited_approve_with_a_hard_code_is_409():
    """Red on base: the claim ran and the version was exported."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_gate_row(failure_codes='["FORBIDDEN_WORD"]')])
    out, export = _approve(conn)
    assert isinstance(out, HTTPException) and out.status_code == 409
    assert out.detail.startswith("approve_blocked:") and "FORBIDDEN_WORD" in out.detail
    assert out.headers == {"X-Error-Code": "approve_blocked"}
    assert conn.fetchrow.await_count == 1                   # no claim
    conn.execute.assert_not_called()
    export.assert_not_called()


def test_a_source_only_ride_is_a_policy_keep_too():
    keep = _gate_row(brand_audit_status="manual_check", brand_audit_codes=json.dumps([elephant_policy.POLICY_CODE]),
                     src_itineraries="Day 2: ride an elephant at Morrow lake", country="Testland")
    assert approve_gate.blockers(keep) == []


def test_a_non_elephant_fact_check_is_not_a_policy_keep():
    """Red before the fix: any manual_check whose only brand code was FACT_CHECK_MANUAL_CHECK (the LLM audit's code
    for a rare-wildlife / location mismatch too) skipped the manual_check blocker and was approved unedited."""
    row = _gate_row(brand_audit_status="manual_check", brand_audit_codes=json.dumps([elephant_policy.POLICY_CODE]),
                    aa_summary="Snow leopards on the shore of Morrow lake.", src_summary="A lake walk.")
    assert not approve_gate.is_policy_keep(approve_gate.stored_result(row))
    found = approve_gate.blockers(row)
    assert [b["code"] for b in found] == ["manual_check"] and elephant_policy.POLICY_CODE in found[0]["detail"]


def test_an_unedited_approve_of_a_non_elephant_fact_check_is_409():
    conn = AsyncMock()
    row = _gate_row(brand_audit_status="manual_check", brand_audit_codes=json.dumps([elephant_policy.POLICY_CODE]))
    conn.fetchrow = AsyncMock(side_effect=[row])
    out, export = _approve(conn)
    assert isinstance(out, HTTPException) and out.status_code == 409 and "manual check" in out.detail
    export.assert_not_called()


def test_an_unedited_approve_of_a_policy_keep_tour_publishes():
    conn = AsyncMock()
    keep = _keep_row()
    conn.fetchrow = AsyncMock(side_effect=[keep, {"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    out, export = _approve(conn)
    assert out["status"] == "approved" and out["policy_keep"] is True and out["override_blockers"] == []
    export.assert_awaited_once_with(GCID, contract_exit="approve")


def test_an_override_reason_approves_and_is_recorded():
    from api.routers.v1_pipeline import ReviewApproveBody
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_gate_row(metadata="{}"),
                                           {"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    short, _ = _approve(conn, ReviewApproveBody(override_reason="ok"))      # too short: still blocked
    assert isinstance(short, HTTPException) and short.status_code == 409
    conn.fetchrow = AsyncMock(side_effect=[_gate_row(metadata="{}"),
                                           {"generated_content_id": GCID, "step_fn_task_token": None}])
    out, export = _approve(conn, ReviewApproveBody(override_reason="Checked against the supplier sheet by hand"))
    assert out["status"] == "approved" and out["override_blockers"] == ["grounding_not_run"]
    note = [c.args for c in conn.execute.await_args_list if "reviewer_notes" in c.args[0]]
    assert note and "Checked against the supplier sheet by hand" in note[0][2] and "grounding_not_run" in note[0][2]
    export.assert_awaited_once_with(GCID, contract_exit="approve")


def test_a_human_edited_version_keeps_the_revalidation_gate_only():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_gate_row(human_edited=True, failure_codes='["FORBIDDEN_WORD"]'),
                                           {"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    out, _ = _approve(conn)
    assert out["status"] == "approved"


def test_the_gate_query_reads_pending_reviews_on_a_live_source():
    from api.routers import v1_pipeline
    from shared.tour_liveness import live_source_exists_sql
    sql = v1_pipeline._APPROVE_GATE_SQL.format(live=live_source_exists_sql("rq.tour_id"))
    assert "rq.review_status = 'pending'" in sql and live_source_exists_sql("rq.tour_id") in sql
    assert "ORDER BY q.evaluated_at DESC" in sql
    # the elephant-policy inputs: the stored text and the raw source, keyed like the detector's row mappers
    for col in ("aa_itineraries", "aa_summary", "seo_meta", "rt.src_itineraries", "rt.inclusions", "rt.country",
                "LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id"):
        assert col in sql, col


# ── 3b. the re-validation gate of a human edit (graph.human_edit_gate_node) ─────────────────────────────────────────

def _edit_state(**kw):
    state = {"quality_score": 9.0, "failure_codes": [], "brand_audit_status": "manual_check",
             "brand_audit_codes": [elephant_policy.POLICY_CODE], "writer_version": "v2",
             "generated": {"summary": "Four quiet days in the Varnel hills.",
                           "itineraries": "Day 2 — Morrow\nAn optional elephant ride by the lake."},
             "tour": {"country": "Testland", "itineraries": "Day 2: optional elephant riding at Morrow lake"}}
    state.update(kw)
    return state


def test_an_edited_elephant_tour_can_pass_the_revalidation_gate():
    """Red before the fix: the detector re-hits the source on every re-validation, so an edited elephant tour was
    manual_check forever (the elephant deadlock) and only an API-only override could approve it."""
    from services.content_generation.graph import human_edit_gate_node
    assert human_edit_gate_node(_edit_state())["revalidate_passed"] is True
    # the reviewer took the ride out of the page: the source still offers it, still the policy's review
    edited = _edit_state(generated={"summary": "Four quiet days.", "itineraries": "Day 2 — Morrow lake walk"})
    assert human_edit_gate_node(edited)["revalidate_passed"] is True


@pytest.mark.parametrize("kw", [
    {"generated": {"summary": "Snow leopards by Morrow lake."}, "tour": {"summary": "A lake walk."}},   # no ride
    {"brand_audit_codes": [elephant_policy.POLICY_CODE, "PRODUCT_TRUTH_RISK"]},
    {"quality_score": 5.0},
    {"failure_codes": ["FORBIDDEN_WORD"]},
])
def test_the_revalidation_gate_still_fails_everything_else(kw):
    from services.content_generation.graph import human_edit_gate_node
    assert human_edit_gate_node(_edit_state(**kw))["revalidate_passed"] is False


def test_the_revalidation_graph_passes_an_edited_elephant_tour_end_to_end():
    """The re-validation graph (validate → judge → brand_audit → human_edit_gate) on an edited elephant tour: the
    brand audit marks it manual_check (the source scan), the gate lets the human edit through."""
    from services.content_generation import brand_audit_node as ba
    state = _edit_state(brand_audit_status="", brand_audit_codes=[])
    audited = ba._audit_from_judge({**state, "judge_brand_fit": None}, state["generated"])
    assert audited["brand_audit_status"] == "manual_check"
    assert audited["brand_audit_codes"] == [elephant_policy.POLICY_CODE]
    from services.content_generation.graph import human_edit_gate_node
    assert human_edit_gate_node({**state, **audited})["revalidate_passed"] is True


# ── 4. the T2 interim profile ───────────────────────────────────────────────────────────────────────────────────────

def _brand_fit(score, feedback="reads generic"):
    from services.content_generation.brand_fit import BrandFitResult
    return BrandFitResult(brand_fit_score=score, cross_brand_distinct=score, mission_present=True,
                          judge_score=score, feedback=feedback, model_used="judge-model", cost_usd=0.001,
                          input_tokens=10, output_tokens=5, stop_reason="end_turn", account=None,
                          fallback_used=False)


def _t2_judge(score, monkeypatch=None):
    from services.content_generation import judge_node as jn
    state = {"generated": {"name": "N", "summary": "S", "highlights": []}, "quality_score": 9.0, "feedback": "",
             "cost_usd": 0.0, "brand_core_idea": "slow travel", "brand_customer_mindset": "y",
             "brand_voice_examples": ["z"], "tour": {"tour_id": "t-1"}, "is_tenant_rewrite": True,
             "tenant_id": "ten-1"}
    fit = MagicMock(return_value=_brand_fit(score))
    with patch.object(jn, "score_brand_fit", fit), patch.object(jn, "_write_judge_rows") as rows, \
            patch.object(jn, "_tie_break") as tie:
        out = jn.judge_node(state)
    return out, rows.call_args.args[0][0]["quality_signal"], tie, fit


def test_t2_status_is_independent_of_the_judge_score():
    """Red on base: a low brand fit capped the T2 score (min(validate, judge)) and drove the retry."""
    out, signal, tie, fit = _t2_judge(4.0)
    assert out["quality_score"] == 9.0 and out["feedback"] == ""     # validate's score, no judge feedback
    assert out["judge_brand_fit"] == 4.0 and signal["report_only"] is True
    tie.assert_not_called()
    assert fit.call_args.kwargs.get("near_band_repeat") is False and "repeat" not in fit.call_args.kwargs


def test_t2_median3_switch_is_ignored_under_the_interim_profile(monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median3")
    _, _, _, fit = _t2_judge(7.0)
    assert "repeat" not in fit.call_args.kwargs


def test_t2_interim_off_restores_the_judge_gate(monkeypatch):
    monkeypatch.setenv("T2_INTERIM", "off")
    out, signal, _, _ = _t2_judge(4.0)
    assert out["quality_score"] == 4.0 and "report_only" not in signal


def test_near_band_repeat_off_is_one_judge_call():
    from services.content_generation import brand_fit
    calls = MagicMock(return_value=_brand_fit(7.0))
    with patch.object(brand_fit, "_judge", calls), patch.object(brand_fit, "_seeded_model", return_value=False):
        brand_fit.score_brand_fit({"brand_core_idea": "x"}, {"name": "N"}, stage="t2_judge", near_band_repeat=False)
        assert calls.call_count == 1
        brand_fit.score_brand_fit({"brand_core_idea": "x"}, {"name": "N"}, stage="t2_judge")
        assert calls.call_count == 4                                 # the AA-714 near-band median of 3


def test_t2_revalidate_makes_no_judge_call():
    from services.content_generation import graph as g
    state = {"generated": {"name": "N", "summary": "Quiet days."}, "fix_pass_applied": True,
             "is_tenant_rewrite": True, "tour": {}, "quality_score": 8.0}
    judge = MagicMock(side_effect=_no_llm)
    with patch.object(g, "validate_node", lambda s: {**s, "quality_score": 8.0}), patch.object(g, "judge_node", judge):
        g.revalidate_node(state)
    judge.assert_not_called()


def test_t2_status_from_validate_and_t3_hard_codes():
    from services.jobs.t2_rewrite_job import _t2_status
    assert _t2_status(9.0) == "ai_generated"
    assert _t2_status(9.0, ["FORBIDDEN_WORD"]) == "needs_review"
    assert _t2_status(5.0) == "needs_review"


def _res(generated, score):
    return {"generated": dict(generated), "quality_score": score, "judge_feedback": "add more detail",
            "status": "success"}


def test_t3_runs_no_full_graph_rewrite_round():
    """Red on base: a structural T3 failure re-ran the whole T2 graph (up to 2 rounds x 3 drafts)."""
    from services.acp_produce import tenant_pipeline as tp
    rewrite = AsyncMock(side_effect=AssertionError("no full-graph rewrite round under the interim profile"))
    with patch("api.routers.v1_pipeline._rewrite_tour", rewrite), \
            patch.object(tp, "_t3_structural_issues", return_value=["MISSING_FIELD"]):
        out = asyncio.run(tp.run_t3_qa_gate({"name": "T"}, ["src"], _res({"summary": "Quiet days."}, 8.0), {},
                                            tenant_id="t1"))
    rewrite.assert_not_called()
    assert out["passed"] is False and out["stopped"] == "full_rewrite_off" and out["attempts"] == 0
    assert out["structural_issues"] == ["MISSING_FIELD"]


def test_t3_revision_feedback_has_no_judge_voice_under_the_interim_profile():
    from services.acp_produce import tenant_pipeline as tp
    base = {"structural": ["MISSING_FIELD"], "grounding": [], "score": 5.0, "result": _res({}, 5.0)}
    assert "BRAND VOICE FEEDBACK" not in tp._revision_feedback(base)


def test_t3_fixes_dashes_before_its_checks():
    """Red on base: the em-dash fix ran after the last check, on text nothing re-checked."""
    from services.acp_produce import tenant_pipeline as tp
    seen = {}

    def structural(generated, _tour, _rules):
        seen["summary"] = generated["summary"]
        return []

    result = _res({"summary": "Quiet days — tea rows and a lake walk."}, 8.0)
    with patch.object(tp, "_t3_structural_issues", side_effect=structural):
        out = asyncio.run(tp.run_t3_qa_gate({"name": "T"}, ["src"], result, {}, tenant_id="t1"))
    assert "—" not in seen["summary"] and out["em_dash_fixes"] >= 1 and out["passed"] is True


# ── 5. the S1 judge off the live path (D9) ──────────────────────────────────────────────────────────────────────────

_A1_V2 = {"generated": {"name": "N", "summary": "S", "highlights": ["Tea estates"], "itineraries": "Day 1 — Varnel"},
          "quality_score": 9.0, "feedback": "", "cost_usd": 0.0, "brand_core_idea": "x",
          "brand_customer_mindset": "y", "brand_voice_examples": ["z"], "tour": {"tour_id": "t-1"},
          "writer_version": "v2"}


def test_the_s1_judge_makes_no_call_on_a_v2_draft():
    from services.content_generation import judge_node as jn
    with patch.object(jn, "score_brand_fit", side_effect=_no_llm):
        out = jn.judge_node(dict(_A1_V2))
    assert out["quality_score"] == 9.0 and "judge_brand_fit" not in out


def test_the_s1_brand_audit_stays_deterministic_without_the_judge():
    """No s1_brand_audit LLM call replaces the judge: the pre-audit codes, as when the judge ran."""
    from services.content_generation import brand_audit_node as ba
    state = {**_A1_V2, "generated": {**_A1_V2["generated"], "itineraries": "Day 1 — elephant riding at dawn"}}
    with patch.object(ba, "LLMClient", side_effect=_no_llm):
        out = ba.brand_audit_node(state)
    assert out["brand_audit_status"] == "manual_check" and elephant_policy.POLICY_CODE in out["brand_audit_codes"]


def test_a_v1_draft_is_still_judged_and_s1_judge_live_restores_the_call(monkeypatch):
    from services.content_generation import judge_node as jn
    v1 = {k: v for k, v in _A1_V2.items() if k != "writer_version"}
    with patch.object(jn, "score_brand_fit", return_value=_brand_fit(5.0)), patch.object(jn, "_write_judge_rows"):
        assert jn.judge_node(dict(v1))["judge_brand_fit"] == 5.0
        monkeypatch.setenv("S1_JUDGE_LIVE", "on")
        assert jn.judge_node(dict(_A1_V2))["judge_brand_fit"] == 5.0


# ── 6. the S1 Bedrock Batch submit is refused while writer v2 is live ───────────────────────────────────────────────

def test_s1_batch_submit_is_refused_while_writer_v2_is_on(monkeypatch):
    from services.content_generation import s1_batch
    from shared.llm_client.bedrock_batch import BatchUnavailable
    monkeypatch.setenv("S1_WRITER_V2", "on")
    with pytest.raises(BatchUnavailable, match="writer v2"):
        asyncio.run(s1_batch.submit_s1_batch(["t-1"]))
    monkeypatch.setenv("S1_WRITER_V2", "off")
    assert s1_batch.writer_v2_refusal() is None


def test_s1_batch_endpoint_answers_409_before_the_kill_switch(monkeypatch):
    from api.routers import admin_pipeline
    monkeypatch.setenv("S1_WRITER_V2", "on")
    monkeypatch.setenv("BATCH_SUBMIT_SWITCH", "on")
    req = admin_pipeline.S1BatchSubmitRequest(tour_ids=["t-1"], batch_id="44444444-4444-4444-4444-444444444444")
    with patch.object(admin_pipeline, "verify_admin_secret"), \
            patch("services.content_generation.s1_batch.submit_s1_batch", side_effect=_no_llm):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(admin_pipeline.submit_s1_batch_endpoint(req, MagicMock(), "secret"))
    assert exc.value.status_code == 409 and "writer v2" in exc.value.detail
