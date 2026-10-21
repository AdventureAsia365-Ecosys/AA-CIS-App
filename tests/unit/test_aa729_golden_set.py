"""AA-729 — golden-set harness (scripts/aa729/): selection math, PII scrubber, cost guard, label rules,
metrics, report rendering, panel-label merge, and the guards on the private names the harness patches.
No DB, no LLM, no AWS."""
import inspect
import json
import typing

import pytest

from scripts.aa729 import common, evalpatch, injection, labels, report, score, select_samples
from scripts.aa729.common import Candidate, CostCapReached, CostGuard

# ── selection math ───────────────────────────────────────────────────────────────────────────────


def test_allocate_sums_to_total_and_is_proportional():
    out = common.allocate(10, {"India": 60, "Nepal": 30, "Laos": 10})
    assert sum(out.values()) == 10
    assert out == {"India": 6, "Nepal": 3, "Laos": 1}


def test_allocate_floors_then_largest_remainder_and_capacity():
    out = common.allocate(5, {"A": 100, "B": 1, "C": 1}, floors={"B": 1, "C": 1})
    assert out == {"A": 3, "B": 1, "C": 1}
    # a group never gets more than it has
    out = common.allocate(6, {"A": 2, "B": 10})
    assert out["A"] <= 2 and sum(out.values()) == 6
    # more requested than available: everything available, no more
    assert sum(common.allocate(50, {"A": 3, "B": 4}).values()) == 7


def test_allocate_floors_beyond_total_go_to_heaviest_groups():
    out = common.allocate(2, {"A": 5, "B": 3, "C": 1}, floors={"A": 1, "B": 1, "C": 1})
    assert out == {"A": 1, "B": 1, "C": 0}


def _cands():
    out = []
    countries = ["India"] * 30 + ["Nepal"] * 12 + ["Laos"] * 6 + ["Japan"] * 2
    for i, c in enumerate(countries):
        tags = set()
        if i % 7 == 0:
            tags.add("cycle_bike")
        if i % 5 == 0:
            tags.add("known_failure")
        out.append(Candidate(id=f"t{i:03d}", group=c, tags=tags, labelled=(i % 4 == 0)))
    return out


def test_pick_stratified_quota_first_spread_and_deterministic():
    quotas = [("cycle_bike", 4), ("known_failure", 5)]
    picks = common.pick_stratified(_cands(), total=20, quotas=quotas, seed=729)
    ids = [c.id for c, _ in picks]
    assert len(ids) == 20 and len(set(ids)) == 20
    reasons = [r for _, r in picks]
    assert reasons.count("cycle_bike") == 4 and reasons.count("known_failure") == 5
    assert all("cycle_bike" in c.tags for c, r in picks if r == "cycle_bike")
    assert {c.group for c, _ in picks} == {"India", "Nepal", "Laos", "Japan"}  # every country represented
    again = common.pick_stratified(_cands(), total=20, quotas=quotas, seed=729)
    assert [c.id for c, _ in again] == ids
    other = common.pick_stratified(_cands(), total=20, quotas=quotas, seed=1)
    assert [c.id for c, _ in other] != ids


def test_pick_stratified_quota_round_robins_groups_and_prefers_labelled():
    cands = [Candidate(id=f"a{i}", group="A", tags={"x"}, labelled=False) for i in range(10)]
    cands += [Candidate(id=f"b{i}", group="B", tags={"x"}, labelled=(i == 3)) for i in range(10)]
    picks = common.pick_stratified(cands, total=4, quotas=[("x", 4)], seed=3)
    assert sorted(c.group for c, _ in picks) == ["A", "A", "B", "B"]
    assert "b3" in [c.id for c, _ in picks]  # the labelled one is taken first in its group


def test_pick_stratified_spreads_the_second_key_inside_each_group():
    # one country, three tour types: the spread cycles over the types instead of taking the first N
    cands = [Candidate(id=f"c{i:02d}", group="Laos", sub="cultural_general") for i in range(20)]
    cands += [Candidate(id=f"t{i}", group="Laos", sub="trek_hike") for i in range(2)]
    cands += [Candidate(id=f"w{i}", group="Laos", sub="water") for i in range(2)]
    picks = common.pick_stratified(cands, total=6, quotas=[], seed=729)
    subs = sorted(c.sub for c, _ in picks)
    assert subs == ["cultural_general", "cultural_general", "trek_hike", "trek_hike", "water", "water"]


def test_s1_quotas_give_every_tour_type_with_candidates_a_sample():
    types = ["cultural_general"] * 60 + ["cycle_bike"] * 8 + ["trek_hike"] * 3 + ["water"] * 1 + ["wildlife"] * 1 \
        + ["culinary"] * 1
    cands = []
    for i, t in enumerate(types):
        tags = {t} if t != "cultural_general" else set()
        if i % 6 == 0:
            tags.add("known_failure")
        cands.append(Candidate(id=f"s{i:03d}", group=["India", "Nepal", "Laos"][i % 3], sub=t, tags=tags))
    picks = common.pick_stratified(cands, total=common.MIX["s1"], quotas=common.QUOTAS["s1"], seed=729)
    assert {c.sub for c, _ in picks} == set(types)
    assert {c.group for c, _ in picks} == {"India", "Nepal", "Laos"}


def test_pick_stratified_fewer_candidates_than_total():
    cands = [Candidate(id=f"x{i}", group="G") for i in range(3)]
    assert len(common.pick_stratified(cands, total=10, quotas=[], seed=1)) == 3


def test_tour_classification():
    assert common.duration_days("18 Days", "") == 18
    assert common.duration_days("18D17N", "") == 18
    assert common.duration_days(None, "LAOS BY BIKE 21 DAYS") == 21
    assert common.duration_days("", "Day trip") is None
    assert common.tour_type("Laos to Vietnam by Bicycle", None) == "cycle_bike"
    assert common.tour_type("Everest Base Camp", '["trekking"]') == "trek_hike"
    assert common.tour_type("Culinary Gems of Bhutan", None) == "culinary"
    assert common.tour_type("Temples of Kyoto", ["culture"]) == "cultural_general"
    assert common.percentile([5, 1, 3, 2, 4], 0.2) == 1.0
    assert common.percentile([10, 20, 30, 40], 0.5) == 20.0


# ── PII ──────────────────────────────────────────────────────────────────────────────────────────


def test_scrub_masks_email_and_phone_keeps_numbers_that_are_not_phones():
    text = ("Mail jane.doe@example.com or call +84 28 3822 1234. Climb to 4,000 m in 1637–1955, "
            "price USD 1,250, 2025-2026 season, 08:00 - 12:00.")
    out = common.scrub_text(text)
    assert "jane.doe@example.com" not in out and common.EMAIL_MASK in out
    assert "3822 1234" not in out and common.PHONE_MASK in out
    for keep in ("4,000 m", "1637–1955", "USD 1,250", "2025-2026", "08:00 - 12:00"):
        assert keep in out


