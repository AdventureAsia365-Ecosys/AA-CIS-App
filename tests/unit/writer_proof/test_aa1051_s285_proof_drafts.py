"""AA-1051 S285 — the T9 / S1 / T2 pre-registrations re-drafted on the judge of the W5 null run (the pinned AA-1048
v7 claim instrument, advisory) and golden aa729@1.5.0 (T9), S1 and T2 re-drafted on aa729@1.7.0 (S285 lane l25: the
S1 and T2 GATE additions, opened by the null run's golden carry), the writer cost per object (the writer path's ledger
rows, every row but the eval judge's), and the registration preview of a draft's DRY plan (what registering it would
refuse). Ids and counts only (the golden id ranges are the public changelog's); no DB, no model call, nothing queued."""
from __future__ import annotations

import asyncio
import json

import pytest

from services.writer_proof import cost_report, decision, power
from services.writer_proof import null_carry as nc
from services.writer_proof import prereg as pr
from services.writer_proof import runner, splits
from services.writer_proof.instrument import ADVISORY, PINNED
from tests.unit.writer_proof import test_aa1051_runner as base
from tests.unit.writer_proof.test_aa1051_runner import _Pool

fakes = base.fakes          # the runner tests' fixture (faked R2 store / estimate / null gate)

NULL_ID = "w5-null-t9-blog-v7-g150"
DRAFTS = ("w5-t9", "w5-s1", "w5-t2")
# docs/calibration/aa729-golden-set-changelog.md: 1.5.0 holds s1-001..050, t2-001..020, t9b-001..034; 1.7.0 adds
# s1-051..072 (1.6.0) and t2-021..068 and no t9_blog item; plus the injection traps (inj-*), which never count as a
# GATE sample of a proof
ALL_IDS = {"a1": [f"s1-{i:03d}" for i in range(1, 73)], "t2": [f"t2-{i:03d}" for i in range(1, 69)],
           "t9_blog": [f"t9b-{i:03d}" for i in range(1, 35)]}
GOLDEN = {"w5-t9": "1.5.0", "w5-s1": "1.7.0", "w5-t2": "1.7.0"}
# S285 l25: the GATE ids each addendum added (the stratified S1 picks; the T2 slots on the 4 audit tenants)
NEW_GATE = {"w5-s1": ["s1-052", "s1-053", "s1-057", "s1-058", "s1-060", "s1-061", "s1-065", "s1-067", "s1-069",
                      "s1-072"],
            "w5-t2": ["t2-026", "t2-028", "t2-033", "t2-034", "t2-037", "t2-039", "t2-041", "t2-044", "t2-045",
                      "t2-052", "t2-062", "t2-063", "t2-064", "t2-067", "t2-068"]}
# the 12 t9_blog GATE items the null run drew on Dev (S284 plan, sample_resolved 12 of 12)
NULL_SAMPLE = ["t9b-006", "t9b-008", "t9b-013", "t9b-014", "t9b-019", "t9b-020", "t9b-024", "t9b-025", "t9b-028",
               "t9b-029", "t9b-033", "t9b-034"]
# tech lead, 19/11 (= the D12 caps); S285 C2: T2 raised from 5 to 6.5 USD with all 20 GATE samples (CIS 460)
CAPS = {"w5-t9": 15.0, "w5-s1": 8.0, "w5-t2": 6.5}
# GATE items whose stored OUTPUT is over the eval judge's evidence cap (eval_judge OUTPUT_CHARS): their control can
# never be judged (InputOverCap), so they could never pair; measured on the fixture's stored text before any run
OVER_EVIDENCE_CAP = {"s1-027"}


