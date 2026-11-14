"""AA-1051 (S280) — the gaps closed in the W5 $0 code: the pinned judge priced from shared.llm_model_catalog (price.py
and the runner's gate on the larger estimate), the null-run-first gate, the campaign DRY RUN, the registered lock, and
R2's call counts. No DB (faked reads), no model call, nothing queued. Invented ids and prices only."""
from __future__ import annotations

import asyncio
import json

import pytest

from services.writer_proof import prereg as pr
from services.writer_proof import price, runner
from services.writing_memory import config as cfg
from services.writing_memory import r2
from tests.unit.writer_proof import test_aa1051_runner as base
from tests.unit.writer_proof.test_aa1051_runner import CATALOG, DEPLOYMENT, TOKEN_ROWS, _Pool, _pre, _r1_report, _t9

fakes = base.fakes          # the runner tests' fixture (faked R2 store / estimate / null gate)

PRICEY = {**CATALOG, "model_key": "zz-pricey", "price_in_per_mtok": 3.30, "price_out_per_mtok": 16.50}


# ── price.py (pure) ──────────────────────────────────────────────────────────────────────────────────────────────

def test_token_profile_needs_enough_rows_and_counts_cache_reads_as_input():
    rows = [{"tokens_in": 1000 + i, "tokens_out": 100 + i, "cache_read_tokens": 500} for i in range(10)]
    prof = price.token_profile(rows, min_samples=5)
    assert prof["samples"] == 10 and prof["in_p50"] == 1504.5 and prof["in_p90"] >= prof["in_p50"]
    assert prof["out_p90"] <= 109 and prof["out_p90"] >= prof["out_p50"]
    assert price.token_profile(rows[:4], min_samples=5) is None
    bad = [{"tokens_in": None, "tokens_out": 3}, {"tokens_in": float("nan"), "tokens_out": 1}, {"tokens_in": -1,
                                                                                                 "tokens_out": 1}]
    assert price.token_profile(bad, min_samples=1) is None


@pytest.mark.parametrize("cat,dep,conn,needle", [
    (None, None, "c", "no row in shared.llm_model_catalog"),
    ({**CATALOG, "price_out_per_mtok": None}, DEPLOYMENT, "c", "no input / output price"),
    ({**CATALOG, "enabled": False}, DEPLOYMENT, "c", "disabled in the catalog"),
    ({**CATALOG, "blocked_reason": "account refuses it"}, DEPLOYMENT, "c", "blocked in the catalog"),
    (CATALOG, None, "c", "no deployment of the model on its pinned connection"),
    (CATALOG, {**DEPLOYMENT, "enabled": False}, "c", "pinned deployment is disabled"),
])
def test_catalog_problems_refuse_what_cannot_be_priced_or_answered(cat, dep, conn, needle):
    assert any(needle in p for p in price.catalog_problems("m", conn, cat, dep))


def test_catalog_problems_accept_a_priced_enabled_judge_with_no_pinned_connection():
    assert price.catalog_problems("m", None, CATALOG, None) == []
    assert price.catalog_problems("m", "c", CATALOG, DEPLOYMENT) == []


def test_call_quote_uses_the_catalog_and_the_deployment_override():
    usd, basis = price.call_quote("gpt-6-luna-openai", CATALOG, None, 10_000, 1_000)
    assert basis == "catalog" and usd == pytest.approx((10_000 * 0.10 + 1_000 * 0.50) / 1e6)
    over = {**DEPLOYMENT, "price_out_override": 1.0}
    usd2, basis2 = price.call_quote("gpt-6-luna-openai", CATALOG, over, 10_000, 1_000)
    assert basis2 == "override" and usd2 == pytest.approx((10_000 * 0.10 + 1_000 * 1.0) / 1e6)