def test_scrub_drops_personal_keys_keeps_tenant_names_and_find_pii():
    raw = {"tenant_slug": "terra-family", "reviewed_by": "someone", "notes": ["ping a@b.co"],
           "nested": {"labelled_by": "x", "brand_name": "Terra Family"}}
    assert set(common.find_pii(raw)) == {"$.reviewed_by (key)", "$.notes[0]", "$.nested.labelled_by (key)"}
    clean = common.scrub(raw)
    assert clean == {"tenant_slug": "terra-family", "notes": [f"ping {common.EMAIL_MASK}"],
                     "nested": {"brand_name": "Terra Family"}}
    assert common.find_pii(clean) == []
    assert common.scrub(clean) == clean  # idempotent


# ── cost guard ───────────────────────────────────────────────────────────────────────────────────


def test_cost_guard_stops_before_the_cap_and_counts_in_flight():
    g = CostGuard(cap_usd=1.0)
    g.check(sample_id="a", prior=0.4, logged_usd=0.0)
    g.check(sample_id="b", prior=0.4, logged_usd=0.0)  # 0.8 reserved
    with pytest.raises(CostCapReached):
        g.check(sample_id="c", prior=0.4, logged_usd=0.0)
    g.done("a", 0.1)
    g.done("b", 0.1)
    g.check(sample_id="c", prior=0.4, logged_usd=0.2)  # 0.2 + 0.4 <= 1.0
    with pytest.raises(CostCapReached):
        g.check(sample_id="d", prior=0.4, logged_usd=0.25)  # 0.25 + 0.4 in flight + 0.4 > 1.0


def test_cost_guard_uses_local_spend_and_observed_mean():
    g = CostGuard(cap_usd=1.0)
    with pytest.raises(CostCapReached):  # the log lost rows; in-process spend still stops the run
        g.check(sample_id="a", prior=0.1, logged_usd=0.0, local_usd=0.95)
    g.done("x", 0.6)  # observed mean 0.6 > prior 0.1
    assert g.estimate(0.1) == pytest.approx(0.6)
    with pytest.raises(CostCapReached):
        g.check(sample_id="b", prior=0.1, logged_usd=0.5)


def test_cost_guard_keeps_a_timed_out_samples_reservation():
    g = CostGuard(cap_usd=1.0)
    g.check(sample_id="a", prior=0.4, logged_usd=0.0)
    g.done("a", 0.1, release=False)  # timed out: its thread may still spend
    with pytest.raises(CostCapReached):
        g.check(sample_id="b", prior=0.4, logged_usd=0.3)  # 0.3 + 0.4 reserved + 0.4 > 1.0
    g2 = CostGuard(cap_usd=1.0)
    g2.check(sample_id="a", prior=0.4, logged_usd=0.0)
    g2.done("a", 0.1)
    g2.check(sample_id="b", prior=0.4, logged_usd=0.3)


def test_budget_refuses_a_call_at_the_cap_and_records_the_sample(monkeypatch):
    spend, budget = evalpatch.Spend(), evalpatch.Budget()
    monkeypatch.setattr(evalpatch, "SPEND", spend)
    budget.configure(None)
    budget.check("job-0")  # no cap: never refuses
    budget.configure(1.0, slots=2)  # stop at 1.0 - 2 x 0.10
    spend.add(0.30)
    budget.check("job-1")
    budget.note_logged(0.65)  # Jev / embeddings seen through the log
    assert budget.projected() == pytest.approx(0.65)
    spend.add(0.10)  # a call after the read counts on top of it
    assert budget.projected() == pytest.approx(0.75)
    budget.check("job-1")  # 0.75 + 0.20 margin < 1.0
    spend.add(0.06)
    with pytest.raises(CostCapReached):
        budget.check("job-2")
    assert budget.tripped and budget.tripped_jobs == {"job-2"}
    budget.configure(1.0)
    assert budget.tripped is None and not budget.tripped_jobs


def test_estimate_run_matches_the_memo_range():
    fixture_kinds = (["s1"] * 50 + ["t2"] * 10 + ["t9_blog"] * 15 + ["t9_linkedin"] * 15
                     + ["injection_s1"] * 5 + ["injection_t9"] * 5)
    est = common.estimate_run(fixture_kinds, "regenerate")
    assert 8.0 <= est <= 15.0
    assert common.estimate_run(fixture_kinds, "score-only") < 3.0


# ── labels, Jev, injection ───────────────────────────────────────────────────────────────────────


def test_make_label_validates_and_first_label_keeps_evidence():
    with pytest.raises(ValueError):
        common.make_label("maybe", "human_review")
    with pytest.raises(ValueError):
        common.make_label("pass", "pipeline")
    lab = common.first_label([common.needs_panel_label([{"e": 1}]), common.make_label("fail", "admin_edit")])
    assert lab["verdict"] == "fail" and lab["source"] == "admin_edit" and lab["evidence"] == [{"e": 1}]
    assert common.first_label([None])["source"] == "needs_panel"


def test_s1_label_priority_admin_edit_then_review_then_jev():
    r = {"tour_id": "t1", "gc_id": "g2", "human_edited": False}
    reviews = [{"gc_id": "g1", "review_status": "rejected", "failure_summary": "old"},
               {"gc_id": "g2", "review_status": "approved", "reviewer_notes": "ok", "failure_summary": ""}]
    lab, post = select_samples.s1_label(r, reviews, [], {})
    assert (lab["verdict"], lab["source"], post) == ("pass", "human_review", False)
    lab, post = select_samples.s1_label({**r, "human_edited": True}, reviews, [], {})
    assert (lab["verdict"], lab["source"], post) == ("fail", "admin_edit", True)
    jev = [{"question_key": "a1_claim_supported", "zone": "reject", "outcome": json.dumps({"truth": False})}]
    lab, _ = select_samples.s1_label({**r, "gc_id": "g9"}, reviews, jev, {})
    assert (lab["verdict"], lab["source"]) == ("fail", "jev_outcome")
    lab, _ = select_samples.s1_label({**r, "gc_id": "g9"}, [], [], {"t1": {"verdict": "pass", "source": "S218"}})
    assert (lab["verdict"], lab["source"]) == ("pass", "audit_verdict")
    lab, _ = select_samples.s1_label({**r, "gc_id": "g9"}, [], [], {})
    assert lab["source"] == "needs_panel" and lab["verdict"] is None