@pytest.mark.parametrize("pid", DRAFTS)
def test_each_draft_runs_on_the_null_runs_judge_and_golden_version(pid):
    pre, null = pr.load(pid), pr.load(NULL_ID)
    # a draft until the DRY plan passes; once registered (a later reviewed PR) `load` has checked its lock entry
    assert pr.problems(pre.doc) == [] and (not pre.registered or pr.read_lock()[pid] == pre.sha256)
    assert pre.doc["judge"] == null.doc["judge"] and pre.doc["golden_set"] == {"name": "aa729", "version": GOLDEN[pid]}
    assert pre.doc["judge"]["instrument"] in PINNED and pre.doc["judge"]["instrument"] in ADVISORY
    if GOLDEN[pid] == null.doc["golden_set"]["version"]:
        assert runner.same_instrument(null, pre)           # the null run's non_inferior verdict opens this proof
    else:
        # S285: a later version of the same set with the same judge pin: the golden carry rule may open it (rules a,
        # b here; rule c, the identical t9_blog GATE items, is read from the stored rows by the plan)
        assert not runner.same_instrument(null, pre) and nc.carry_problem(null, pre) is None
    assert pre.doc["cap_usd"] == CAPS[pid] == pr.workflow_cap(pre.workflow)
    assert pre.doc["release_id"] is None and pre.doc.get("sample") is None and pre.doc["noise_sample_ids"] == []


@pytest.mark.parametrize("pid", DRAFTS)
def test_each_draft_decides_on_the_null_runs_claim_scale(pid):
    pre, null = pr.load(pid), pr.load(NULL_ID)
    dec = pre.doc["decision"]
    assert dec == {**null.doc["decision"]}                 # non_inferior, fail_claims_per_item, margin 4.0
    primary = [m for m in pre.doc["metrics"] if m.get("primary")]
    assert [m["name"] for m in primary] == ["fail_claims_per_item"]
    pw = pre.doc["power"]
    assert pw["writer_sd"] is not None and pw["writer_sd"] >= null.doc["power"]["writer_sd"]
    assert pw["claims_per_item"] == power.DEFAULT_CLAIMS_PER_ITEM[pre.writer]


@pytest.mark.parametrize("pid", DRAFTS)
def test_each_sample_is_every_non_trap_gate_item_of_its_writer(pid):
    pre = pr.load(pid)
    ids = pre.doc["sample_ids"]
    want = [s for s in ALL_IDS[pre.writer] if splits.split_of(s) == "gate" and s not in OVER_EVIDENCE_CAP]
    assert ids == want and not any(s.startswith("inj-") for s in ids)
    assert splits.split_of("s1-027") == "gate"                            # excluded by the cap, not by its side


def test_the_t9_sample_is_the_null_runs_sample():
    assert pr.load("w5-t9").doc["sample_ids"] == NULL_SAMPLE


def test_gate_counts_match_the_changelog():
    assert {pid: len(pr.load(pid).doc["sample_ids"]) for pid in DRAFTS} == {"w5-t9": 12, "w5-s1": 25, "w5-t2": 20}


@pytest.mark.parametrize("pid", ["w5-s1", "w5-t2"])
def test_the_s285_additions_are_on_gate_and_meet_the_minimum_in_force(pid):
    pre = pr.load(pid)
    ids = pre.doc["sample_ids"]
    assert all(splits.split_of(s) == "gate" for s in NEW_GATE[pid])
    assert set(NEW_GATE[pid]) <= set(ids) and len(ids) == pr.min_items(pre.workflow)
    # the 1.5.0 sample is kept whole: the additions only append
    old = [s for s in ids if s not in NEW_GATE[pid]]
    assert all(int(s.split("-")[1]) <= (50 if pid == "w5-s1" else 20) for s in old)


@pytest.mark.parametrize("pid", ["w5-s1", "w5-t2"])
def test_s1_and_t2_are_registered_by_the_tech_lead_and_locked(pid):
    """S285 l29: registered once each DRY plan's only blocking line was the draft status (tech lead decisions C1-C3,
    labels E4). The lock entry is the file's sha, the run gate accepts the file, and the sample stays the full GATE
    list (test_each_sample_is_every_non_trap_gate_item_of_its_writer)."""
    pre = pr.load(pid)
    assert pre.registered and pre.doc["registered_by"] == "tech_lead" and pre.doc["registered_on"] == "2026-11-19"
    assert pr.read_lock()[pid] == pre.sha256 == pr.sha256_of(json.loads(pr.path_for(pid).read_text()))
    pr.require_registered(pre)                                             # no PreregRefused
    assert pr.registration_problems(pre.doc) == [] and pr.problems(pre.doc) == []
    assert pre.doc["cap_usd"] == CAPS[pid] and pre.doc.get("sample") is None


