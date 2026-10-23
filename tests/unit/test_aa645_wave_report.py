"""AA-645 step 0 (d13) — scripts/aa645/wave_report.py: the per-wave before/after quality read, per country, $0."""
import datetime as dt
import json

from scripts.aa645 import wave_report as wr

TOUR = {"src_name": "Hanoi and Halong Bay", "src_summary": "Two days.", "src_highlights": '["Old Quarter"]',
        "src_itineraries": "Day 1: Hanoi. Walk the Old Quarter.\nDay 2: Halong Bay. Kayak 3 km.",
        "country": "Vietnam", "duration": "2 days", "inclusions": "Meals as per itinerary", "exclusions": "",
        "group_size": "2-12", "best_time_to_go": None}


def _version(vid, *, gate=(), unsupported=0, brand_fit=7.0, cost=0.08, writer="v1", days=(1, 2), codes=(),
             audit="pass", report_only=()):
    return {"version_id": vid, "status": "approved", "review_status": None, "writer_version": writer,
            "brand_audit_status": audit, "brand_audit_report_only": list(report_only),
            "gate_codes": list(gate), "report_codes": [], "figure_coverage": 0.5, "failure_codes": [],
            "brand_audit_codes": list(codes), "grounding_found": unsupported, "grounding_unsupported": unsupported,
            "brand_fit": brand_fit, "score": 8.0, "cost_usd": cost, "days": list(days), "canonical_days": bool(days)}


def _tour(country, outcome, before, after, atoms=None):
    return {"tour_id": f"{country}-{outcome}-{id(after)}", "country": country, "outcome": outcome,
            "before": before, "after": after, "atoms": atoms}


def test_outcome_from_the_live_pointer():
    assert wr.outcome({"live_id": "a", "after_id": "a", "before_id": "b"}) == "published"
    assert wr.outcome({"live_id": "b", "after_id": "a", "before_id": "b"}) == "stayed_v1"
    assert wr.outcome({"live_id": None, "after_id": "a", "before_id": None}) == "not_published"
    assert wr.outcome({"live_id": "c", "after_id": "a", "before_id": "b"}) == "other_live_version"


def test_version_facts_runs_the_code_checks_on_the_stored_page():
    tour = wr.source_tour(TOUR)
    from services.quality.s1_facts import build_fact_table
    facts = build_fact_table(tour)
    stored = {"id": "v1", "status": "approved", "metadata": '{"writer_version": "v2", "judge": {"brand_fit": 6}, '
                                                             '"grounding": {"found": 2, "violations": ["x"]}}',
              "aa_name": "Hanoi and Halong Bay", "aa_subtitle": "Old Quarter, then the bay", "aa_summary": "Two days.",
              "aa_description": "", "aa_highlights": '["Old Quarter"]',
              "aa_itineraries": "Day 1 — Hanoi\nWalk.\n\nDay 2 — Halong Bay\nKayak 3 km.\n\nDay 3 — Extra\nMore.",
              "seo_title": "Hanoi", "seo_meta": "Short.", "failure_codes": '["META_TOO_SHORT"]',
              "brand_audit_codes": None, "score_overall": 7.5, "review_status": None}
    v = wr.version_facts(stored, tour, facts)
    assert v["writer_version"] == "v2" and v["brand_fit"] == 6 and v["grounding_unsupported"] == 1
    assert v["days"] == [1, 2, 3] and v["canonical_days"] and v["failure_codes"] == ["META_TOO_SHORT"]
    assert "DAY_*" in v["gate_codes"]                 # a day the source does not have = a gate code
    assert "Group size: 2-12" in tour["description"]  # agency fields are source facts, as in the A/B


def test_aggregate_per_country_and_the_clear_improvement_rule():
    vn = [_tour("Vietnam", "published", _version("b1", gate=["DAY_*"], unsupported=2),
                _version("a1", writer="v2", brand_fit=6.5), atoms={"fingerprint_days": 2, "atoms_off_days": 0}),
          _tour("Vietnam", "published", _version("b2", gate=["META_CUT"]), _version("a2", writer="v2"),
                atoms={"fingerprint_days": 2, "atoms_off_days": 0})]
    la = [_tour("Laos", "stayed_v1", _version("b3"), _version("a3", gate=["NUMBER_NOT_IN_SOURCE"], writer="v2")),
          _tour("Laos", "published", _version("b4"), _version("a4", writer="v2"), atoms=None)]
    out = wr.aggregate(vn + la)
    v = out["Vietnam"]
    assert v["gate_page_rate_before"] == 1.0 and v["gate_page_rate_after"] == 0.0 and v["clearly_better"]
    assert v["grounding_unsupported_before"] == 2 and v["grounding_unsupported_after"] == 0
    assert v["brand_fit_after_report_only"] == 6.75
    lao = out["Laos"]
    assert lao["stayed_v1_rate"] == 0.5 and not lao["clearly_better"]
    assert lao["published_not_atomized"] == 1
    assert out["ALL"]["tours"] == 4 and list(out)[-1] == "ALL"