def test_t9_label_human_label_majority_and_post_edit_by_hash():
    text = "The walk takes two hours."
    r = {"piece_id": "p1", "content_text": text}
    labs = [{"label": "light", "content_sha256": select_samples._sha(text)},
            {"label": "heavy", "content_sha256": select_samples._sha(text)},
            {"label": "as_is", "content_sha256": select_samples._sha(text)}]
    lab, post = select_samples.t9_label(r, labs, {"content_piece.admin_sentence_cut": 1}, {})
    assert (lab["verdict"], lab["source"], post) == ("pass", "human_label", False)
    lab, post = select_samples.t9_label(r, [], {"content_piece.admin_sentence_cut": 2}, {})
    assert (lab["verdict"], lab["source"], post) == ("fail", "admin_edit", True)
    lab, _ = select_samples.t9_label(r, [], {"content_piece.published": 1}, {})
    assert lab["source"] == "needs_panel" and {"tenant_published": True} in lab["evidence"]


def test_t2_label_tenant_edit_and_tenant_review():
    r = {"version_id": "v1", "tenant_edited": True, "status": "approved"}
    lab, post = select_samples.t2_label(r, [], {}, {})
    assert (lab["verdict"], lab["source"], post) == ("fail", "tenant_edit", False)
    lab, _ = select_samples.t2_label({**r, "tenant_edited": False}, [], {}, {})
    assert (lab["verdict"], lab["source"]) == ("pass", "tenant_review")
    lab, _ = select_samples.t2_label({**r, "tenant_edited": False, "status": "ai_generated"}, [], {}, {})
    assert lab["source"] == "needs_panel"


def test_jev_polarity_and_summary():
    assert common.jev_problem("a1_claim_supported", "reject") is True
    assert common.jev_problem("a1_promise_unsupported", "accept") is True
    assert common.jev_problem("t10_brand_voice", "accept") is False
    assert common.jev_problem("a1_claim_supported", "grey") is None
    assert common.jev_problem("a3_activity_type", "reject") is None
    s = common.jev_summary([{"question_key": "a1_claim_supported", "zone": "accept", "n": 5},
                            {"question_key": "a1_claim_supported", "zone": "grey", "n": 2}])
    assert s == {"ran": True, "problem": False, "zones": {"a1_claim_supported": {"accept": 5, "grey": 2}}}
    assert common.jev_summary([])["ran"] is False


def test_injection_fixtures_violate_and_clean_text_does_not():
    assert len(injection.S1_ATTACKS) == 5 and len(injection.T9_ATTACKS) == 5
    for a in injection.S1_ATTACKS:
        assert a["text"] in common.flatten_text(injection.tour_source(a))
        assert common.injection_violations(common.flatten_text(injection.compromised_s1_output(a)),
                                           injection.spec_of(a))
    for a in injection.T9_ATTACKS:
        assert a["text"] in injection.t9_seed(a)
        assert common.injection_violations(injection.compromised_t9_output(a), injection.spec_of(a))
    clean = "A two-hour walk to the hill monastery, with a picnic lunch in the courtyard."
    assert common.injection_violations(clean, injection.spec_of(injection.T9_ATTACKS[1])) == []


def test_injection_samples_use_a_host_context_and_drop_its_scope():
    host = {"sample_id": "t9b-001", "channel": "blog", "refs": {"tenant_slug": "terra-family"},
            "input": {"request_id": "00000000-0000-0000-0000-0000000000aa", "tenant_id": "t-uuid",
                      "context": {"atom_text": "real seed", "scope": "Punakha", "keyword": "punakha tour",
                                  "cta": "Ask us", "channel": "blog"}}}
    lin = {**host, "sample_id": "t9l-001", "channel": "linkedin",
           "input": {**host["input"], "context": {**host["input"]["context"], "channel": "linkedin"}}}
    out = select_samples.injection_samples([host, lin], "s1:default", 729)
    assert len(out) == 10 and {s["kind"] for s in out} == {"injection_s1", "injection_t9"}
    t9 = [s for s in out if s["kind"] == "injection_t9"]
    assert all(s["input"]["context"]["scope"] is None and "AA729-CANARY" in s["input"]["context"]["atom_text"]
               and s["input"]["context"]["cta"] == "Ask us" for s in t9)
    assert all(s["label"]["source"] == "synthetic" and s["label"]["verdict"] == "fail" for s in out)
    assert common.find_pii(out) == []


def test_seo_from_row_without_a_row_is_the_empty_shape():
    assert select_samples.seo_from_row(None) == {"top_keywords": [], "keywords": {"top_keywords": []},
                                                 "people_also_ask": []}
    seo = select_samples.seo_from_row({"top_keywords": '["a", "b"]', "keyword_ideas": None,
                                       "people_also_ask": "[]", "related_keywords": None, "keyword_search": "x"})
    assert seo["top_keywords"] == ["a", "b"] and seo["keywords"]["top_keywords"] == ["a", "b"]


# ── metrics ──────────────────────────────────────────────────────────────────────────────────────


def _row(sid, passed, label, *, post_edit=False, cost=0.01, writer=1, jev=None, error=None, inj=None):
    return {"sample_id": sid, "stratum": "s1", "verdict": {"passed": passed}, "label_verdict": label,
            "post_edit": post_edit, "cost_usd": cost, "calls": {"writer": writer}, "error": error,
            "jev": jev or {"ran": False, "problem": None}, "injection": inj}


def test_stratum_metrics_score_only_disagreement_excludes_post_edit_and_unlabelled():
    rows = [_row("a", True, "pass"), _row("b", True, "fail"), _row("c", False, "pass"),
            _row("d", True, "fail", post_edit=True), _row("e", False, None), _row("f", None, "pass", error="boom")]
    m = common.stratum_metrics(rows, "score-only")
    assert m["n"] == 6 and m["errors"] == 1 and m["scored"] == 5
    assert m["pass_rate"] == pytest.approx(3 / 5)
    assert m["disagreement"] == {"n": 3, "disagree": 2, "rate": pytest.approx(2 / 3, abs=1e-3), "false_pass": 1,
                                 "false_fail": 1}
    assert m["cost_usd"] == pytest.approx(0.06) and m["writer_calls_per_sample"] == 1.0


def test_stratum_metrics_regenerate_by_label_jev_and_injection():
    rows = [_row("a", True, "fail", jev={"ran": True, "problem": False}),
            _row("b", False, "fail", jev={"ran": True, "problem": True}),
            _row("c", True, "pass", inj={"violations": []}),
            _row("d", True, "fail", inj={"violations": ["canary X present"]})]
    m = common.stratum_metrics(rows, "regenerate")
    assert "disagreement" not in m
    assert m["pass_rate_by_frozen_label"]["fail"] == {"n": 3, "pass_rate": pytest.approx(2 / 3, abs=1e-3)}
    assert m["jev"]["ran"] == 2 and m["jev"]["agree_with_verdict"] == 1.0 and m["jev"]["agree_with_label"] == 0.5
    assert m["injection_defended_rate"] == 0.5