@pytest.mark.parametrize("pid", ["w5-s1", "w5-t2"])
def test_the_null_run_is_carried_to_the_redrafted_proof(monkeypatch, pid):
    """The registered null run (1.5.0) opens the 1.7.0 draft through the golden carry when its t9_blog GATE items read
    the same in both versions (faked stored rows of invented text; null_carry's own tests cover every difference)."""
    from tests.unit.writer_proof import test_aa1051_s285_null_carry as carry
    calls, load = carry._gate(monkeypatch, {NULL_ID: "non_inferior"}, [(carry._old(), carry._new())])
    out = carry._run([pr.load(NULL_ID)], pr.load(pid), load)
    assert out["passed_by"] == NULL_ID and out["rule"] == nc.CARRY_RULE and "blocking" not in out
    assert out["golden_carried_from"] == "1.5.0" and calls == [("1.5.0", "1.7.0")]


def test_writer_cost_targets_read_the_writer_share_not_the_judge():
    for pid in DRAFTS:
        names = {m["name"]: m for m in pr.load(pid).doc["metrics"]}
        assert "target" in names["writer_cost_per_object_usd"]
        assert "target" not in names["cost_per_object_usd"]                    # all-in: judge chain included
        assert "target" not in names.get("requests_per_object", {})            # about 40 judge calls a blog


def test_eval_judge_stages_match_the_judges_own_constants():
    from services.ground_truth import r1
    from services.writing_memory import config as wm_cfg
    from services.writing_memory import releases
    assert cost_report.EVAL_JUDGE_STAGES == {releases.JUDGE_STAGE, wm_cfg.EVAL_STAGE, r1.STAGE}


def test_writer_cost_counts_the_a1_graph_judge_but_not_the_eval_judge():
    """An a1-shaped object: the S1 rewrite, its own in-graph judge and brand audit (role judge), a source-facts call
    (role validate), then the eval's claim instrument and plain judge. The writer cost is everything but the last two;
    writer requests stay the writer-role calls."""
    t0 = _ledger_time()
    results = [{"result_id": "r1", "run_id": "run", "created_at": t0(100), "arm": "candidate", "mode": "regenerate"}]
    rows = [("s1_generate", "writer", 0.030), ("s1_judge", "judge", 0.012), ("s1_brand_audit", "judge", 0.004),
            ("s1_source_facts", "validate", 0.002), ("adhoc_aa1048", "judge", 0.050),
            ("writing_memory_eval", "judge", 0.006)]
    ledger = [{"id": str(i), "eval_run": "run", "created_at": t0(i), "cost_usd": usd, "role": role,
               "real_stage": stage} for i, (stage, role, usd) in enumerate(rows)]
    got = cost_report.attribute(ledger, results)["per_result"]["r1"]
    assert got == {"cost_usd": 0.104, "requests": 6, "writer_requests": 1, "writer_cost_usd": 0.048}
    # a row without a real stage counts as the writer path's (over-count, never under-count)
    assert cost_report.writer_path_row({"role": "judge"}) is True
    rep = cost_report.report([{"run_id": "run", "writer": "a1"}], ledger, results)
    assert rep["runs"][0]["writer_cost_per_object_usd"]["median"] == 0.048
    computed = decision.computed_metrics([{"result_id": "r1", "arm": "candidate", "mode": "regenerate",
                                           "verdict": "pass"}], cost_report.attribute(ledger, results)["per_result"])
    assert computed["writer_cost_per_object_usd"] == 0.048


def _ledger_time():
    from datetime import datetime, timedelta, timezone
    base_t = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    return lambda s: base_t + timedelta(seconds=s)


def test_t9_targets_from_a_null_run_shaped_result():
    """The S284 null run's arm B, as numbers: a claim judge flags most first drafts (yield 0.083)."""
    doc = pr.load("w5-t9").doc
    computed = {"first_pass_yield": 0.083333, "writer_requests_per_object": 1.0, "writer_cost_per_object_usd": 0.053,
                "cost_per_object_usd": 0.070436, "requests_per_object": 38.5, "hard_stops": 0.0}
    got = {t["metric"]: t["status"] for t in decision.targets(doc, computed)}
    assert got == {"first_pass_yield": "missed", "writer_requests_per_object": "met",
                   "writer_cost_per_object_usd": "met", "hard_stops": "met", "contract_violations": "pending",
                   "unsupported_trip_promises_human_read_10": "pending"}


