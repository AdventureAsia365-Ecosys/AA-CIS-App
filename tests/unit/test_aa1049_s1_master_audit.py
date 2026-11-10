"""AA-1049 — the read-only S1 Master audit (scripts/quality/audit_s1_masters.py + its local decode helper).

Invented tours only (no DB, audit or prompt text). No network, no model: the audit's own guards answer every model
path, and the tests check that nothing reaches Jev or a model client.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

QUALITY = Path(__file__).resolve().parents[2] / "scripts" / "quality"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, QUALITY / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load("audit_s1_masters")
local = _load("audit_s1_masters_local")

SOURCE_ITIN = ("Day 1: Arrive in Velmora and walk along the canal to the guesthouse.\nOvernight: Velmora\nMeals: D\n"
               "Day 2: Walk up Corran Ridge to the chapel and back to the station.\nMeals: B")
PAGE_ITIN = ("Day 1 — Arrive in Velmora\nArrive in Velmora and walk along the canal to the guesthouse.\n"
             "Overnight: Velmora. Meals: dinner.\n\n"
             "Day 2 — Corran Ridge walk\nWalk up Corran Ridge to the chapel, then return to the station.\n"
             "Meals: breakfast.")
META_OK = ("A two-day walking break in Velmora, Testland, with a canal walk to the guesthouse and a climb up "
           "Corran Ridge to the chapel before the station.")


def row(**over) -> dict:
    r = {"tour_id": "t-velmora", "generated_content_id": "v-velmora", "published_tour_id": "p-velmora",
         "version_num": 3, "status": "published", "model_editorial": "invented-writer-model",
         "metadata": json.dumps({"writer_version": "v2", "grounding": {"violations": []},
                                 "final_contract": {"verdict": "publish"}}),
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
         "exclusions": "Flights", "group_size": "2-8", "best_time_to_go": None, "source_missing": False,
         "source_deleted": False, "source_status": "active", "lifecycle_stage": "active"}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _no_jev_network():
    """The real Jev pool is never reached: the audit patches judge_units above it."""
    from services.content_generation import grounding
    with patch.object(grounding, "_judge_all", side_effect=AssertionError("Jev reached")):
        yield


def _texts(r: dict) -> list[str]:
    return [s for s in (PAGE_ITIN.splitlines() + [r["aa_summary"], r["seo_meta"], r["aa_subtitle"]]) if len(s) > 12]


def test_a_clean_v2_master_publishes_and_reports_only_codes_and_counts():
    r = row()
    ln = audit.audit_one(r, ["velvety"])
    assert ln["errors"] == {}
    assert ln["writer"] == "v2" and ln["writer_group"] == "v2"
    assert ln["contract"]["verdict"] == "publish" and ln["contract"]["verdict_changed"] is False
    assert set(audit.ALWAYS_NOT_RUN) <= set(ln["not_run"])
    assert ln["rules"]["validate"]["codes"] == [] and ln["rules"]["pre_audit"]["codes"] == []
    comp = ln["compose"]
    assert comp["applies"] is True and comp["slug"] == "not_applicable"
    assert comp["days"] == {"stored": 2, "composed": 2, "planned": 2}
    # the writer's own fact lines are replaced by the source day's verbatim lines: counted, never shown
    assert "fact_lines" in comp["changed"] and comp["fields"]["day_headings"] == 0
    assert comp["fields"]["seo_title"] == 0 and comp["fields"]["seo_meta"] == 0
    dumped = json.dumps({k: ln[k] for k in ("compose", "rules", "contract")})
    for text in _texts(r):
        assert text not in dumped


def test_a_brand_word_blocks_the_contract_and_is_found_per_field():
    r = row(aa_summary="A velvety two-day walking break in Velmora with a climb up Corran Ridge.")
    ln = audit.audit_one(r, ["velvety"])
    block = {f["code"] for f in ln["contract"]["findings"] if f["severity"] == "block"}
    assert "FORBIDDEN_WORD" in block and ln["contract"]["verdict"] == "review"
    assert ln["contract"]["verdict_changed"] is True
    assert {"field": "summary", "source": "brand", "words": ["velvety"]} in ln["brand_words"]["hits"]


def test_without_the_brand_list_a_brand_only_word_passes_so_the_audit_passes_the_list():
    r = row(aa_summary="A velvety two-day walking break in Velmora with a climb up Corran Ridge.")
    assert audit.audit_one(r, [])["contract"]["verdict"] == "publish"


def test_a_meal_claim_the_source_lacks_needs_jev_and_is_reported_not_run():
    r = row(aa_itineraries=PAGE_ITIN.replace("then return to the station.",
                                             "then a picnic lunch is served by the chapel."))
    ln = audit.audit_one(r, [])
    assert "jev_meal_claims" in ln["not_run"]
    assert ln["rules"]["pre_audit"]["needs_model"] == ["ITINERARY_MEAL_TIME_INVENTED"]
    assert "ITINERARY_MEAL_TIME_INVENTED" not in ln["rules"]["pre_audit"]["codes"]


def test_a_dropped_day_and_a_long_title_show_in_the_compose_diff():
    itin = PAGE_ITIN.split("\n\n")[0]
    title = "Velmora Canal and Corran Ridge Walk with a Chapel Visit and Station Return"
    ln = audit.audit_one(row(aa_itineraries=itin, seo_title=title), [])
    comp = ln["compose"]
    assert comp["days"]["stored"] == 1 and comp["days"]["planned"] == 2
    assert comp["report"]["missing"] == [2]
    assert "seo_title" in comp["changed"]
    block = {f["code"] for f in ln["contract"]["findings"] if f["severity"] == "block"}
    assert "ITINERARY_DAY_COUNT_MISMATCH" in block and ln["contract"]["verdict"] == "review"


def test_an_older_writer_is_labelled_by_its_model_and_the_train_does_not_apply():
    ln = audit.audit_one(row(metadata=json.dumps({"grounding": {"violations": []}})), [])
    assert ln["writer"] == "invented-writer-model" and ln["writer_group"] == "older"
    assert ln["compose"]["applies"] is False


def test_a_failing_section_is_an_error_entry_and_the_rest_still_runs():
    with patch.object(audit, "compose_of", side_effect=ValueError("boom")):
        ln = audit.audit_one(row(), [])
    assert ln["errors"] == {"compose": "ValueError: boom"} and "contract" in ln and "rules" in ln


def test_a_model_attempt_is_refused_and_reported():
    from shared.llm_client.client import LLMClient

    def calls_a_model(*_a, **_k):
        try:
            LLMClient().generate(None)
        except RuntimeError:
            pass
        return {"fields": {}, "changed": []}

    with patch.object(audit, "compose_of", side_effect=calls_a_model):
        ln = audit.audit_one(row(), [])
    assert ln["errors"]["model_call_refused"] == "1 attempt(s)"


def test_the_summary_splits_by_writer_and_lists_the_non_publish_ids():
    lines = [audit.audit_one(row(), []),
             audit.audit_one(row(tour_id="t-older", metadata=json.dumps({"grounding": {"violations": []}}),
                                 aa_itineraries=PAGE_ITIN.split("\n\n")[0]), [])]
    s = audit.summarize(lines)
    assert s["masters"] == {"all": 2, "v2": 1, "older": 1}
    assert s["verdict"]["v2"] == {"publish": 1} and s["verdict"]["older"] == {"review": 1}
    assert s["block_codes"]["older"]["ITINERARY_DAY_COUNT_MISMATCH"] == 1
    assert s["compose_fields"]["v2"]["fact_lines"] == 1
    assert s["not_run"]["all"]["s1_judge"] == 2
    assert [r["tour_id"] for r in s["not_publish"]] == ["t-older"]
    assert s["not_publish"][0]["writer_group"] == "older"


def test_population_counts_states_and_lists_the_unmastered_live_sources():
    base = {"source_deleted": False, "source_status": "active", "lifecycle_stage": "active",
            "pipeline_status": "published", "master_status": "active", "master_status_reason": None,
            "master_deleted": False, "latest_version_status": "published", "latest_version_writer": "v2"}
    rows = [{**base, "n": 5},
            {**base, "pipeline_status": "hitl_required", "master_status": "none", "latest_version_status": "hitl",
             "latest_version_writer": "v2", "n": 2},
            {**base, "source_status": "superseded", "master_status": "inactive",
             "master_status_reason": "superseded", "n": 3},
            {**base, "source_deleted": True, "master_status": "trashed", "master_status_reason": "source_deleted",
             "n": 1}]
    unm = [{"tour_id": "t-a", "pipeline_status": "hitl_required", "lifecycle_stage": "active",
            "master_status": "none", "master_status_reason": None, "latest_version_status": "hitl"}]
    pop = audit.population_of(rows, unm, {"master_tenant": 5, "other_tenants": 0})
    assert pop["raw_tours"] == 11
    assert pop["source"] == {"active/active": 7, "superseded/active": 3, "deleted": 1}
    assert pop["master"]["inactive/superseded"] == 3 and pop["master"]["none"] == 2
    assert pop["pipeline_unmastered"] == {"hitl_required": 2}
    assert pop["writer_of_active_masters_latest"] == {"v2": 5}
    assert pop["unmastered_live_sources"][0]["tour_id"] == "t-a"


def test_the_blocks_decode_locally_and_the_readme_holds_ids_and_codes_only(tmp_path):
    r = row(aa_summary="A velvety two-day walking break in Velmora with a climb up Corran Ridge.")
    lines = [audit.audit_one(r, ["velvety"])]
    summary = {"schema": audit.SCHEMA, "contract_version": "x", "limit": 0, "generated_at": "2026-11-11T00:00:00",
               "brand_rule": {"active": True, "forbidden_words": 1}, **audit.summarize(lines),
               "population": audit.population_of([], [], {"master_tenant": 1, "other_tenants": 0})}
    out = tmp_path / "ecsx.out"
    out.write_text("noise before\n" + audit.render(lines, summary) + "\nnoise after\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    assert local.main([str(out), "--work", str(tmp_path / "work"), "--readme", str(readme)]) == 0
    decoded = (tmp_path / "work" / audit.LINES_BLOCK).read_text(encoding="utf-8").splitlines()
    assert json.loads(decoded[0])["tour_id"] == "t-velmora"
    md = readme.read_text(encoding="utf-8")
    assert "`t-velmor`" in md and "t-velmora" not in md and "FORBIDDEN_WORD" in md and "review" in md
    for text in _texts(r) + ["velvety", "Velmora Canal and Corran Ridge Walk"]:
        assert text not in md


def test_the_local_helper_refuses_a_missing_block(tmp_path):
    out = tmp_path / "ecsx.out"
    out.write_text("AUDIT_SUMMARY {}\n", encoding="utf-8")
    assert local.main([str(out), "--work", str(tmp_path / "w")]) == 2


def test_the_master_query_reads_the_contract_row_of_the_published_active_masters():
    sql = audit.masters_sql()
    assert "pt.generated_content_id = gc.id" in sql and "pt.master_status = 'active'" in sql
    assert "pt.deleted_at IS NULL" in sql and "pt.tenant_id = $1::uuid" in sql
    assert "qs.failure_codes" in sql and sql.rstrip().endswith("ORDER BY gc.tour_id")


def test_the_container_script_holds_no_decode_call():
    text = (QUALITY / "audit_s1_masters.py").read_text(encoding="utf-8")
    assert "b64decode" not in text and "exec(" not in text


def test_a_stored_null_subtitle_is_a_missing_field_not_a_crash():
    ln = audit.audit_one(row(aa_subtitle=None), [])
    assert ln["errors"] == {}
    assert "MISSING_FIELD" in ln["rules"]["validate"]["codes"]


def test_one_rule_set_failing_leaves_the_others_reporting():
    from services.quality import s1_checks
    with patch.object(s1_checks, "run_checks", side_effect=KeyError("x")):
        ln = audit.audit_one(row(), [])
    assert list(ln["errors"]) == ["rules.s1_checks"]
    assert "validate" in ln["rules"] and "pre_audit" in ln["rules"] and "grounding_numeric" in ln["rules"]