def test_plan_limit_round_robins_strata_and_skips_done():
    samples = [{"sample_id": f"{k}-{i}", "stratum": k, "kind": "t9" if k.startswith("t9") else k}
               for k in ("s1", "t2", "t9_blog", "t9_linkedin") for i in range(3)]
    assert [s["sample_id"] for s in score.plan(samples, limit=3)] == ["s1-0", "t9_blog-0", "t9_linkedin-0"]
    assert [s["sample_id"] for s in score.plan(samples, limit=2, done={"s1-0"})] == ["s1-1", "t9_blog-0"]
    assert len(score.plan(samples, strata={"t2"})) == 3
    est = score.estimate(score.plan(samples, limit=3), "regenerate")
    assert est["estimated_usd"] == pytest.approx(0.06 + 0.14 + 0.06)


# ── report ───────────────────────────────────────────────────────────────────────────────────────


def _score(run_id, passed_b, version="1.1.0", mode="score-only"):
    rows = [{**_row("s1-001", True, "pass"), "group": "Nepal", "label_source": "human_review"},
            {**_row("s1-002", passed_b, "fail"), "group": "India | north", "label_source": "agent_panel",
             "verdict": {"passed": passed_b, "failure_codes": ["FORBIDDEN_WORD"]}}]
    return {"schema": "aa729-score", "run_id": run_id, "mode": mode, "fixture_version": version,
            "started_at": "2026-10-20T00:00:00+00:00", "updated_at": "2026-10-20T01:00:00+00:00", "final": True,
            "cap_usd": 15, "stopped": None, "planned": ["s1-001", "s1-002"], "samples": rows,
            "metrics": {"s1": common.stratum_metrics(rows, mode), "all": common.stratum_metrics(rows, mode)},
            "totals": {"cost_usd_llm_call_log": 0.02, "local_llm_usd": 0.018, "local_llm_calls": 4,
                       "logged_llm_rows": 4, "unlogged_llm_calls": 0,
                       "cost_source": "shared.llm_call_log stage=adhoc_aa729"}}


def test_report_renders_sections_and_escapes_pipes():
    text = report.render(_score("r1", False))
    for heading in ("## Cost", "## Metrics per stratum", "## Label provenance", "## Per sample", "## How to read"):
        assert heading in text
    assert "$0.0200" in text and "India \\| north" in text
    assert "50.0%" in text  # s1 pass-rate 1/2
    assert "codes: FORBIDDEN_WORD" in text


def test_report_before_after_same_version_only_and_lists_flips():
    before, after = _score("r1", False), _score("r2", True)
    text = report.render(after, baseline=before)
    assert "## Before / after" in text and "+50.0 pt" in text and "| s1-002 | False | True |" in text
    with pytest.raises(ValueError):
        report.render(after, baseline=_score("r0", False, version="1.0.0"))
    with pytest.raises(ValueError):
        report.render(after, baseline=_score("r0", False, mode="regenerate"))
    with pytest.raises(ValueError):
        report.render({"schema": "other"})


# ── panel labels ─────────────────────────────────────────────────────────────────────────────────


def _fixture():
    return {"fixture_version": "1.0.0", "samples": [
        {"sample_id": "s1-001", "stratum": "s1", "label_source": "needs_panel", "label": common.needs_panel_label(),
         "output": {"summary": "x"}, "input": {"tour": {"name": "T"}}},
        {"sample_id": "s1-002", "stratum": "s1", "label_source": "human_review",
         "label": common.make_label("pass", "human_review"), "output": {}, "input": {}},
        {"sample_id": "t9b-001", "stratum": "t9_blog", "label_source": "needs_panel",
         "label": common.needs_panel_label(), "output": {"content_text": "y"},
         "input": {"context": {"atom_text": "seed", "brand_audience": {"brand_name": "Terra"}}}}]}


def test_panel_pack_hides_the_pipeline_verdict_and_gate_results():
    fx = _fixture()
    fx["samples"][2].update({"tags": ["held", "known_failure"], "pipeline_verdict": {"passed": False,
                             "status": "held", "held_reason": "F1_grounding"},
                             "output": {"content_text": "y", "gate_ledger": [{"gate": "F1_grounding"}],
                                        "flags": [{"gate": "F15"}]}})
    fx["samples"][0].update({"tags": ["cycle_bike", "known_failure"], "pipeline_verdict": {"passed": True}})
    pack = labels.export_pack(fx)
    blob = json.dumps(pack) + labels.pack_markdown(pack)
    for word in ("pipeline_verdict", "gate_ledger", "flags", "held", "F1_grounding", "known_failure"):
        assert word not in blob, word
    assert pack["items"][0]["tags"] == ["cycle_bike"] and pack["items"][1]["output"] == {"content_text": "y"}


def test_labels_export_and_apply():
    pack = labels.export_pack(_fixture())
    assert pack["count"] == 2 and "seed" in pack["items"][1]["source"]
    assert "## t9b-001" in labels.pack_markdown(pack)
    panel = {"s1-001": {"verdict": "fail", "grade": "heavy", "reason": "invented altitude; ask x@y.io"},
             "s1-002": {"verdict": "fail"}, "t9b-001": {"verdict": "pass", "status": "disputed"}}
    new, info = labels.apply_labels(_fixture(), panel, approved_by="nghiep")
    assert new["fixture_version"] == "1.1.0" and info["applied"] == ["s1-001"]
    assert ("s1-002", "already labelled by human_review") in info["skipped"]
    assert ("t9b-001", "disputed") in info["skipped"] and info["needs_panel_left"] == 1
    s = new["samples"][0]
    assert s["label_source"] == "agent_panel" and s["label"]["strength"] == "panel"
    assert "x@y.io" not in s["label"]["reason"] and new["label_history"][0]["approved_by"] == "nghiep"
    new2, info2 = labels.apply_labels(_fixture(), panel, approved_by="nghiep", include_disputed=True)
    assert set(info2["applied"]) == {"s1-001", "t9b-001"}
    with pytest.raises(ValueError):
        labels.apply_labels(_fixture(), panel, approved_by=" ")


# ── guards on the names the harness patches ──────────────────────────────────────────────────────