def test_stop_conditions():
    calm = {"stayed_v1_rate": 0.05, "legacy_itinerary_pages": 0, "atoms_off_days": 0, "cost_per_tour_after": 0.08,
            "published_not_atomized": 0, "clearly_better": True}
    assert not any(wr.stop_conditions(calm).values())
    bad = dict(calm, stayed_v1_rate=0.2, atoms_off_days=3, cost_per_tour_after=0.2, published_not_atomized=1,
               legacy_itinerary_pages=1, clearly_better=False)
    assert all(wr.stop_conditions(bad).values())


def test_markdown_and_args():
    args = wr.parse_args(["--since", "2026-01-24T02:00:00Z"])
    assert args.since == dt.datetime(2026, 1, 24, 2, tzinfo=dt.timezone.utc) and args.until > args.since
    tours = [_tour("Vietnam", "published", _version("b"), _version("a", writer="v2"),
                   atoms={"fingerprint_days": 2, "atoms_off_days": 0})]
    report = wr.build(tours, args)
    md = wr.markdown(report["countries"], report["stop_conditions"])
    assert "| Vietnam | 1 | 1 | 0 |" in md and "Stop conditions" in md


def test_stayed_v1_is_broken_down_by_the_brand_audit_and_fact_check_holds_count():
    """AA-645 review round: a stayed_v1 driven by the brand audit shows (status + codes), the v2 report-only voice
    codes are counted, and an elephant hold (FACT_CHECK_MANUAL_CHECK) is counted (an exact `in` never matched it)."""
    rows = [_tour("Laos", "stayed_v1", _version("b1"),
                  _version("a1", writer="v2", audit="manual_check", codes=["FACT_CHECK_MANUAL_CHECK"])),
            _tour("Laos", "stayed_v1", _version("b2"),
                  _version("a2", writer="v2", audit="flagged", codes=["SUBTITLE_CITY_LIST"])),
            _tour("Laos", "published", _version("b3"), _version("a3", writer="v2", report_only=["BRAND_TONE_HYPE"]),
                  atoms={"fingerprint_days": 2, "atoms_off_days": 0})]
    lao = wr.aggregate(rows)["Laos"]
    assert lao["stayed_v1_by_brand_audit"] == {"flagged": 1, "manual_check": 1}
    assert lao["stayed_v1_brand_audit_codes"] == {"FACT_CHECK_MANUAL_CHECK": 1, "SUBTITLE_CITY_LIST": 1}
    assert lao["brand_audit_report_only_codes_after"] == {"BRAND_TONE_HYPE": 1}
    assert lao["fact_check_holds"] == 1


def test_version_facts_reads_the_audit_status_and_the_v2_report_only_codes():
    tour = wr.source_tour(TOUR)
    from services.quality.s1_facts import build_fact_table
    shadow = [{"code": "BRAND_TONE_HYPE", "detector": "s1_writer_v2_brand_audit"},
              {"code": "DFS_INTENT_UNDERUSED", "detector": "s1_writer_v2"}]
    stored = {"id": "v9", "status": "hitl",
              "metadata": json.dumps({"writer_version": "v2", "quality_shadow": shadow}),
              "aa_name": "Hanoi", "aa_subtitle": "s", "aa_summary": "s", "aa_description": "", "aa_highlights": "[]",
              "aa_itineraries": "Day 1 — Hanoi\nWalk.", "seo_title": "t", "seo_meta": "m", "failure_codes": None,
              "brand_audit_status": "flagged", "brand_audit_codes": '["SUBTITLE_CITY_LIST"]', "score_overall": 7.0,
              "review_status": "pending"}
    v = wr.version_facts(stored, tour, build_fact_table(tour))
    assert v["brand_audit_status"] == "flagged" and v["brand_audit_report_only"] == ["BRAND_TONE_HYPE"]
