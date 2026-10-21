"""AA-925 (Q-2) — scripts/aa925/day_oracle_report.py without a database: the per-set evaluation, the leak
count against validate's own ITINERARY_DAY_COUNT_MISMATCH, the archive loader and the Wilson bound."""
import os

from scripts.aa925 import day_oracle_report as rep

SRC4 = "Day 1: Paro\nDay 2: Taktsang\nDay 3: Punakha\nDay 4: depart"


def _row(tour_id, itin, duration="4 days", src=SRC4, meta="A complete sentence that ends on its noun guides.",
         status="approved", country="Bhutan"):
    return {"gc_id": f"gc-{tour_id}", "tour_id": tour_id, "status": status, "country": country, "duration": duration,
            "src_itineraries": src, "aa_itineraries": itin, "seo_meta": meta}


def _days(*nums):
    return "\n\n".join(f"Day {n} — Title {n}\nBody." for n in nums)


def test_evaluate_counts_codes_leaks_and_samples():
    det = rep.load_detectors(None)
    rows = [
        _row("t1", _days(1, 2, 3, 4)),                                  # clean
        _row("t2", _days(1, 2, 2, 3, 4)),                               # duplicate, no source day missing → leak
        _row("t3", _days(1, 2, 4)),                                     # gap: ITINERARY_DAY_COUNT_MISMATCH fires
        _row("t4", _days(1, 2), duration="3 hours", src="A cable car ride."),
        _row("t5", _days(1, 2, 3, 4), meta="Four days in Bhutan along a 20-kilometre."),
    ]
    out = rep.evaluate(rows, "A_published", det)
    assert out["rows"] == 5
    assert out["counts"]["DAY_OUTPUT_DUPLICATE"] == 1
    assert out["leak_vs_itinerary_day_count_mismatch"]["DAY_OUTPUT_DUPLICATE"] == 1
    assert "DAY_OUTPUT_GAP" not in out["leak_vs_itinerary_day_count_mismatch"]
    assert out["counts"]["DAY_SUBDAY_SPLIT"] == 1
    assert out["counts"][rep.CUT_CODE] == 1
    sample = out["samples"]["DAY_OUTPUT_DUPLICATE"][0]
    assert sample["tour_id"] == "t2" and sample["output_days"][2] == [2, "Title 2"]
    assert "Body" not in str(sample)  # no body text in a sample


def test_samples_are_capped_per_code():
    det = rep.load_detectors(None)
    rows = [_row(f"t{i}", _days(1, 1, 2, 3, 4)) for i in range(rep.SAMPLES_PER_CODE + 10)]
    out = rep.evaluate(rows, "A", det)
    assert len(out["samples"]["DAY_OUTPUT_DUPLICATE"]) == rep.SAMPLES_PER_CODE
    assert out["hits_total"]["DAY_OUTPUT_DUPLICATE"] == rep.SAMPLES_PER_CODE + 10


def test_archive_loader_reads_the_given_tree():
    """The loader re-registers modules under their package names; restore them so later tests keep the
    modules graph.py already imported."""
    import sys

    names = ("services.content_generation.day_parse", "services.content_generation.prompts",
             "services.content_generation.seo_meta_utils", "services.quality", "services.quality.day_oracle")
    saved = {n: sys.modules.get(n) for n in names}
    try:
        oracle, day_parse, prompts, seo, _iu = rep.load_detectors(os.getcwd())
        assert oracle.__file__.endswith(os.path.join("services", "quality", "day_oracle.py"))
        assert oracle is not saved["services.quality.day_oracle"]
        assert callable(seo.meta_cut_signature) and callable(day_parse.parse_duration)
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


def test_wilson_lower_bound():
    assert rep.wilson_lower(0, 0) is None
    assert rep.wilson_lower(21, 30) == 0.5212
    assert rep.wilson_lower(30, 30) > 0.88


def test_sql_reads_schema_qualified_tables_and_no_bucket_literal():
    for sql in (rep.SET_A_SQL, rep.SET_B_SQL):
        assert "gold_aa_internal.published_tours" in sql and "silver_aa_internal.raw_tours" in sql
        assert "rt.deleted_at IS NULL" in sql
    assert "pt.deleted_at IS NULL" in rep.SET_A_SQL
    src = open(rep.__file__, encoding="utf-8").read()
    assert "aa-cis-bronze-" not in src