def test_relabel_sql_matches_the_live_insert_and_validates_the_run_id():
    from shared.llm_client import call_log

    run = "6f2b8a3e-5d7c-4e1a-9b0f-2c3d4e5f6a7b"
    sql = evalpatch.relabel_sql(call_log._INSERT_SQL, run)
    assert f"'{common.STAGE}'" in sql and f"'{run}'::text" in sql and "'aa729_stage', $2::text" in sql
    assert all(f"${i}" in sql for i in range(1, 16)) and "$16" not in sql
    values = sql.split(" VALUES (", 1)[1]
    assert values.startswith(f"NULL, '{common.STAGE}', $3,")  # tenant_id NULL, stage overridden
    assert ", NULL, NULL, $11," in values  # content_piece_id, angle_gate_request_id NULL
    for key, param in (("aa729_tenant", "$1"), ("aa729_piece", "$9"), ("aa729_request", "$10")):
        assert f"'{key}', {param}::uuid::text" in sql
    with pytest.raises(ValueError):
        evalpatch.relabel_sql(call_log._INSERT_SQL, "1'; DROP TABLE x; --")
    with pytest.raises(RuntimeError):
        evalpatch.relabel_sql(call_log._INSERT_SQL.replace("job_id", "batch_id"), run)


def test_decision_log_relabel_matches_the_live_statements():
    from shared.llm_client import decide

    log = evalpatch.relabel_decision_sql(decide._LOG_SQL)
    assert f"VALUES ('{common.STAGE}:' || $1::text, $2, $3," in log
    assert all(f"${i}" in log for i in range(1, 18)) and "$18" not in log
    hits = evalpatch.relabel_hits_sql(decide._HITS_SQL)
    assert f"VALUES ($1, '{common.STAGE}:' || $2::text, $3, $4, $5, $6) ON CONFLICT" in hits
    with pytest.raises(RuntimeError):
        evalpatch.relabel_decision_sql(decide._LOG_SQL.replace("subject_key", "subject"))
    with pytest.raises(RuntimeError):
        evalpatch.relabel_hits_sql(decide._HITS_SQL.replace("$6", "$6, $7"))
    for fn in (decide._insert_logs,):
        assert {"_LOG_SQL", "_HITS_SQL"} <= set(fn.__code__.co_names), "decide no longer reads them at call time"


def test_call_log_writers_read_the_insert_sql_at_call_time():
    from shared.llm_client import call_log

    # AA-927: record_calls is the batch writer under record_calls_sync / record_billed_failure*.
    for fn in (call_log.record_call, call_log.record_call_with_pool, call_log.record_calls):
        assert "_INSERT_SQL" in fn.__code__.co_names, f"{fn.__name__} no longer uses the module-level INSERT"


def test_billed_failure_rows_use_the_relabelled_insert(monkeypatch):
    """AA-927 (p): record_calls_sync / record_billed_failure_sync write through the module _INSERT_SQL as it
    is AFTER the harness relabel, so eval failure rows never land under the real stage or tenant."""
    import asyncio

    from shared.llm_client import call_log

    run = "8f07b98f-aec7-46c9-a1a5-661bf4735db1"
    monkeypatch.setattr(call_log, "_INSERT_SQL", evalpatch.relabel_sql(call_log._INSERT_SQL, run))
    seen = []

    class Conn:
        def transaction(self):
            class T:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False
            return T()

        async def executemany(self, sql, args):
            seen.append((sql, list(args)))

        async def close(self):
            pass

    async def connect(*a, **k):
        return Conn()

    monkeypatch.setattr(call_log.asyncpg, "connect", connect)
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://x")
    call_log.record_billed_failure_sync(stage="s1_judge", role="judge", error="bad json",
                                        billed=[{"model": "satellite-gpt-5.6-luna", "cost_usd": 0.01}],
                                        tenant_id="00000000-0000-0000-0000-0000000000aa")
    call_log.record_calls_sync([dict(stage="t2_judge", role="judge", model="m", tokens_in=1, tokens_out=1,
                                     cost_usd=0.02, quality_signal={"judge_run": 2})])
    assert len(seen) == 2
    for sql, args in seen:
        assert f"'{common.STAGE}'" in sql and "aa729_stage" in sql and run in sql
        assert all(len(a) == evalpatch.N_PARAMS for a in args)
    assert seen[0][1][0][1] == "s1_judge"            # $2 = the real stage, moved into quality_signal by the SQL
    assert asyncio.iscoroutinefunction(call_log.record_calls)


def test_evalpatch_counts_per_stage_and_embedding_responses(monkeypatch):
    """AA-927: SPEND counts LLMClient responses per real stage (with provider_requests) and every
    embedding provider response through embed._call (which embed() looks up at call time)."""
    from shared.llm_client import embed as embed_mod

    spend = evalpatch.Spend()
    spend.add(0.01, stage="s1_judge")
    spend.add(0.02, stage="s1_judge", provider_requests=2)
    spend.add(0.03, stage="t10_judge")
    spend.add_embed(0.0001)
    snap = spend.snapshot()
    assert snap["calls"] == 3 and snap["by_stage"]["s1_judge"] == {"responses": 2, "provider_requests": 3,
                                                                  "usd": pytest.approx(0.03)}
    assert snap["embed_calls"] == 1
    assert "_call" in embed_mod.embed.__code__.co_names, "embed() no longer looks _call up at call time"


def test_reconcile_is_signed_per_stage_and_filters_non_llm_rows():
    rows = [
        {"real_stage": "s1_judge", "role": "judge", "provider": "openai", "gateway_attempt": False, "n": 3,
         "usd": 0.03},
        {"real_stage": "s1_judge", "role": "judge", "provider": "bedrock-satellite", "gateway_attempt": True,
         "n": 1, "usd": 0.005},
        {"real_stage": "s1_grounding", "role": "validate", "provider": "typesafe", "gateway_attempt": False,
         "n": 4, "usd": 0.001},
        {"real_stage": "f10_embed", "role": "embed", "provider": "bedrock-native", "gateway_attempt": False,
         "n": 2, "usd": 0.0002},
        {"real_stage": "t2_judge", "role": "judge", "provider": "openai", "gateway_attempt": False, "n": 4,
         "usd": 0.04},
    ]
    spend = {"usd": 0.06, "calls": 6, "embed_calls": 2,
             "by_stage": {"s1_judge": {"responses": 3, "provider_requests": 3, "usd": 0.03},
                          "t2_judge": {"responses": 3, "provider_requests": 3, "usd": 0.03}}}
    rec = score.reconcile(rows, spend)
    assert rec["rows_minus_calls_by_stage"] == {"s1_judge": 0, "t2_judge": 1}   # over-logging shows (+1)
    assert rec["rows_minus_calls_nonzero"] == {"t2_judge": 1}
    assert rec["gateway_attempt_rows"] == 1 and rec["gateway_attempt_usd"] == pytest.approx(0.005)
    assert rec["logged_llm_usd"] == pytest.approx(0.07) and rec["cost_diff_usd"] == pytest.approx(0.01)
    assert rec["local_embed_calls"] == 2 and rec["logged_embed_rows"] == 2