def test_judge_call_from_the_profile_and_the_upper_bound_without_one():
    prof = price.token_profile(TOKEN_ROWS, min_samples=5)
    led = price.judge_call("gpt-6-luna-openai", CATALOG, DEPLOYMENT, prof, margin=1.3)
    assert led["source"] == "ledger_p90" and led["tokens_in"] == 11_700 and led["tokens_out"] == 1_170
    bound = price.judge_call("gpt-6-luna-openai", CATALOG, DEPLOYMENT, None, margin=1.3)
    assert bound["source"] == "bound" and bound["usd"] > led["usd"]
    from services.writing_memory import eval_judge as ej
    assert bound["tokens_in"] * price.CHARS_PER_TOKEN >= ej.SOURCE_CHARS + ej.OUTPUT_CHARS
    assert bound["tokens_out"] >= ej.MAX_TOKENS


def test_arm_estimate_is_the_larger_of_r2_and_the_catalog_price():
    est = {"estimated_usd_max": 2.0, "calls": {"judge": 30, "writer_items": 12}, "item_estimate": {"item_usd": 0.1}}
    cheap = price.arm_estimate(est, 0.001)
    assert cheap["price_usd_max"] == 1.23 and cheap["usd_max"] == 2.0
    dear = price.arm_estimate(est, 0.1)
    assert dear["price_usd_max"] == 4.2 and dear["usd_max"] == 4.2
    missing = price.arm_estimate({"estimated_usd_max": 2.0}, 0.1)
    assert missing["calls_missing"] and missing["usd_max"] == 2.0


# ── R2's call counts (the numbers the catalog check re-prices) ───────────────────────────────────────────────────

def test_r2_estimate_reports_the_call_counts_behind_its_figure(monkeypatch):
    from tests.unit.test_aa900_w0_eval_interim import FakeStore, _estimate_on, golden
    est = _estimate_on(FakeStore(golden(), cost_samples=[0.134] * 10), monkeypatch, r2.R2Config(writer="a1"))
    calls = est["calls"]
    assert calls["writer_items"] == sum(est["plan"].values()) > 0
    assert calls["judge"] == est["calibration_judged_items"] + 2 * calls["writer_items"]
    rebuilt = calls["judge"] * cfg.JUDGE_CALL_ESTIMATE_USD + calls["writer_items"] * est["item_estimate"]["item_usd"]
    assert est["estimated_usd_max"] == pytest.approx(rebuilt, abs=1e-3)
    cal = _estimate_on(FakeStore(golden(), cost_samples=[0.134] * 10), monkeypatch,
                       r2.R2Config(writer="a1", calibrate_only=True))
    assert cal["calls"]["writer_items"] == 0 and cal["calls"]["judge"] == cal["calibration_judged_items"]


# ── the runner's plan with the catalog check ─────────────────────────────────────────────────────────────────────

def test_plan_reports_the_catalog_price_and_keeps_r2_figure_when_it_is_higher(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(), _pre(), r1_report=_r1_report(tmp_path)))
    assert out["ready_for_live"], out["blocking"]
    pc = out["price_check"]
    assert pc["judge"]["call"]["source"] == "ledger_p90" and pc["judge"]["call"]["pricing"] == "catalog"
    assert pc["arms"]["B"]["judge_calls"] == 30 and pc["arms"]["B"]["writer_items"] == 12
    assert out["r2_estimated_usd_max"] == 2.0 and out["estimated_usd_max"] == 2.0      # cheap judge: R2 stands


def test_a_pricier_pinned_judge_raises_the_estimate_and_hits_the_arm_cap(fakes, tmp_path):
    pre = _pre(judge={"model_key": "zz-pricey", "connection": "zz-conn"})
    pool = _Pool(catalog={"zz-pricey": PRICEY}, deployment={("zz-pricey", "zz-conn"): DEPLOYMENT},
                 token_rows=[{"tokens_in": 20_000, "tokens_out": 6_000, "cache_read_tokens": 0}] * 20)
    out = asyncio.run(runner.plan(pool, pre, r1_report=_r1_report(tmp_path)))
    jc = out["price_check"]["judge"]["call"]["usd"]
    assert jc == pytest.approx((26_000 * 3.30 + 7_800 * 16.50) / 1e6, rel=1e-4)
    assert out["estimated_usd_max"] == pytest.approx(30 * jc + 12 * 0.1, abs=1e-3)
    assert out["estimated_usd_max"] > out["r2_estimated_usd_max"]
    assert any("B: estimated" in b and "its run cap 5.0" in b for b in out["blocking"])
    assert any("the plan's estimate" in b and "is above the cap 5.0" in b for b in out["blocking"])


