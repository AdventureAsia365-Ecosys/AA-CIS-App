"""AA-990 N3 (S252) — the Japan "TRANS-TOHOKU Bike Tour" day loss: a 12-day source, a published v2 page with Days 1-4.

Root cause: the source's Day 5-12 lines start with the spreadsheet cell's opening quote ('"Day 5 / -Stage 4- ...'),
which the "Day N" marker regex did not accept. The source read as Days 1-4 (Day 4's block swallowed Days 5-12), so
the writer v2 day plan asked for 4 days, validate compared 4 with 4, and the day oracle filed "12 DAYS vs 4 markers"
as a source contradiction (shadow). No step after the gate dropped a day.

1. The marker parser reads quoted / bracketed "Day N" lines: 12 source days, a 12-day plan.
2. validate fires ITINERARY_DAY_COUNT_MISMATCH on the 4-day page and the publish gate holds it (v2 draft).
3. A fix pass that returns 4 of the 12 days is reverted (repair contract ``days_kept``).
4. The final day guard re-checks the persisted text after every edit: an edit after the last validate that drops
   days puts the code back on failure_codes and holds a v2 draft; the normal 12-day path is untouched.
5. The s1_rewrite job result says why a version was held.
Mock-only: no AWS, DB or model call. The source below is a trimmed, shape-faithful stand-in for the real row.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.content_generation import graph as g
from services.content_generation.day_parse import source_day_markers

CODE = "ITINERARY_DAY_COUNT_MISMATCH"

_STAGES = ["Nasu to Aizu Higashiyama Onsen", "Aizu Higashiyama Onsen to Iizaka Onsen",
           "Iizaka Onsen to Kaminoyama Onsen", "Kaminoyama Onsen to Hijiori Onsen", "Hijiori Onsen to Chokai Onsen",
           "Chokai Onsen to Kakunodate", "Kakunodate to Nyuto Onsen", "Nyuto Onsen to Yuze Onsen",
           "Yuze Onsen to Oirase Gorge", "Oirase Gorge to Aomori"]


def _japan_source() -> str:
    lines = ["Day 1", "Meet at a hotel in Nasu, Tochigi. Check-in for the tour and arrange bike rentals."]
    for day in range(2, 12):
        quote = '"' if day >= 5 else ""          # the real row: Days 5-12 keep the cell's opening quote
        lines += [f"{quote}Day {day} / -Stage {day - 1}- {_STAGES[day - 2]} [80km, 1,000m up]",
                  f"Ride stage {day - 1} through the valley and stay at a hot spring inn."]
    lines += ['"Day 12 / Tour Conclusion', "Breakfast at the hotel, then the tour ends in Aomori."]
    return "\n".join(lines)


JAPAN_TOUR = {"name": "TRANS-TOHOKU Bike Tour", "duration": "12 DAYS", "itineraries": _japan_source()}


def _page(days) -> str:
    return "\n\n".join(f"Day {d} — Stage {d}\nThe group rides stage {d} and stays at a hot spring inn."
                       for d in days)


def _generated(days) -> dict:
    return {"name": "Trans-Tohoku Bike Tour", "summary": "A cycling tour across Tohoku.",
            "highlights": ["a", "b", "c", "d"], "itineraries": _page(days)}


# ── 1. the parser ────────────────────────────────────────────────────────────────────────────────

def test_quoted_day_lines_are_markers_and_the_plan_has_12_days():
    assert [first for first, _last, _i in source_day_markers(JAPAN_TOUR["itineraries"])] == list(range(1, 13))
    from services.content_generation.s1_writer_v2 import day_plan
    plan = day_plan(JAPAN_TOUR)
    assert plan["mode"] == "source_days" and [d["day"] for d in plan["days"]] == list(range(1, 13))
    # the quote is stripped with the marker: the day's source block starts at its own text
    assert plan["days"][4]["source"].startswith("Stage 4- Kaminoyama") or "Kaminoyama" in plan["days"][4]["source"]


@pytest.mark.parametrize("text,expected", [
    ('"Day 1: a\n"Day 2: b', [(1, 1, 0), (2, 2, 1)]),
    ("“Day 1: a\n‘Day 2 b", [(1, 1, 0), (2, 2, 1)]),
    ("(Day 1) a\n[Day 2] b", [(1, 1, 0), (2, 2, 1)]),
    ('Easy "Day 82" ride\nDay 1: a', [(1, 1, 1)]),            # still line-anchored: a mid-line quote is not one
])
def test_leading_quotes_and_brackets(text, expected):
    assert source_day_markers(text) == expected


def test_the_day_oracle_no_longer_calls_the_quoted_source_a_contradiction():
    from services.quality import day_oracle
    codes = {f["code"] for f in day_oracle.check(JAPAN_TOUR["itineraries"], "12 DAYS", _page(range(1, 5)))}
    assert day_oracle.SOURCE_DAY_CONTRADICTION not in codes
    assert day_oracle.DAY_COUNT_VS_DURATION in codes


# ── 2. validate + the publish gate on the published 4-day v2 page ────────────────────────────────

def test_validate_fires_the_code_and_the_v2_page_is_held():
    from api.routers.admin_pipeline import _is_publishable

    state = {"tour": JAPAN_TOUR, "generated": _generated(range(1, 5)), "retry_count": 0, "writer_version": "v2"}
    out = g.validate_node(state)
    assert CODE in out["failure_codes"]
    assert "day 5/6/7/8/9/10/11/12" in out["feedback"]
    held = {**out, "brand_audit_status": "fixed", "quality_score": 10.0}
    assert not _is_publishable(held)


# ── 3. a fix pass that returns 4 of the 12 days ──────────────────────────────────────────────────

def _llm_returning(payload: dict):
    resp = MagicMock(content=json.dumps(payload), model_used="sonnet", cost_usd=0.001, input_tokens=10,
                     output_tokens=5, stop_reason="end_turn", satellite_account=None, fallback_used=False)
    client = MagicMock()
    client.generate.return_value = resp
    return client


def test_a_fix_pass_that_drops_8_days_is_reverted():
    from services.content_generation.flag_fix_node import flag_fix_node

    full = _generated(range(1, 13))
    state = {"brand_audit_status": "flagged", "brand_audit_codes": ["ITINERARY_MEAL_TIME_INVENTED"],
             "brand_audit_issues": ["a meal time the source does not give"], "failure_codes": [],
             "generated": dict(full), "tour": JAPAN_TOUR, "writer_version": "v2"}
    client = _llm_returning({"itineraries": _page(range(1, 5))})
    with patch("services.content_generation.flag_fix_node.LLMClient", return_value=client), \
         patch("services.content_generation.flag_fix_node.record_call_sync"):
        out = flag_fix_node(state)
    assert out["generated"]["itineraries"] == full["itineraries"]          # all 12 days kept
    assert "itineraries" not in out["fix_pass_fields"]
    assert any(d.get("name") == "repair_contract" for d in out["degraded"])


# ── 4. the final day guard (last step of the A1 graph) ───────────────────────────────────────────

def _after_revalidate(days, **over):
    s = {"tour": JAPAN_TOUR, "generated": _generated(days), "writer_version": "v2", "quality_score": 10.0,
         "brand_audit_status": "fixed", "fix_pass_applied": True, "failure_codes": [], "feedback": "",
         "revalidate_ran": True, "revalidate_passed": True, "grounding_ran": False}
    s.update(over)
    return s


def test_an_edit_after_the_last_validate_that_drops_days_is_held():
    """The Japan shape on a v2 draft: validate saw 12 days (score 10, no code), then the post-revalidate sentence
    re-repair hands back 4 — the guard puts the code back and the gate holds the page."""
    from api.routers.admin_pipeline import _is_publishable, _surviving_hard_codes

    state = _after_revalidate(range(1, 13), grounding_ran=True,
                              grounding_violations=[{"code": "GROUNDING_NOVEL_NUMBER", "field": "itineraries"}])
    shortened = _generated(range(1, 5))
    with patch.object(g, "regrounding", return_value={"violations": [{"code": "GROUNDING_NOVEL_NUMBER"}]}), \
         patch.object(g, "repair_and_recheck", return_value={"generated": shortened, "violations": [],
                                                             "fields": ["itineraries"], "cost_usd": 0.0,
                                                             "check": None, "contract": None}):
        out = g._apply_grounding_recheck(state)
    assert out["generated"]["itineraries"] == shortened["itineraries"]
    assert CODE in out["failure_codes"]
    assert out["day_guard_missing"] == list(range(5, 13))
    assert out["revalidate_passed"] is False
    assert _surviving_hard_codes(out) == {CODE}
    assert not _is_publishable(out)


def test_revalidate_end_to_end_holds_a_4_day_v2_page():
    """revalidate_node with a 4-day page and a validate stub that misses it (score 10, no code): the guard is the
    last word."""
    with patch.object(g, "validate_node", lambda st: {**st, "quality_score": 10.0, "failure_codes": []}), \
         patch.object(g, "judge_node", lambda st: st):
        out = g.revalidate_node(_after_revalidate(range(1, 5), revalidate_ran=False, revalidate_passed=False))
    assert out["brand_audit_status"] == "fixed"         # the brand status is unchanged …
    assert CODE in out["failure_codes"] and out["revalidate_passed"] is False   # … the day guard holds it


def test_a_validate_code_on_a_v2_draft_also_turns_revalidate_passed_false():
    out = g.final_day_guard(_after_revalidate(range(1, 5), failure_codes=[CODE]))
    assert out["failure_codes"] == [CODE] and out["revalidate_passed"] is False


def test_normal_12_day_path_is_unchanged():
    from api.routers.admin_pipeline import _is_publishable

    state = _after_revalidate(range(1, 13))
    out = g._apply_grounding_recheck(state)
    assert out["failure_codes"] == [] and out["revalidate_passed"] is True
    assert "day_guard_missing" not in out
    assert out["generated"] == state["generated"]
    assert _is_publishable(out)


def test_v1_and_tenant_runs_keep_their_rules():
    from api.routers.admin_pipeline import _is_publishable

    v1 = g.final_day_guard(_after_revalidate(range(1, 5), writer_version=None))
    assert CODE in v1["failure_codes"]                  # recorded …
    assert v1["revalidate_passed"] is True and _is_publishable(v1)   # … but soft on v1, as decided in S249
    tenant = _after_revalidate(range(1, 5), is_tenant_rewrite=True)
    assert g.final_day_guard(tenant) is tenant          # T3 checks a tenant rewrite


def test_no_source_markers_or_an_empty_draft_is_a_no_op():
    no_markers = _after_revalidate(range(1, 3), tour={"duration": "2 days", "itineraries": "Two easy days."})
    assert g.final_day_guard(no_markers) is no_markers
    empty = _after_revalidate(range(1, 3), generated={})
    assert g.final_day_guard(empty) is empty


def test_validate_and_the_guard_share_one_check():
    gap = g.itinerary_day_gap(JAPAN_TOUR, _page(range(1, 5)))
    assert gap["missing"] == list(range(5, 13))
    assert g.day_gap_issue(gap).startswith("Itinerary missing 8 day(s) vs source (12 source days -> 4 output days)")
    assert g.itinerary_day_gap(JAPAN_TOUR, _page(range(1, 13)))["missing"] == []


# ── 5. the job row says why ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_s1_rewrite_job_result_carries_the_hold_reason():
    from services.jobs import s1_rewrite_job as sj
    from shared.jobs import queue
    from shared.jobs.registry import JobContext

    job = queue.Job(id="job-1", kind="s1_rewrite", payload={"tour_id": "t", "batch_id": "b", "tenant_id": "x"},
                    status="running", attempt=1, max_attempts=1, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {"redis": None})
    ctx.progress = AsyncMock()
    run_result = {"version_id": "v", "status": "success", "quality_score": 10.0, "model_used": "m",
                  "brand_audit_status": "fixed", "failure_codes": [CODE], "hard_block_codes": [CODE],
                  "publishable": False}
    with patch("api.routers.admin_pipeline._build_s1_progress_sink", return_value=None), \
         patch("api.routers.admin_pipeline._run_tour_safe", AsyncMock(return_value=run_result)):
        out = await sj.run(ctx)
    assert out["publishable"] is False and out["hard_block_codes"] == [CODE] and out["failure_codes"] == [CODE]
