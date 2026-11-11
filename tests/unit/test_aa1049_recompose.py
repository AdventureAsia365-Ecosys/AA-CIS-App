"""AA-1049 path A-new — the fixed-field recompose of published S1 Masters (services/content_generation/s1_recompose.py,
s1_recompose_store.py, scripts/quality/recompose_masters.py), no DB.

1. the recompose: the source day's verbatim fact lines replace the writer's, the narrative (name, summary,
   highlights, day titles, prose) is byte-identical, a re-run on the result is a no-op;
2. the narrative guard: text before the first heading, a stored day the plan lacks, a changed prose line;
3. the seo fields: the title fit at a word boundary and the code meta template, only when the stored ones fail;
4. the decision: every skip reason, resume, contract not worse;
5. the report: counts, 8-char ids, the pin, never page text;
6. the script's set rules (APPLY needs an open wave and a pin).

Invented tours and places only. No network, no model: every model path raises (s1_recompose.no_model).
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.content_generation import s1_recompose as rc
from services.content_generation import s1_recompose_store as st
from services.content_generation import s1_writer_v2 as v2
from services.quality.s1_facts import build_fact_table

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality" / "recompose_masters.py"

SOURCE_ITIN = ("Day 1: Arrive in Velmora and walk along the canal to the guesthouse.\nOvernight: Velmora\nMeals: D\n"
               "Day 2: Walk up Corran Ridge to the chapel and back to the station.\nMeals: B")
DAY1_PROSE = "Arrive in Velmora and walk along the canal to the guesthouse."
DAY2_PROSE = "Walk up Corran Ridge to the chapel, then return to the station."
PAGE_ITIN = (f"Day 1 — Arrive in Velmora\n{DAY1_PROSE}\nOvernight: Velmora. Meals: dinner.\n\n"
             f"Day 2 — Corran Ridge walk\n{DAY2_PROSE}\nMeals: breakfast.")
META_OK = ("A two-day walking break in Velmora, Testland, with a canal walk to the guesthouse and a climb up "
           "Corran Ridge to the chapel before the station.")


def row(**over) -> dict:
    r = {"tour_id": "0a1b2c3d-0000-4000-8000-000000000001", "generated_content_id": "9f8e7d6c-0000-4000-8000-00000000000a",
         "published_tour_id": "p-velmora", "version_num": 3, "status": "approved",
         "metadata": json.dumps({"writer_version": "v2", "grounding": {"violations": []},
                                 "final_contract": {"verdict": "publish"}, "s1_job_id": "job-x"}),
         "fix_pass_applied": False, "human_edited": False,
         "aa_name": "Velmora Canal and Corran Ridge Walk", "aa_subtitle": "Two easy walking days from Velmora.",
         "aa_summary": "A two-day walking break in Velmora with a climb up Corran Ridge to the chapel.",
         "aa_description": "", "aa_highlights": json.dumps(["Canal walk in Velmora", "Corran Ridge chapel",
                                                            "Guesthouse night by the canal"]),
         "aa_itineraries": PAGE_ITIN, "seo_title": "Velmora Canal and Corran Ridge Walk, 2 Days",
         "seo_meta": META_OK, "score_overall": 8.5, "failure_codes": "[]", "brand_audit_status": "pass",
         "brand_audit_codes": "[]", "country": "Testland", "duration": "2 days",
         "src_name": "VELMORA CANAL AND RIDGE 2D", "src_subtitle": None,
         "src_summary": "Two days in Velmora with a ridge walk.", "src_description": "A small-group walking break.",
         "src_highlights": json.dumps(["Corran Ridge chapel"]), "src_itineraries": SOURCE_ITIN,
         "inclusions": "Guesthouse night\nMeals as listed", "activities": None, "price_raw": None,
         "exclusions": "Flights", "group_size": "2-8", "best_time_to_go": None,
         "source_live": True, "pending_review": False, "newer_id": None, "newer_status": None,
         "newer_recompose_of": None, "newer_was_published": False, "job_active": False}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _no_jev_network():
    from services.content_generation import grounding
    with patch.object(grounding, "_judge_all", side_effect=AssertionError("Jev reached")):
        yield


def _recompose(r: dict, words=("velvety",)) -> rc.Recomposed:
    tour = st.tour_of(r)
    with rc.no_model():
        return rc.recompose(st.page_of(r), tour, build_fact_table(tour), list(words))


def _plan(r: dict) -> dict:
    tour = st.tour_of(r)
    return v2.day_plan(tour, build_fact_table(tour))


# ── 1. the recompose ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_source_fact_lines_replace_the_writer_ones_and_the_narrative_is_unchanged():
    r = row()
    res = _recompose(r)
    itin = res.page["itineraries"]
    assert itin == (f"Day 1 — Arrive in Velmora\n{DAY1_PROSE}\nOvernight: Velmora\nMeals: D\n\n"
                    f"Day 2 — Corran Ridge walk\n{DAY2_PROSE}\nMeals: B")
    assert res.changed == ["fact_lines"] and res.fields["fact_lines"] == 2
    assert res.narrative_ok and res.heading_ok
    page = st.page_of(r)
    for k in rc.NARRATIVE_FIELDS + ("seo_title", "seo_meta"):
        assert res.page[k] == page[k], k
    assert DAY1_PROSE in itin.splitlines() and DAY2_PROSE in itin.splitlines()   # prose lines byte for byte


def test_a_re_run_on_the_recomposed_page_is_a_no_op():
    first = _recompose(row())
    again = _recompose(row(aa_itineraries=first.page["itineraries"]))
    assert again.no_op and again.page["itineraries"] == first.page["itineraries"]


def test_a_source_fact_line_that_is_not_a_bare_label_line_is_not_duplicated_on_a_re_run():
    src = SOURCE_ITIN.replace("Overnight: Velmora", "Accommodation: Canal guesthouse. Breakfast included at 8.")
    first = _recompose(row(src_itineraries=src))
    assert first.page["itineraries"].count("Accommodation: Canal guesthouse.") == 1
    again = _recompose(row(src_itineraries=src, aa_itineraries=first.page["itineraries"]))
    assert again.no_op


def test_a_fact_clause_at_the_end_of_a_prose_line_moves_to_the_fact_lines_and_is_counted():
    itin = PAGE_ITIN.replace(f"{DAY1_PROSE}\nOvernight: Velmora. Meals: dinner.",
                             f"{DAY1_PROSE} Overnight: Velmora. Meals: dinner.")
    res = _recompose(row(aa_itineraries=itin))
    assert res.narrative_ok and res.report["fact_clauses_cut"] == 1
    assert f"{DAY1_PROSE}\nOvernight: Velmora\nMeals: D" in res.page["itineraries"]


def test_a_heading_number_is_set_by_the_plan_and_counted():
    itin = PAGE_ITIN.replace("Day 2 — Corran", "Day 2-3 — Corran")   # a range the two-day source lacks
    res = _recompose(row(aa_itineraries=itin))
    assert "Day 2 — Corran Ridge walk" in res.page["itineraries"]
    assert res.fields["day_headings"] == 1 and res.fields["day_count"] == 1 and res.narrative_ok


# ── 2. the narrative guard ──────────────────────────────────────────────────────────────────────────────────────


def test_text_before_the_first_heading_is_never_dropped_silently():
    res = _recompose(row(aa_itineraries="A short note on the walk.\n\n" + PAGE_ITIN))
    assert not res.narrative_ok


def test_a_stored_day_the_plan_lacks_is_never_dropped_silently():
    res = _recompose(row(aa_itineraries=PAGE_ITIN + "\n\nDay 3 — Spare day\nA spare day in the old town."))
    assert not res.narrative_ok and res.report["extra"] == [3]


def test_narrative_of_reads_titles_and_prose_only():
    plan = _plan(row())
    a = rc.narrative_of(st.page_of(row()), plan)
    b = rc.narrative_of({**st.page_of(row()), "itineraries": PAGE_ITIN.replace("dinner", "lunch")}, plan)
    c = rc.narrative_of({**st.page_of(row()), "itineraries": PAGE_ITIN.replace("chapel,", "chapel and")}, plan)
    assert a == b and a != c


# ── 3. the seo fields ───────────────────────────────────────────────────────────────────────────────────────────


def test_a_long_title_is_fitted_and_a_failing_meta_gets_the_code_template():
    long_title = "Velmora Canal and Corran Ridge Walk with a Night in the Canal Guesthouse, Two Days"
    highlights = json.dumps(["Canal walk in Velmora", "Corran Ridge chapel", "Guesthouse night by the canal",
                             "Pine-wood trail above the old market town"])
    res = _recompose(row(seo_title=long_title, seo_meta="Too short.", aa_highlights=highlights))
    from services.content_generation.seo_meta_utils import SEO_META_MAX, SEO_META_MIN, SEO_TITLE_MAX
    assert {"seo_title", "seo_meta"} <= set(res.changed)
    assert len(res.page["seo_title"]) <= SEO_TITLE_MAX and long_title.startswith(res.page["seo_title"].rstrip(" ,"))
    assert SEO_META_MIN <= len(res.page["seo_meta"]) <= SEO_META_MAX
    assert res.page["seo_meta"].startswith("Velmora Canal and Corran Ridge Walk")


def test_a_meta_in_band_is_kept():
    res = _recompose(row())
    assert res.page["seo_meta"] == META_OK and "seo_meta" not in res.changed


# ── 4. the decision ─────────────────────────────────────────────────────────────────────────────────────────────


def test_a_clean_master_is_due_with_a_verdict_that_is_not_worse():
    d = st.decide(row(), ["velvety"])
    assert d["state"] == st.RECOMPOSE, d
    assert d["verdict"]["old"] == d["verdict"]["new"] == "publish"
    assert d["page"]["itineraries"] != PAGE_ITIN and d["changed"] == ["fact_lines"]


@pytest.mark.parametrize("over, state", [
    ({"source_live": False}, st.SOURCE_NOT_LIVE),
    ({"pending_review": True}, st.PENDING_REVIEW),
    ({"newer_id": "n1", "newer_status": "draft"}, st.NEWER_VERSION),
    ({"newer_id": "n1", "newer_status": "approved"}, st.NEWER_VERSION),
    ({"job_active": True}, st.JOB_ACTIVE),
    ({"metadata": json.dumps({"grounding": {"violations": []}})}, rc.NOT_V2),
    ({"human_edited": True}, rc.HUMAN_EDITED),
    ({"aa_itineraries": "A short note.\n\n" + PAGE_ITIN}, rc.NARRATIVE_CHANGED),
    ({"aa_itineraries": "Day one: arrive.\nDay two: walk."}, rc.HEADING_PARSE),
])
def test_every_skip_reason(over, state):
    assert st.decide(row(**over), [])["state"] == state


def test_a_recomposed_master_is_a_no_op():
    first = st.decide(row(), [])
    assert st.decide(row(aa_itineraries=first["page"]["itineraries"]), [])["state"] == rc.NO_OP


def test_a_newer_hitl_version_does_not_block_and_a_written_recompose_is_resumed():
    assert st.decide(row(newer_id="n1", newer_status="hitl"), [])["state"] == st.RECOMPOSE
    assert st.decide(row(newer_id="n1", newer_status="rejected"), [])["state"] == st.RECOMPOSE
    d = st.decide(row(newer_id="n2", newer_status="approved",
                      newer_recompose_of="9f8e7d6c-0000-4000-8000-00000000000a"), [])
    assert d["state"] == st.RESUME and d["new_version_id"] == "n2"


def test_the_candidates_sql_renders_and_reads_the_publish_trace():
    sql = st.candidates_sql()
    assert '@> \'[{"exit": "recompose", "actual": "published"}]\'::jsonb' in sql
    assert "newer_was_published" in sql


def test_a_published_recompose_that_a_person_rolled_back_is_never_resumed():
    d = st.decide(row(newer_id="n2", newer_status="approved", newer_was_published=True,
                      newer_recompose_of="9f8e7d6c-0000-4000-8000-00000000000a"), [])
    assert d["state"] == st.ROLLED_BACK and "new_version_id" not in d
    assert st.pin_of([d]).startswith("0:")


def test_a_human_edited_master_is_taken_when_asked():
    # its grounding predates the edit: review today and stored, and the recompose does not make it worse
    md = json.dumps({"writer_version": "v2", "grounding": {"violations": []}, "final_contract": {"verdict": "review"}})
    d = st.decide(row(human_edited=True, metadata=md), [], include_human_edited=True)
    assert d["state"] == st.RECOMPOSE and d["verdict"]["old"] == d["verdict"]["new"] == "review"
    # stored publish, review today: a review after the recompose is worse than the stored verdict, so it waits
    assert st.decide(row(human_edited=True), [], include_human_edited=True)["state"] == rc.CONTRACT_WORSE


def test_a_worse_contract_is_skipped():
    worse = {"old": "publish", "new": "review", "stored": "publish", "worse": True, "policy": False,
             "old_blocks": [], "new_blocks": ["X"], "new_failure_codes": []}
    with patch.object(rc, "contract_pair", return_value=worse):
        d = st.decide(row(), [])
    assert d["state"] == rc.CONTRACT_WORSE and d["verdict"]["new"] == "review"


def test_an_elephant_riding_master_is_left_for_its_policy_review():
    d = st.decide(row(aa_summary="A two-day break in Velmora with an elephant ride through the forest."), [])
    assert d["state"] == rc.POLICY


def test_contract_worse_rules():
    r = row()
    page = st.page_of(r)
    new = _recompose(r).page
    tour = st.tour_of(r)
    with rc.no_model():
        con = rc.contract_pair(r, page, new, tour, [])
    assert con["worse"] is False and con["old"] == con["new"] == "publish"
    # a recompose that adds a blocking code (a forbidden word in the composed meta) is worse
    with rc.no_model():
        bad = rc.contract_pair(r, page, {**new, "seo_meta": META_OK.replace("walking", "velvety")}, tour, ["velvety"])
    assert bad["worse"] is True and "FORBIDDEN_WORD" in bad["new_blocks"]


def test_a_model_path_raises_inside_the_guard():
    from shared.llm_client.client import LLMClient
    with rc.no_model() as nm:
        with pytest.raises(RuntimeError):
            LLMClient()
    assert nm.attempts == 1


# ── 5. the report ───────────────────────────────────────────────────────────────────────────────────────────────


def test_the_report_has_counts_short_ids_and_a_pin_never_text():
    rows = [row(), row(tour_id="0a1b2c3d-0000-4000-8000-000000000002", pending_review=True)]
    ds = [st.decide(r, []) for r in rows]
    lines = st.report_lines(ds)
    text = "\n".join(lines)
    assert "recompose=1" in lines[0] and "pending_review=1" in lines[0]
    assert "fact_lines=1" in lines[1]
    assert "0a1b2c3d recompose" in text and "0000-4000" not in text
    assert DAY1_PROSE not in text and "Velmora" not in text
    pin = lines[-1].removeprefix("PIN=")
    assert pin == st.pin_of(ds) and pin.startswith("1:")
    assert st.pin_of(list(reversed(ds))) == pin


def test_new_metadata_keeps_the_writer_keys_and_stamps_the_recompose():
    d = st.decide(row(), [])
    md = st.new_metadata(row()["metadata"], d, "2026-11-11T00:00:00+00:00")
    assert md["writer_version"] == "v2" and md["grounding"] == {"violations": []}
    assert "final_contract" not in md and "s1_job_id" not in md
    assert md["recompose_of"] == d["version_id"] and md["writer"] == rc.WRITER == "recompose-v1"
    assert md["llm_cost_usd"] == 0.0 and md["recompose"]["fields"] == ["fact_lines"]


# ── 6. the script's set rules ───────────────────────────────────────────────────────────────────────────────────


def _script():
    spec = importlib.util.spec_from_file_location("recompose_masters", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = "0a1b2c3d-0000-4000-8000-000000000001"
B = "0a1b2c3d-0000-4000-8000-000000000002"


@pytest.mark.parametrize("env, wave, want_ids, refused", [
    ({}, None, None, "name the set"),
    ({"ALL": "1"}, None, None, ""),
    ({"TOUR_IDS": f"{B},{A}"}, None, [A, B], ""),
    ({}, [A, B], [A, B], ""),
    ({"TOUR_IDS": A}, [A, B], [A], ""),
    ({"TOUR_IDS": "0a1b2c3d-0000-4000-8000-000000000009"}, [A, B], None, "outside the wave"),
    ({"APPLY": "1", "PIN": "1:x", "TOUR_IDS": A}, None, None, "needs WAVE_ID"),
    ({"APPLY": "1"}, [A], None, "needs PIN"),
    ({"APPLY": "1", "PIN": "1:x", "ALL": "1"}, [A], None, "DRY RUN option"),
    ({"APPLY": "1", "PIN": "1:x"}, [A], [A], ""),
])
def test_resolve_set(env, wave, want_ids, refused):
    ids, why = _script().resolve_set(env, wave)
    assert (refused in why) if refused else why == ""
    if not refused:
        assert ids == want_ids


def test_a_short_id_is_refused():
    with pytest.raises(ValueError):
        _script().parse_ids("0a1b2c3d")