def test_plan_blocks_a_judge_the_catalog_cannot_price_or_a_missing_pinned_deployment(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(catalog={}), _pre(), r1_report=_r1_report(tmp_path)))
    assert any("no row in shared.llm_model_catalog" in b for b in out["blocking"]) and not out["ready_for_live"]
    out = asyncio.run(runner.plan(_Pool(deployment={}), _pre(), r1_report=_r1_report(tmp_path)))
    assert any("no deployment of the model on its pinned connection" in b for b in out["blocking"])


def test_plan_blocks_when_r2_gives_no_call_counts(fakes, tmp_path):
    fakes["calls"] = None
    out = asyncio.run(runner.plan(_Pool(), _pre(), r1_report=_r1_report(tmp_path)))
    assert any("no call counts" in b for b in out["blocking"])


def test_plan_without_a_judge_profile_reserves_the_upper_bound(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(token_rows=[]), _pre(), r1_report=_r1_report(tmp_path)))
    assert out["price_check"]["judge"]["call"]["source"] == "bound"
    assert out["price_check"]["judge"]["profile"] is None


# ── null run first ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_writer_proof_is_blocked_until_a_null_run_with_the_same_judge_is_non_inferior(fakes, tmp_path, monkeypatch):
    fakes["null_gate"] = "real"
    monkeypatch.setattr(pr, "registered_files", lambda workflow=None: [])
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path)))
    assert out["null_run"]["required"] and out["null_run"]["passed_by"] is None
    assert any("run the null run first" in b for b in out["blocking"])

    null_ok, null_other = _pre(), _pre(judge={"model_key": "zz-other", "connection": "zz-c"})
    verdicts = {null_ok.sha256: "non_inferior", null_other.sha256: "non_inferior"}

    async def report(pool, n):
        return {"ok": True, "decision": {"verdict": verdicts[n.sha256]}}
    monkeypatch.setattr(runner, "report", report)
    monkeypatch.setattr(pr, "registered_files", lambda workflow=None: [null_other])
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path)))
    assert any("run the null run first" in b for b in out["blocking"])              # another judge does not count
    monkeypatch.setattr(pr, "registered_files", lambda workflow=None: [null_other, null_ok])
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path)))
    assert out["null_run"]["passed_by"] == null_ok.id and not any("null run first" in b for b in out["blocking"])
    verdicts[null_ok.sha256] = "inferior"
    out = asyncio.run(runner.plan(_Pool(), _t9(), r1_report=_r1_report(tmp_path)))
    assert any("run the null run first" in b for b in out["blocking"])


def test_the_null_run_itself_needs_no_null_run(fakes, tmp_path):
    out = asyncio.run(runner.plan(_Pool(), _pre(), r1_report=_r1_report(tmp_path)))
    assert out["null_run"] == {"required": False}


def test_same_instrument_compares_judge_and_golden_version():
    a = _pre()
    assert runner.same_instrument(a, _t9())
    assert not runner.same_instrument(a, _t9(golden_set={"name": "aa729", "version": "1.5.0"}))
    assert not runner.same_instrument(a, _t9(judge={"model_key": "gpt-6-luna-openai", "connection": "zz"}))


# ── the campaign DRY RUN ─────────────────────────────────────────────────────────────────────────────────────────

def test_campaign_plans_every_proof_in_the_d12_order_and_sums_the_estimates(fakes):
    out = asyncio.run(runner.campaign(_Pool()))
    assert out["order"] == list(runner.CAMPAIGN_ORDER) and [r["prereg"] for r in out["proofs"]] == out["order"]
    assert all(not r["ready_for_live"] for r in out["proofs"])                       # the shipped files are drafts
    assert all(r["stage"] == cfg.W5_STAGE for r in out["proofs"])
    assert out["d12_total_cap_usd"] == pr.total_cap() == 33.0 and out["fits_d12_total"]
    assert not fakes["estimates"]                                                   # drafts: nothing estimated