def test_patched_names_still_exist():
    from services.acp_content_writing import service
    from services.content_generation import flag_fix_node
    from shared.llm_client.client import LLMClient

    from shared.llm_client import decide

    for owner, name in ((decide, "_LOG_SQL"), (decide, "_HITS_SQL"),
                        (service, "_finalize_piece"), (service, "_insert_placeholder_piece"),
                        (flag_fix_node, "_write_lessons_safe"), (LLMClient, "_start_shadow"),
                        (LLMClient, "generate")):
        assert hasattr(owner, name), name
    from shared.llm_client import embed as embed_mod
    assert hasattr(embed_mod, "_call")  # AA-927: the embedding counter wraps it
    assert "_finalize_piece" in service.run_write_background.__code__.co_names
    fin = inspect.signature(service._finalize_piece).parameters
    assert "piece_id" in fin and fin["piece_id"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("channel_spec", "hygiene", "trip_facts", "run_quality_gates", "F9PassMemory", "_grounding_text",
                 "_add_date_spans"):
        assert hasattr(service, name), name


def test_graph_state_keys_are_declared_in_content_state():
    from services.content_generation.graph import ContentState

    declared = set(typing.get_type_hints(ContentState))
    state = score.graph_state({"name": "T"}, {}, {"system_prompt": "x"}, is_tenant=True, tenant_id="t",
                              generate_stage="t2_generate", generated={"name": "T"})
    assert set(state) <= declared, set(state) - declared


# ── scoring paths with the model calls stubbed ───────────────────────────────────────────────────


def _t9_sample(channel="blog"):
    ctx = {"atom_text": "Hill monastery walk — two hours on stone steps; picnic lunch in the courtyard.",
           "channel": channel, "brand_audience": {"brand_name": "Terra Family", "customer_segment": "families"},
           "facts_text": "", "scope": None, "trip_name": "Bhutan Family Trip", "cta": "Ask us about this trip.",
           "trip_places": [], "chosen": {"name": "The slow climb"}, "keyword": None, "brand_rubric_text": "",
           "sibling_openers": [], "destination": "Bhutan", "route_segments": None}
    return {"sample_id": "t9b-001", "stratum": "t9_blog", "kind": "t9", "channel": channel,
            "input": {"context": ctx, "request_id": "00000000-0000-0000-0000-0000000000a1",
                      "piece_id": "00000000-0000-0000-0000-0000000000b1", "tenant_id": None},
            "output": {"content_text": "The climb takes two hours.\n\nAsk us about this trip.", "seo_title": "T",
                       "meta_description": "M", "slug": "t"}}


def test_t9_score_only_builds_the_gate_inputs_the_service_uses(monkeypatch):
    import asyncio

    from services.acp_content_writing import service

    seen = {}

    def fake_gates(**kw):
        seen.update(kw)
        return {"passed": False, "first_failure": {"gate": "F9_cta_fact"}, "flags": [{"gate": "F15_voice_warn"}],
                "gate_ledger": [{"gate": "F9_cta_fact", "passed": False, "blocking": True},
                                {"gate": "F15_voice_warn", "passed": False, "blocking": False}]}

    monkeypatch.setattr(service, "run_quality_gates", fake_gates)
    verdict, out = asyncio.run(score.t9_score_only(_t9_sample()))
    assert out is None and verdict == {"passed": False, "first_failure": "F9_cta_fact",
                                       "blocking_failures": ["F9_cta_fact"], "flags": ["F15_voice_warn"],
                                       "degraded": {}, "degraded_hard": []}  # AA-925
    assert seen["channel"] == "blog" and seen["cannibalization_match"] is None and seen["sibling_texts"] == []
    assert "TRIP: " in seen["atom_text"] and "BRAND: Terra Family" in seen["atom_text"]
    assert seen["hygiene_ctx"] is not None and seen["trip_ctx"] is not None


def test_t9_regenerate_captures_the_piece_instead_of_writing_it(monkeypatch):
    import asyncio

    from services.acp_content_writing import service

    monkeypatch.setattr(service, "_finalize_piece", evalpatch._capture_finalize)

    async def fake_write(request_id, piece_id, context, pool):
        assert context["atom_text"].startswith("Hill monastery")
        await service._finalize_piece(pool, piece_id=piece_id, attempt_number=2, content_text="New text",
                                      status="held", held_reason="F1_grounding", repair_log=[{"attempt": 1}],
                                      gate_ledger=[{"gate": "F1_grounding", "passed": False}], flags=[])

    monkeypatch.setattr(service, "run_write_background", fake_write)
    verdict, out = asyncio.run(score.t9_regenerate(_t9_sample(), pool=None))
    assert verdict["passed"] is False and verdict["status"] == "held" and verdict["attempts"] == 2
    assert verdict["blocking_failures"] == ["F1_grounding"] and out["content_text"] == "New text"
    assert "00000000-0000-0000-0000-0000000000b1" not in evalpatch.T9_CAPTURE


def test_t9_regenerate_failed_or_missing_capture_is_an_error(monkeypatch):
    import asyncio

    from services.acp_content_writing import service

    monkeypatch.setattr(service, "_finalize_piece", evalpatch._capture_finalize)

    async def crashed(request_id, piece_id, context, pool):  # what run_write_background's except does
        await service._finalize_piece(pool, piece_id=piece_id, attempt_number=1, content_text="", status="failed",
                                      held_reason="ThrottlingException: rate", gate_ledger=[], repair_log=[],
                                      flags=[])

    monkeypatch.setattr(service, "run_write_background", crashed)
    with pytest.raises(score.WriterFailed, match="ThrottlingException"):
        asyncio.run(score.t9_regenerate(_t9_sample(), pool=None))

    async def nothing(request_id, piece_id, context, pool):  # the failed-status write failed too
        return None

    monkeypatch.setattr(service, "run_write_background", nothing)
    with pytest.raises(score.WriterFailed, match="no _finalize_piece capture"):
        asyncio.run(score.t9_regenerate(_t9_sample(), pool=None))


def test_s1_regenerate_failed_rewrite_or_retry_is_an_error(monkeypatch):
    import asyncio

    from api.routers import v1_pipeline

    async def failed(tour, idx, total, **kw):
        return {"status": "failed", "error": "ThrottlingException: rate"}

    monkeypatch.setattr(v1_pipeline, "_rewrite_tour", failed)
    sample = {"input": {"tour": {"name": "T"}, "seo": {}, "brand_rules_ref": "s1:default"}}
    fx = {"shared_inputs": {"brand_rules": {"s1:default": {}}}}
    with pytest.raises(score.WriterFailed, match="s1 rewrite: ThrottlingException"):
        asyncio.run(score.s1_regenerate(sample, fx))

    async def retry_fails(tour, idx, total, **kw):
        if kw.get("model_tier") == "sonnet":
            return {"status": "failed", "error": "CostCapReached: cap"}
        return {"status": "success", "model_used": "haiku-4-5", "quality_score": 8.0,
                "failure_codes": ["MISSING_FIELD"], "brand_audit_status": "pass", "generated": {"name": "H"}}

    monkeypatch.setattr(v1_pipeline, "_rewrite_tour", retry_fails)
    with pytest.raises(score.WriterFailed, match="Sonnet retry: CostCapReached"):
        asyncio.run(score.s1_regenerate(sample, fx))


def test_t2_regenerate_failed_rewrite_is_an_error_and_skips_qa(monkeypatch):
    import asyncio

    from api.routers import v1_pipeline
    from services.acp_produce import tenant_pipeline

    async def failed(tour, idx, total, **kw):
        return {"status": "failed", "error": "KeyError: 'brand'"}

    async def no_qa(*a, **k):
        raise AssertionError("QA must not run on a failed rewrite")

    monkeypatch.setattr(v1_pipeline, "_rewrite_tour", failed)
    monkeypatch.setattr(tenant_pipeline, "run_t3_qa_gate", no_qa)
    sample = {"input": {"tour": {"name": "T"}, "seo": {}, "brand_rules_ref": "t2:terra", "tenant_id": "tid"}}
    with pytest.raises(score.WriterFailed, match="t2 rewrite: KeyError"):
        asyncio.run(score.t2_regenerate(sample, {"shared_inputs": {"brand_rules": {"t2:terra": {}}}}))


def test_s1_regenerate_applies_the_sonnet_retry_rule(monkeypatch):
    import asyncio

    from api.routers import v1_pipeline

    calls = []

    async def fake_rewrite(tour, idx, total, **kw):
        calls.append(kw.get("model_tier"))
        if kw.get("model_tier") == "sonnet":
            return {"status": "success", "model_used": "sonnet-4-6", "quality_score": 8.0, "failure_codes": [],
                    "brand_audit_status": "pass", "generated": {"name": "Sonnet"}}
        return {"status": "success", "model_used": "haiku-4-5", "quality_score": 8.0,
                "failure_codes": ["MISSING_FIELD"], "brand_audit_status": "pass", "generated": {"name": "Haiku"}}

    monkeypatch.setattr(v1_pipeline, "_rewrite_tour", fake_rewrite)
    sample = {"input": {"tour": {"name": "T"}, "seo": {}, "brand_rules_ref": "s1:default"}}
    verdict, out = asyncio.run(score.s1_regenerate(sample, {"shared_inputs": {"brand_rules": {"s1:default": {}}}}))
    assert calls == [None, "sonnet"] and verdict["sonnet_retried"] is True
    assert verdict["passed"] is True and out == {"name": "Sonnet"}


def test_t2_score_only_checks_without_repairs(monkeypatch):
    import asyncio

    from services.acp_produce import tenant_pipeline

    async def fake_reval(state):
        assert state["is_tenant_rewrite"] is True and state["generate_stage"] == "t2_generate"
        assert state["generated"] == {"name": "V"} and state["brand_name"] == "Terra"
        return {**state, "quality_score": 7.5, "failure_codes": []}

    seen = {}

    async def fake_t3(tour, source_texts, result, brand_rules, max_repairs=2, seo_data=None, tenant_id=None):
        seen["max_repairs"] = max_repairs
        return {"passed": False, "attempts": 0, "structural_issues": ["S"], "grounding_issues": [{}],
                "result": result}

    monkeypatch.setattr(score, "run_revalidation", fake_reval)
    monkeypatch.setattr(tenant_pipeline, "run_t3_qa_gate", fake_t3)
    sample = {"input": {"tour": {"name": "T"}, "seo": {}, "brand_rules_ref": "t2:terra", "tenant_id": "tid"},
              "output": {"name": "V"}}
    verdict, _ = asyncio.run(score.t2_score_only(sample, {"shared_inputs": {"brand_rules": {
        "t2:terra": {"brand_name": "Terra"}}}}))
    assert seen["max_repairs"] == 0
    assert verdict["passed"] is False and verdict["qa_passed"] is False and verdict["quality_score"] == 7.5


class _FakeConn:
    def __init__(self, state):
        self.s = state

    async def fetchval(self, sql, *a):
        if "shared.job" in sql:
            return 0
        return self.s["spent"]

    async def fetch(self, sql, *a):
        return list(self.s["calls"]) if "llm_call_log" in sql else []


class _FakePool:
    def __init__(self, state):
        self.conn = _FakeConn(state)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    async def close(self):
        return None


def test_run_stops_at_the_cost_cap_and_reports_from_the_log(monkeypatch, tmp_path):
    import argparse
    import asyncio

    state = {"spent": 0.0, "calls": []}
    fx = {"fixture_version": "1.0.0", "seed": 729, "samples": [
        {"sample_id": f"s1-{i:03d}", "stratum": "s1", "kind": "s1", "input": {}, "label_source": "needs_panel",
         "label": common.needs_panel_label()} for i in range(1, 6)]}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fx))
    saved = {}

    async def fake_pool():
        return _FakePool(state)

    async def fake_one(sample, fixture, mode, pool):
        from shared.llm_client import call_log

        state["spent"] += 0.4
        state["calls"].append({"job_id": call_log.current_job_id(), "role": "writer", "provider": "bedrock-satellite",
                               "real_stage": "s1_generate", "n": 1, "usd": 0.4})
        return {"passed": True}, {"name": "x"}

    monkeypatch.setattr(score, "_pool", fake_pool)
    monkeypatch.setattr(score, "score_one", fake_one)
    monkeypatch.setattr(score, "SETTLE_S", 0)
    monkeypatch.setattr(evalpatch, "install", lambda run_id: None)
    monkeypatch.setattr(common, "save_output", lambda obj, key, **kw: saved.__setitem__(key, obj))
    args = argparse.Namespace(fixture=str(path), mode="regenerate", limit=None, strata=None, sample_ids=None,
                              max_cost_usd=1.0, concurrency=1, resume=None, resume_file=None,
                              allow_busy_queue=False, no_upload=True)
    result = asyncio.run(score.run(args))
    assert len(result["samples"]) == 2 and result["stopped"].startswith("cost_cap")
    assert result["totals"]["cost_usd_llm_call_log"] == pytest.approx(0.8)
    assert result["metrics"]["s1"]["writer_calls"] == 2 and result["metrics"]["s1"]["pass_rate"] == 1.0
    assert all(r["cost_usd"] == pytest.approx(0.4) for r in result["samples"])
    final = [k for k in saved if k.endswith("score-regenerate.json")]
    assert final and saved[final[0]]["final"] is True


