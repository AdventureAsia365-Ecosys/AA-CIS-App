"""AA-919 (WM-17) — services/golden_set/derive.py on an INVENTED fixture (tests/unit/aa919_fake_golden.py).

Covers: writer / channel / kind / scope mapping (inj-t9 tenant from its host), the stratified 30% split (round half up,
deterministic for a seed, traps apart), the tiers (fixture tags, long_9d with NULL days = false, fact_dense top 30% of
a1 / t2 by fact density, post_edit), tenant brand packs inlined only on their own tenant's items and never kept at set
level, the refusals, the count report (no text), and the baseline rows (post_edit never a score_only control; score
files replace the fixture verdicts; their refusals).
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from services.golden_set import derive
from tests.unit.aa919_fake_golden import SLUG_A, TENANT_A, TENANT_B, fake_fixture

SHA = "b" * 64


def _build(fx=None, **kw):
    return derive.build(fx if fx is not None else fake_fixture(), name="zz919", source_uri="file:fake.json",
                        source_sha256=SHA, imported_by="pytest", **kw)


def _items(gs):
    return {i["sample_id"]: i for i in gs.items}


# ── mapping ─────────────────────────────────────────────────────────────────────────────────────────────────────


def test_writer_channel_kind_scope_mapping():
    it = _items(_build())
    assert (it["s1-001"]["writer"], it["s1-001"]["channel"], it["s1-001"]["scope"], it["s1-001"]["tenant_id"]) == \
        ("a1", None, "platform", None)
    assert (it["t2-001"]["writer"], it["t2-001"]["scope"], it["t2-001"]["tenant_id"]) == ("t2", "tenant", TENANT_A)
    assert (it["t9b-001"]["writer"], it["t9b-001"]["channel"]) == ("t9_blog", "blog")
    assert (it["t9l-001"]["writer"], it["t9l-001"]["channel"], it["t9l-001"]["tenant_id"]) == \
        ("t9_social", "linkedin", TENANT_B)
    assert (it["inj-s1-1"]["writer"], it["inj-s1-1"]["kind"], it["inj-s1-1"]["trap"]) == ("a1", "synthetic", True)
    # the injection on a LinkedIn host is a t9_social trap of the HOST's tenant (critic 11)
    assert (it["inj-t9-2"]["writer"], it["inj-t9-2"]["tenant_id"], it["inj-t9-2"]["split"]) == \
        ("t9_social", TENANT_B, "trap")
    assert {i["kind"] for i in it.values() if not i["trap"]} == {"real"}
    assert all(i["injection_spec"] for i in it.values() if i["trap"])


@pytest.mark.parametrize("pct,n,k", [("0.300", 44, 13), ("0.300", 6, 2), ("0.300", 5, 2), ("0.300", 7, 2),
                                     ("0.300", 3, 1), ("0.300", 1, 0), ("0.300", 50, 15), ("0.300", 10, 3)])
def test_round_half_up_is_decimal(pct, n, k):
    assert derive.round_half_up(Decimal(pct), n) == k          # 0.3 x 5 = 1.5 -> 2 (a float gives 1.4999…)


# ── split ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_split_is_stratified_30_percent_with_traps_apart():
    gs = _build()
    by = {}
    for i in gs.items:
        by.setdefault((i["writer"], "trap" if i["trap"] else i["label_verdict"]), []).append(i["split"])
    assert sorted(by[("a1", "fail")]).count("holdout") == 2          # 6 -> 1.8 -> 2
    assert sorted(by[("a1", "pass")]).count("holdout") == 1          # 2 -> 0.6 -> 1
    assert by[("t2", "fail")].count("holdout") == 1 and by[("t2", "pass")].count("holdout") == 0
    assert by[("t9_blog", "fail")].count("holdout") == 1 and by[("t9_blog", "pass")].count("holdout") == 1
    assert set(by[("a1", "trap")]) == {"trap"} and len(by[("a1", "trap")]) == 2
    rep = derive.count_report(gs)
    assert rep["by_writer_split"]["a1"] == {"holdout": 3, "trap": 2, "tune": 5}
    assert rep["traps"] == 4


def test_split_is_deterministic_for_a_seed_and_moves_with_it():
    a = {i["sample_id"]: i["split"] for i in _build().items}
    assert a == {i["sample_id"]: i["split"] for i in _build().items}
    rows = [{"sample_id": f"s1-{n:03d}", "writer": "a1", "label_verdict": "fail", "trap": False} for n in range(40)]
    one, other = derive.assign_splits(rows, 729), derive.assign_splits(rows, 730)
    assert sum(v == "holdout" for v in one.values()) == sum(v == "holdout" for v in other.values()) == 12
    assert one != other


# ── tiers ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_long_9d_null_days_is_not_long():
    it = _items(_build())
    assert "long_9d" in it["s1-001"]["tiers"] and "long_9d" in it["s1-002"]["tiers"]     # 15, 9
    assert "long_9d" not in it["s1-003"]["tiers"]                                        # 8
    assert it["s1-004"]["days"] is None and "long_9d" not in it["s1-004"]["tiers"]       # NULL
    assert "long_9d" not in it["t9b-001"]["tiers"]                                       # T9: no days


def test_fact_dense_top_30_percent_of_a1_and_t2_traps_excluded():
    gs = _build()
    it = _items(gs)
    a1 = [i for i in gs.items if i["writer"] == "a1" and "fact_dense" in i["tiers"]]
    assert len(a1) == 2 and {i["sample_id"] for i in a1} <= {"s1-001", "s1-002", "s1-005"}   # 8 -> 2.4 -> 2, dense
    assert [i["sample_id"] for i in gs.items if i["writer"] == "t2" and "fact_dense" in i["tiers"]] == ["t2-001"]
    assert not [i for i in gs.items if i["writer"].startswith("t9") and "fact_dense" in i["tiers"]]
    assert "fact_dense" not in it["inj-s1-1"]["tiers"]
    rule = gs.set_row["tier_rules"]["fact_dense"]["per_writer"]
    assert rule["a1"]["n"] == 8 and rule["a1"]["k"] == 2 and rule["t2"]["k"] == 1
    assert all(i["density"] >= rule["a1"]["cut"] for i in a1)


def test_fixture_tags_and_post_edit_tier_in_canonical_order():
    it = _items(_build())
    assert it["s1-001"]["tiers"] == ["cycle_bike", "long_9d", "long_18d", "fact_dense"] or \
        it["s1-001"]["tiers"] == ["cycle_bike", "long_9d", "long_18d"]
    assert it["t2-002"]["post_edit"] is True and "post_edit" in it["t2-002"]["tiers"]
    assert "post_edit" not in it["t2-001"]["tiers"]
    assert all(t in derive.TIERS for i in it.values() for t in i["tiers"])


# ── tenant brand packs ──────────────────────────────────────────────────────────────────────────────────────────


def test_tenant_pack_inlined_on_its_items_and_never_at_set_level():
    gs = _build()
    it = _items(gs)
    assert it["t2-001"]["input"]["brand_rules"]["default_cta"] == "Ask the pretend agency."
    assert "brand_rules" not in it["s1-001"]["input"]                                    # s1:* stays at set level
    assert set(gs.set_row["shared_inputs"]["brand_rules"]) == {"s1:default"}
    assert gs.set_row["meta"]["tenant_packs_inlined"] == [f"t2:{SLUG_A}"]
    assert gs.dropped_packs == ["t2:zz-unused-agency"]                                   # unused: not stored
    assert "zz-unused-agency" not in json.dumps(gs.set_row["shared_inputs"])
    assert it["t2-001"]["input_sha256"] == derive.sha256_json(it["t2-001"]["input"])


def test_a_pack_is_refused_on_another_tenants_item():
    fx = fake_fixture()
    t2 = next(s for s in fx["samples"] if s["sample_id"] == "t2-001")
    t2["refs"]["tenant_slug"] = "zz-other-agency"
    with pytest.raises(derive.FixtureError, match="only be inlined on its own tenant"):
        _build(fx)


# ── refusals ────────────────────────────────────────────────────────────────────────────────────────────────────


def _mutate(sample_id, fn):
    fx = fake_fixture()
    fn(next(s for s in fx["samples"] if s["sample_id"] == sample_id), fx)
    return fx


@pytest.mark.parametrize("sample_id,fn,match", [
    ("s1-001", lambda s, fx: s.update(kind="s2"), "unknown kind"),
    ("s1-001", lambda s, fx: s.update(tags=["long_7d"]), "unknown tags"),
    ("s1-001", lambda s, fx: s.update(output_post_edit="yes"), "must be a boolean"),
    ("s1-001", lambda s, fx: s.update(days=0), "not 1..60"),
    ("s1-001", lambda s, fx: s["refs"].update(tenant_id=TENANT_A), "a1 sample carries a tenant"),
    ("t2-001", lambda s, fx: s["refs"].pop("tenant_id"), "has no refs.tenant_id"),
    ("t2-001", lambda s, fx: s["input"].update(tenant_id=TENANT_B), "input.tenant_id differs"),
    ("t9b-001", lambda s, fx: s["input"]["context"].update(channel="linkedin"), "differs from input.context"),
    ("t9b-001", lambda s, fx: s["input"]["context"].update(tenant_id=TENANT_B), "context.tenant_id differs"),
    ("inj-t9-1", lambda s, fx: s["refs"].update(host_sample_id="t9b-999"), "is not in the fixture"),
    ("inj-t9-1", lambda s, fx: s["refs"].update(tenant_slug="zz-x"), "tenant_slug differs"),
    ("inj-s1-1", lambda s, fx: s.pop("injection_spec"), "without an injection_spec"),
    ("s1-002", lambda s, fx: s.update(sample_id="s1-001"), "duplicate sample_id"),
    ("s1-002", lambda s, fx: s.update(sample_id="S1 002"), "bad sample_id"),
    ("s1-001", lambda s, fx: s["label"].update(verdict="maybe"), "label.verdict"),
    ("s1-001", lambda s, fx: s.update(label_source="crowd"), "unknown label_source"),
    ("s1-001", lambda s, fx: s["refs"].update(tour_id={"nested": 1}), "is not an id"),
    ("s1-001", lambda s, fx: fx.update(schema="other"), "not a golden-set fixture"),
    ("s1-001", lambda s, fx: fx.update(fixture_version="1.1"), "is not x.y.z"),
])
def test_refusals(sample_id, fn, match):
    with pytest.raises(derive.FixtureError, match=match):
        _build(_mutate(sample_id, fn))


def test_count_report_holds_counts_not_text():
    rep = derive.count_report(_build())
    blob = json.dumps(rep)
    assert rep["items"] == 24 and rep["post_edit"] == 2 and rep["tenants"] == 2
    assert rep["scope"] == {"platform": 10, "tenant": 14}
    for phrase in ("pretend", "invented", "Invented", "made-up"):
        assert phrase not in blob


# ── baseline ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_fixture_baseline_one_run_per_writer_no_post_edit_control():
    gs = _build()
    b = derive.baseline(gs)
    assert set(b) == {"a1", "t2", "t9_blog", "t9_social"}
    assert {r["sample_id"] for r in b["t2"]["results"]} == {"t2-001", "t2-003", "t2-004"}       # t2-002 is post_edit
    assert b["t2"]["run"]["details"]["skipped_post_edit"] == 1
    assert b["t9_blog"]["run"]["details"]["skipped_post_edit"] == 1
    assert not [r for r in b["a1"]["results"] if r["sample_id"].startswith("inj")]           # traps: no verdict
    run = b["a1"]["run"]
    assert (run["run_kind"], run["verdict"], run["stop_reason"], run["cost_usd"]) == \
        ("baseline_import", "baseline", "completed", 0.0)
    assert run["prompt_version"] == "aa729-frozen@1.0.0" and run["tour_count"] == 8
    r = next(x for x in b["a1"]["results"] if x["sample_id"] == "s1-001")
    assert (r["arm"], r["mode"], r["verdict"], r["failure_codes"], r["output"]) == \
        ("control", "score_only", "fail", ["F_FAKE"], None)
    # the fixture stores pipeline_verdict.score as a string ("5.00"): the frozen score is kept, not dropped
    assert r["score"] == 5.0
    assert next(x for x in b["a1"]["results"] if x["verdict"] == "pass")["score"] == 7.5
    assert {x["score"] for x in b["t2"]["results"]} == {8.0}
    phases = {x["sample_id"]: x["phase"] for x in b["a1"]["results"]}
    it = _items(gs)
    assert all(phases[s] == it[s]["split"] for s in phases)


@pytest.mark.parametrize("value,want", [
    ("7.00", 7.0), (" 8.5 ", 8.5), (7, 7.0), (7.25, 7.25), (Decimal("6.50"), 6.5), ("1e1", 10.0),
    ("", None), ("seven", None), ("NaN", None), ("Infinity", None), (float("nan"), None), (float("inf"), None),
    (True, None), (None, None), ([7], None), ({"score": 7}, None),
])
def test_num_reads_numeric_strings_and_drops_the_rest(value, want):
    assert derive._num(value) == want


def _score_file(mode, rows, **kw):
    return dict(schema="aa729-score", run_id=f"run-{mode}", mode=mode, fixture_version="1.0.0", final=True,
                samples=rows, **kw)


def test_score_files_replace_fixture_verdicts_and_carry_regenerated_outputs():
    gs = _build()
    so = _score_file("score-only", [
        {"sample_id": "s1-001", "verdict": {"passed": True, "quality_score": "8.10"}, "cost_usd": "0.02"},
        {"sample_id": "t2-002", "verdict": {"passed": True}, "cost_usd": 0.01},                 # post_edit: skipped
        {"sample_id": "t9b-001", "verdict": {}, "error": "TimeoutError: invented", "cost_usd": 0.0}])
    rg = _score_file("regenerate", [
        {"sample_id": "t2-002", "verdict": {"passed": False, "failure_codes": ["F_X"]}, "cost_usd": 0.3,
         "output": {"name": "A regenerated invented name"}}])
    b = derive.baseline(gs, [("s3:scripts/out/so.json", so), ("s3:scripts/out/rg.json", rg)])
    assert [r["sample_id"] for r in b["a1"]["results"]] == ["s1-001"]
    assert b["a1"]["results"][0]["score"] == 8.1 and b["a1"]["results"][0]["verdict"] == "pass"
    t2 = b["t2"]["results"]
    assert [(r["sample_id"], r["mode"]) for r in t2] == [("t2-002", "regenerate")]          # post_edit: regen only
    assert t2[0]["output"] == {"name": "A regenerated invented name"}
    assert t2[0]["output_sha256"] == derive.sha256_json(t2[0]["output"])
    err = b["t9_blog"]["results"][0]
    assert (err["verdict"], err["details"]["error_kind"]) == ("error", "TimeoutError")
    assert "invented" not in json.dumps(err["details"])
    assert b["t2"]["run"]["details"]["source"] == "score_files"
    assert b["t2"]["run"]["details"]["source_cost_usd"] == 0.3 and b["t2"]["run"]["cost_usd"] == 0.0


@pytest.mark.parametrize("change,match", [
    (dict(schema="x"), "not an aa729 score file"),
    (dict(fixture_version="9.9.9"), "scored fixture"),
    (dict(final=False), "partial"),
    (dict(mode="judge"), "unknown mode"),
    (dict(samples=[{"sample_id": "zz-nope", "verdict": {}}]), "not in the fixture"),
])
def test_score_file_refusals(change, match):
    f = _score_file("score-only", [])
    f.update(change)
    with pytest.raises(derive.FixtureError, match=match):
        derive.baseline(_build(), [("k", f)])


def test_two_files_of_one_mode_are_refused():
    f = _score_file("score-only", [])
    with pytest.raises(derive.FixtureError, match="a second"):
        derive.baseline(_build(), [("a", f), ("b", copy.deepcopy(f))])