def test_campaign_reports_a_file_that_does_not_load(fakes):
    out = asyncio.run(runner.campaign(_Pool(), ids=("w5-nothing-here",)))
    assert out["proofs"][0]["ok"] is False and "missing" in out["proofs"][0]["refused"]


# ── immutable once registered ────────────────────────────────────────────────────────────────────────────────────

def test_a_registered_file_must_match_the_lock():
    pre = _pre()
    assert "not in registered.lock.json" in pr.registered_lock_problem(pre, {})
    assert "edited after it was registered" in pr.registered_lock_problem(pre, {pre.id: "e" * 64})
    assert pr.registered_lock_problem(pre, {pre.id: pre.sha256}) is None
    assert pr.registered_lock_problem(pr.load("w5-t9"), {}) is None                 # a draft is not locked


def test_load_refuses_a_registered_file_outside_the_lock(tmp_path, monkeypatch):
    pre = _pre()
    f = tmp_path / f"{pre.id}.json"
    f.write_text(json.dumps(pre.doc))
    lock = tmp_path / "registered.lock.json"
    monkeypatch.setattr(pr, "LOCK_FILE", lock)
    with pytest.raises(pr.PreregRefused, match="not in registered.lock.json"):
        pr.load(str(f))
    lock.write_text(json.dumps({"registered": {pre.id: pre.sha256}}))
    assert pr.load(str(f)).sha256 == pre.sha256
    lock.write_text(json.dumps({"registered": ["not", "a", "map"]}))
    with pytest.raises(pr.PreregRefused, match="must be"):
        pr.load(str(f))


def test_the_shipped_lock_and_the_registered_files_agree():
    """The CI guard: every registered file is in the lock with its sha, and no lock entry lost its file."""
    lock = pr.read_lock()
    files = {}
    for f in sorted(pr.PREREG_DIR.glob("*.json")):
        if f.name == pr.LOCK_FILE.name:
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        if doc.get("status") == "registered":
            files[doc["id"]] = pr.sha256_of(doc)
    assert {k: lock.get(k) for k in files} == files
    assert set(lock) <= set(files), f"lock entries without their registered file: {sorted(set(lock) - set(files))}"


# ── the D12 caps and minimum samples are settings (CIS 419) ─────────────────────────────────────────────────────

def test_caps_and_minimum_samples_come_from_the_settings_registry(monkeypatch):
    from shared import settings
    defaults = {"null": (5.0, 12), "t9": (15.0, 12), "s1": (8.0, 25), "t2": (5.0, 20)}
    assert {w: (pr.workflow_cap(w), pr.min_items(w)) for w in pr.WORKFLOWS} == defaults
    for w, spec in pr.WORKFLOWS.items():
        assert settings.definition(spec["cap_setting"]).default_value == defaults[w][0]
        assert settings.definition(spec["min_items_setting"]).default_value == defaults[w][1]
    doc = dict(_pre().doc)                          # valid under the defaults
    real = settings.get
    lowered = {"cis.spend.w5_null_run_cap_usd": 3.0, "cis.quality.w5_null_run_min_items": 20,
               "cis.spend.w5_total_cap_usd": 10.0}
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: lowered.get(key, real(key, tenant_id)))
    p = pr.problems(doc)
    assert any("cap_usd must be in (0, 3.0]" in x for x in p) and any("needs >= 20 sample ids" in x for x in p)
    from services.writer_proof import spend
    out = spend.refusals(workflow="t9", prereg_cap=15.0, cap_usd=12.0, confirm=spend.CONFIRM_TOKEN,
                         spent_workflow=0.0, spent_total=0.0, estimate_usd=1.0)
    assert any("passes the D12 total 10.0" in x for x in out)