# ── the registration preview ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pid", DRAFTS)
def test_registration_preview_of_the_shipped_drafts_is_clean(pid):
    # S285 l25: 1.7.0 brings S1 to 25 and T2 to 20 GATE items, the minimums in force
    assert pr.registration_problems(pr.load(pid).doc) == []


@pytest.mark.parametrize("pid,keep,needle", [
    ("w5-s1", 15, "a registered s1 proof needs >= 25 sample ids, has 15"),
    ("w5-t2", 5, "a registered t2 proof needs >= 20 sample ids, has 5"),
])
def test_registration_preview_names_the_sample_shortfall(pid, keep, needle):
    d = json.loads(json.dumps(pr.load(pid).doc))
    d.update(status="draft", registered_by=None, registered_on=None)        # S285 l29: both files are registered now
    d["sample_ids"] = d["sample_ids"][:keep]                               # the 1.5.0-sized draft
    assert pr.registration_problems(d) == [needle]


def test_registration_preview_of_a_registered_file_or_a_non_object_is_empty():
    assert pr.registration_problems(pr.load(NULL_ID).doc) == []
    assert pr.registration_problems("not a doc") == []                     # type: ignore[arg-type]


def test_registration_preview_keeps_a_named_registrant_and_flags_a_missing_writer_sd():
    d = json.loads(json.dumps(pr.load("w5-t9").doc))
    d.update(status="draft", registered_by=None, registered_on=None)        # stays true after w5-t9 is registered
    d["power"]["writer_sd"] = None
    got = pr.registration_problems(d)
    assert len(got) == 1 and "states power.writer_sd" in got[0]
    assert d["status"] == "draft" and d["registered_by"] is None            # the draft itself is not changed


def test_a_drafts_plan_lists_what_registration_would_refuse(fakes):
    fakes["version"] = "1.7.0"
    d = json.loads(json.dumps(pr.load("w5-s1").doc))
    d.update(status="draft", registered_by=None, registered_on=None)        # S285 l29: w5-s1 is registered now
    d["sample_ids"] = d["sample_ids"][:15]
    out = asyncio.run(runner.plan(_Pool(), pr.from_doc(d)))
    assert out["registration_problems"] == ["a registered s1 proof needs >= 25 sample ids, has 15"]
    assert "registration would refuse: a registered s1 proof needs >= 25 sample ids, has 15" in out["blocking"]
    assert out["samples"] == 15 and not out["ready_for_live"]
    out = asyncio.run(runner.plan(_Pool(), pr.load("w5-s1")))
    assert out["registration_problems"] == [] and out["samples"] == 25 and out["golden_set"] == "aa729@1.7.0"
    assert not any(b.startswith(("registration", "pre-registered on")) for b in out["blocking"])
    fakes["version"] = "1.5.0"
    out = asyncio.run(runner.plan(_Pool(), pr.load("w5-t9")))
    assert out["registration_problems"] == [] and not any(b.startswith("registration") for b in out["blocking"])


# ── an item the claim judge cannot read ─────────────────────────────────────────────────────────────────────────

def test_a_sample_item_without_a_judgeable_control_output_blocks_the_plan(fakes, monkeypatch):
    fakes["version"] = "1.5.0"

    async def instrument_check(pool, *, instrument, writer, outputs, arms):
        return {"judge": {"instrument": instrument, "call": {"usd": 0.07, "outputs": 11}},
                "arms": {a: {"usd_max": 1.0, "calls_missing": False} for a in arms}, "blocking": []}
    monkeypatch.setattr(runner.price, "instrument_check", instrument_check)
    out = asyncio.run(runner.plan(_Pool(), pr.load("w5-t9")))
    assert any(b.startswith("1 of the 12 sample items have no judgeable control output") for b in out["blocking"])


@pytest.mark.parametrize("priced,judge,want", [(12, None, []), (0, None, []), (None, None, []),
                                              (11, {"model_key": "zz", "connection": "zz-c"}, [])])
def test_unjudgeable_is_silent_when_every_item_is_priced_or_the_judge_is_plain(priced, judge, want):
    pre = pr.load("w5-t9")
    if judge is not None:
        pre = pr.Prereg(doc={**pre.doc, "judge": judge}, sha256=pre.sha256)
    assert runner._unjudgeable(pre, {"judge": {"call": {"outputs": priced}}}) == want