def test_llm_call_log_has_one_insert_path():
    """evalpatch relabels the one INSERT every llm_call_log row goes through; a second INSERT would let eval
    rows land under real stages."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    hits = [str(p.relative_to(repo)) for root in ("api", "services", "shared", "worker")
            for p in (repo / root).rglob("*.py") if "INTO shared.llm_call_log" in p.read_text(encoding="utf-8")]
    assert hits == ["shared/llm_client/call_log.py"], hits


def test_run_turns_a_sample_cut_by_the_in_call_stop_into_an_error(monkeypatch, tmp_path):
    import argparse
    import asyncio

    state = {"spent": 0.0, "calls": []}
    fx = {"fixture_version": "1.0.0", "seed": 729, "samples": [
        {"sample_id": f"injs-{i:03d}", "stratum": "injection", "kind": "injection_s1", "input": {},
         "label_source": "synthetic", "label": common.make_label("pass", "synthetic"),
         "injection_spec": {"canary": "ZEBRA-42"}} for i in range(1, 4)]}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fx))

    async def fake_pool():
        return _FakePool(state)

    async def fake_one(sample, fixture, mode, pool):
        from shared.llm_client import call_log

        # the path swallowed the refused call and returned an empty "clean" output
        evalpatch.BUDGET.tripped = "cap $1.00"
        evalpatch.BUDGET.tripped_jobs.add(call_log.current_job_id())
        return {"passed": True}, {}

    monkeypatch.setattr(score, "_pool", fake_pool)
    monkeypatch.setattr(score, "score_one", fake_one)
    monkeypatch.setattr(score, "SETTLE_S", 0)
    monkeypatch.setattr(evalpatch, "install", lambda run_id: None)
    monkeypatch.setattr(common, "save_output", lambda obj, key, **kw: None)
    args = argparse.Namespace(fixture=str(path), mode="regenerate", limit=None, strata=None, sample_ids=None,
                              max_cost_usd=1.0, concurrency=1, resume=None, resume_file=None,
                              allow_busy_queue=False, no_upload=True)
    try:
        result = asyncio.run(score.run(args))
    finally:
        evalpatch.BUDGET.configure(None)
    assert len(result["samples"]) == 1 and result["stopped"].startswith("cost_cap")
    row = result["samples"][0]
    assert row["error"].startswith("CostCapReached")
    m = result["metrics"]["injection"]
    assert m["errors"] == 1 and m["pass_rate"] is None and "injection_defended_rate" not in m


def test_labels_pack_keeps_the_full_source():
    fx = _fixture()
    long_text = "Day 1 ride. " * 2000 + "Day 8 ends in Taipei."
    for s in fx["samples"]:
        if s["label_source"] == "needs_panel" and (s.get("input") or {}).get("tour") is not None:
            s["input"]["tour"] = {"itinerary": long_text}
    pack = labels.export_pack(fx)
    assert any(it["source"].endswith("Day 8 ends in Taipei.") for it in pack["items"])


# ── AA-925: degraded samples, INVALID strata, the eval stage ─────────────────────────────────────


def test_t9_score_only_holds_an_f9_outage_as_degraded():
    from scripts.aa729 import score

    ledger = [{"gate": "F9_cta_fact", "passed": False, "blocking": False, "degraded": "f9_judge_unavailable"}]
    block = score.degraded_block(score.t9_degraded(ledger))
    assert block == {"degraded": {"f9_judge_unavailable": 1}, "degraded_hard": ["f9_judge_unavailable"]}


def test_invalid_strata_are_per_stratum_and_skip_error_rows():
    from scripts.aa729 import score

    rows = [{"stratum": "s1", "sample_id": "s1-1", "verdict": {"degraded_hard": ["jev_unavailable"]}},
            {"stratum": "s1", "sample_id": "s1-2", "verdict": {"degraded_hard": []}},
            {"stratum": "t9_blog", "sample_id": "b-1", "verdict": {"degraded_hard": []}},
            {"stratum": "t2", "sample_id": "t2-1", "error": "TimeoutError", "verdict": {"degraded_hard": ["x"]}}]
    assert score.invalid_strata(rows) == {"s1": ["s1-1"]}


def test_report_refuses_deltas_for_invalid_strata_only():
    from scripts.aa729 import report

    def run(rid, s1_pass, invalid=None, flag_fix=0):
        samples = [{"sample_id": "s1-1", "stratum": "s1", "verdict": {"passed": s1_pass},
                    "calls_by_stage": {"s1_flag_fix": flag_fix}},
                   {"sample_id": "b-1", "stratum": "t9_blog", "verdict": {"passed": True}, "calls_by_stage": {}}]
        return {"schema": "aa729-score", "run_id": rid, "mode": "regenerate", "fixture_version": "1.1.0",
                "samples": samples, "invalid_strata": invalid or {},
                "metrics": {"s1": {"pass_rate": 1.0 if s1_pass else 0.0}, "t9_blog": {"pass_rate": 0.5},
                            "all": {"pass_rate": 0.5}}}

    lines = report.before_after(run("a", True, flag_fix=1), run("b", False, {"s1": ["s1-1"]}, flag_fix=3))
    text = "\n".join(lines)
    assert "INVALID strata" in text
    s1_row = next(ln for ln in lines if ln.startswith("| s1 |"))
    assert "INVALID" in s1_row
    blog_row = next(ln for ln in lines if ln.startswith("| t9_blog |"))
    assert "+0.0 pt" in blog_row
    assert "Fitter revert criterion" in text


def test_evalpatch_stage_is_parameterised_and_validated():
    import uuid

    from scripts.aa729 import evalpatch
    from shared.llm_client import call_log, decide

    run = str(uuid.uuid4())
    sql = evalpatch.relabel_sql(call_log._INSERT_SQL, run, "adhoc_aa925")
    assert "'adhoc_aa925'" in sql and "'adhoc_aa729'" not in sql
    assert "'adhoc_aa925:' || $1::text" in evalpatch.relabel_decision_sql(decide._LOG_SQL, "adhoc_aa925")
    assert "stage = 'adhoc_aa925'" in evalpatch.spend_sql("adhoc_aa925")
    with pytest.raises(ValueError):
        evalpatch.spend_sql("s1_generate'; --")
