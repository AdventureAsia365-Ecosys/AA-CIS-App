"""AA-919 WM-19 — the R2 runner end to end on an in-memory store, a fake writer and a fake judge (no DB, no model).

Done-when tests: controls are never regenerated outside reverify; the cost cap is enforced; a seeded failing candidate
stops at tune. Invented text only (public repo)."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import r2
from shared.llm_client import call_log

SET_ID = "5e700000-0000-4000-8000-000000000001"
BASELINE_RUN = "ba5e0000-0000-4000-8000-000000000001"
TENANT_A = "0a919a00-0000-4000-8000-00000000000a"
TENANT_B = "0a919b00-0000-4000-8000-00000000000b"


def _item(i: int, split: str, *, label="pass", trap=False, post_edit=False, tiers=(), writer="a1", tenant=None):
    tour = {"name": f"Invented loop {i}", "summary": f"Pretend walkers cross invented meadow {i}.",
            "description": "Nothing real happens on this fictional route."}
    return {"item_id": str(uuid.UUID(int=1000 + i)), "sample_id": f"s-{i:03d}", "writer": writer, "channel": None,
            "scope": "tenant" if tenant else "platform", "tenant_id": tenant, "split": split, "trap": trap,
            "tiers": list(tiers), "input": {"tour": tour, "brand_rules_ref": "s1:default", "tenant_id": tenant,
                                            "context": {"channel": "linkedin", "atom_text": tour["summary"],
                                                        "tenant_id": tenant}},
            "control_output": {"name": f"Rewritten loop {i}", "summary": "A calm invented rewrite."},
            "post_edit": post_edit, "label_verdict": label,
            "injection_spec": {"canary": "ZZCANARY"} if trap else None}


def golden(n_tune=10, n_hold=4, n_trap=2, labels_fail=0):
    items = [_item(i, "trap", label="fail", trap=True) for i in range(n_trap)]
    items += [_item(100 + i, "tune", label="fail" if i < labels_fail else "pass") for i in range(n_tune)]
    items += [_item(200 + i, "holdout") for i in range(n_hold)]
    return items


class FakeStore:
    def __init__(self, items, *, release=None, active=None, spend_per_writer_call=0.0):
        self._items = items
        self.release = release
        self.active = active or {"platform": {"release_id": "r0", "release_no": 0, "content_class": "empty",
                                              "rule_ids": [], "eval_run_id": BASELINE_RUN, "scope": "platform"},
                                 "tenant": None}
        self.results: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.spend = 0.0
        self.spend_per_writer_call = spend_per_writer_call

    async def get_release(self, release_id):
        return self.release if self.release and self.release["release_id"] == release_id else None

    async def active_parts(self, writer, tenant_id):
        return self.active

    async def golden_set(self):
        return {"set_id": SET_ID, "name": "aa729", "version": "1.1.0", "shared_inputs": {"brand_rules": {}}}

    async def items(self, set_id, writer, tenant_id):
        return [i for i in self._items if i["writer"] == writer and (tenant_id is None or i["tenant_id"] == tenant_id)]

    async def tenant_packs(self, set_id):
        return {TENANT_B: {"brand_name": "Zzother Pretend Tours"}}

    async def baseline_run(self, set_id, writer):
        return BASELINE_RUN

    async def control_sources(self, run_id, item_ids):
        assert run_id == BASELINE_RUN
        return {i["item_id"]: {"result_id": f"src-{i['item_id']}", "mode": "score_only", "arm": "control",
                               "output": None}
                for i in self._items if i["item_id"] in item_ids and not i["trap"]}

    async def rule_texts(self, rule_ids):
        return []

    async def fingerprint(self, writer):
        return {"gate_taxonomy": "qtax-v1", "golden_set": "aa729@1.1.0"}

    async def create_run(self, row):
        run_id = str(uuid.uuid4())
        self.runs[run_id] = dict(row)
        return run_id

    async def insert_result(self, row):
        self.results.append(dict(row))
        return str(uuid.uuid4())

    async def finish_run(self, run_id, row):
        self.runs[run_id].update(row, finished=True)

    async def run_spend(self, run_id, since):
        return round(self.spend, 6)

    async def month_spend(self):
        return 0.0


class FakeWriter:
    """Returns a clean output; `fail_ids` -> a text the fake judge fails; records the eval label it ran under."""

    def __init__(self, store, *, fail_ids=(), artefact_ids=(), raise_ids=(), leak_ids=()):
        self.store = store
        self.calls: list[str] = []
        self.labels: list = []
        self.fail_ids, self.artefact_ids, self.raise_ids, self.leak_ids = (set(fail_ids), set(artefact_ids),
                                                                           set(raise_ids), set(leak_ids))

    async def __call__(self, writer, inp, *, view, shared_inputs):
        self.labels.append(call_log.eval_label())
        name = inp["tour"]["name"]
        self.calls.append(name)
        self.store.spend += self.store.spend_per_writer_call
        if name in self.raise_ids:
            raise RuntimeError("writer down")
        text = "A calm invented rewrite."
        if name in self.fail_ids:
            text += " BADFACT"
        if name in self.artefact_ids:
            text += " F9_cta_fact"
        if name in self.leak_ids:
            text += " Book with Zzother Pretend Tours."
        return {"name": name, "summary": text}


def make_judge(*, wrong_on=()):
    """Fails a text holding BADFACT or the seeded helicopter claim (fact.inclusion); `wrong_on` = control item ids
    the judge gets wrong (to lower kappa)."""
    calls = []

    async def judge(pool, *, writer, source_text, output_text, run_id, item_id, arm):
        calls.append((arm, item_id, call_log.eval_label()))
        bad = "BADFACT" in output_text or "helicopter" in output_text
        verdict = "fail" if bad else "pass"
        if arm == "control" and item_id in wrong_on:
            verdict = "fail" if verdict == "pass" else "pass"
        return SimpleNamespace(verdict=verdict, score=3.0 if verdict == "fail" else 8.0,
                               failure_codes=["fact.inclusion"] if bad else [], model="fake-judge",
                               judge_version="v-test", cost_usd=0.0)
    judge.calls = calls
    return judge


async def _flush():
    return None


def run(store, conf, writer=None, judge=None):
    writer = writer or FakeWriter(store)
    judge = judge or make_judge()
    out = asyncio.run(r2.run_r2(store, conf, writer_fn=writer, judge_fn=judge, flush=_flush))
    return out, writer, judge


def _rows(store, **kw):
    return [r for r in store.results if all(r[k] == v for k, v in kw.items())]


# ── the happy path ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_clean_candidate_passes_with_a_calibrated_judge():
    store = FakeStore(golden(labels_fail=0))
    out, writer, judge = run(store, r2.R2Config(writer="a1"))
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("pass", "completed", "holdout")
    assert out.judge_counts and out.judge_kappa == 1.0
    run_row = store.runs[out.run_id]
    assert run_row["verdict"] == "pass" and run_row["finished"] and run_row["judge_counts"] is True
    # every non-trap item: one control rejudge (scored_result_id = the stored baseline row) + one candidate regenerate
    controls = _rows(store, arm="control")
    assert len(controls) == 14 and all(r["mode"] == "rejudge" and r["scored_result_id"].startswith("src-")
                                       and r["output"] is None for r in controls)
    cands = _rows(store, arm="candidate")
    assert len(cands) == 16 and all(r["mode"] == "regenerate" and r["output"] for r in cands)
    assert {r["phase"] for r in cands} == {"probe", "tune", "holdout"}
    # every model call ran under the eval label of this run
    assert set(writer.labels) == {(cfg.EVAL_STAGE, out.run_id)}
    assert {lab for *_, lab in judge.calls} == {(cfg.EVAL_STAGE, out.run_id)}
    assert call_log.eval_label() is None


def test_controls_are_never_regenerated_outside_reverify():
    store = FakeStore(golden())
    out, writer, _ = run(store, r2.R2Config(writer="a1"))
    assert not _rows(store, arm="control", mode="regenerate")
    assert len(writer.calls) == len(_rows(store, arm="candidate"))      # the writer wrote candidates only


def test_reverify_regenerates_controls_with_the_active_view():
    store = FakeStore(golden())
    out, writer, _ = run(store, r2.R2Config(writer="a1", run_kind="reverify"))
    regen = _rows(store, arm="control", mode="regenerate")
    assert regen and not _rows(store, arm="control", mode="rejudge")
    assert len(writer.calls) == len(regen) + len(_rows(store, arm="candidate"))


# ── money ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_cap_is_enforced_from_the_logged_spend():
    store = FakeStore(golden(), spend_per_writer_call=0.2)
    out, writer, _ = run(store, r2.R2Config(writer="a1", cap_usd=1.0))
    assert (out.verdict, out.stop_reason) == ("invalid", "budget_cap")
    # the reservation (item estimate) stops before the cap is passed: never more than the cap is logged
    assert store.spend <= 1.0 and len(writer.calls) <= 5
    assert store.runs[out.run_id]["cost_cap_usd"] == 1.0


def test_cap_over_ten_dollars_is_refused():
    with pytest.raises(r2.R2Refused):
        run(FakeStore(golden()), r2.R2Config(writer="a1", cap_usd=10.5))
    with pytest.raises(r2.R2Refused):
        run(FakeStore(golden()), r2.R2Config(writer="a1", cap_usd=0))


def test_month_cap_stops_the_run():
    store = FakeStore(golden())

    async def month():
        return cfg.MONTH_CAP_USD - 0.01
    store.month_spend = month
    out, writer, _ = run(store, r2.R2Config(writer="a1"))
    assert out.stop_reason == "budget_cap" and writer.calls == []


# ── stops ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_seeded_failing_candidate_stops_at_tune_before_holdout():
    store = FakeStore(golden())
    out, writer, _ = run(store, r2.R2Config(writer="a1", seed_fault="qa_trace"))
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("fail", "hard_stop", "tune")
    assert writer.calls == []                                 # seeded: no writer call, judge only
    cands = _rows(store, arm="candidate")
    assert len(cands) == 1 and cands[0]["hard_stop"] and cands[0]["phase"] == "tune"
    assert not _rows(store, phase="holdout", arm="candidate")


def test_seeded_unsourced_claim_is_hard_stop_1_with_a_calibrated_judge():
    store = FakeStore(golden())
    out, _, _ = run(store, r2.R2Config(writer="a1", seed_fault="unsourced_claim"))
    assert (out.stop_reason, out.stopped_phase) == ("hard_stop", "tune")
    assert _rows(store, arm="candidate")[0]["details"]["new_hard"] == ["fact.inclusion"]


def test_uncalibrated_judge_counts_no_hard_stop_1_and_spends_no_holdout():
    items = golden(n_tune=4, n_hold=2)                         # 6 labelled pairs < MIN_KAPPA_PAIRS
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1", seed_fault="unsourced_claim"))
    assert not out.judge_counts
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("informative", "completed", "tune")
    assert not _rows(store, phase="holdout", arm="candidate")


def test_low_kappa_judge_is_informative():
    items = golden()
    wrong = [i["item_id"] for i in items if i["split"] == "tune"][:5]
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"), judge=make_judge(wrong_on=wrong))
    assert out.judge_kappa is not None and out.judge_kappa < cfg.MIN_KAPPA and not out.judge_counts
    assert out.verdict == "informative"


def test_futility_at_tune():
    items = golden()
    fail_names = [i["input"]["tour"]["name"] for i in items if i["split"] == "tune"][:3]
    store = FakeStore(items)
    out, writer, _ = run(store, r2.R2Config(writer="a1"), writer=FakeWriter(store, fail_ids=fail_names))
    assert (out.verdict, out.stop_reason) == ("fail", "hard_stop")   # fact.inclusion new vs a passing control
    # without the hard fact class the same candidate is a futility stop
    store2 = FakeStore(golden())

    def judge_no_class():
        inner = make_judge()

        async def j(pool, **kw):
            v = await inner(pool, **kw)
            return SimpleNamespace(**{**v.__dict__, "failure_codes": []})
        return j
    out2, _, _ = run(store2, r2.R2Config(writer="a1"), writer=FakeWriter(store2, fail_ids=fail_names),
                     judge=judge_no_class())
    assert (out2.verdict, out2.stop_reason, out2.stopped_phase) == ("fail", "futility", "tune")
    assert not _rows(store2, phase="holdout", arm="candidate")


def test_probe_trap_or_artefact_fails_the_probe():
    items = golden()
    trap_name = items[0]["input"]["tour"]["name"]
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"), writer=FakeWriter(store, artefact_ids=[trap_name]))
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("fail", "probe_fail", "probe")


def test_isolation_leak_is_a_hard_stop():
    items = golden()
    name = [i for i in items if i["split"] == "tune"][4]["input"]["tour"]["name"]
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"), writer=FakeWriter(store, leak_ids=[name]))
    assert out.stop_reason == "hard_stop"
    leak = [r for r in store.results if r["arm"] == "candidate" and r["hard_stop"]][0]
    assert leak["details"]["isolation"]["leak"] and leak["details"]["hard_stop"] == 2


def test_writer_errors_are_counted_never_a_verdict_and_too_many_invalidate():
    items = golden()
    names = [i["input"]["tour"]["name"] for i in items if i["split"] == "tune"][:6]
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"), writer=FakeWriter(store, raise_ids=names))
    errors = _rows(store, arm="candidate", verdict="error")
    assert errors and all(r["output"] is None for r in errors)
    assert (out.verdict, out.stop_reason) == ("invalid", "error")


def test_limit_makes_the_run_informative_even_when_clean():
    store = FakeStore(golden())
    out, _, _ = run(store, r2.R2Config(writer="a1", limit=4))
    assert out.verdict == "informative" and store.runs[out.run_id]["details"]["truncated"]


def test_post_edit_frozen_output_is_never_a_control():
    items = golden()
    items[3]["post_edit"] = True
    store = FakeStore(items)
    out, _, _ = run(store, r2.R2Config(writer="a1"))
    assert not _rows(store, item_id=items[3]["item_id"], arm="control")
    assert store.runs[out.run_id]["details"]["counts"]["skipped_post_edit"] == 1


# ── refusals ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _release(status="draft", **kw):
    return {"release_id": "rel-1", "writer": "a1", "scope": "platform", "tenant_id": None, "release_no": 1,
            "content_class": "rules", "rule_ids": [], "status": status, **kw}


def test_release_in_the_wrong_state_or_writer_is_refused():
    with pytest.raises(r2.R2Refused):
        run(FakeStore(golden(), release=_release("active")), r2.R2Config(writer="a1", release_id="rel-1"))
    with pytest.raises(r2.R2Refused):
        run(FakeStore(golden(), release=_release(writer="t2")), r2.R2Config(writer="a1", release_id="rel-1"))
    with pytest.raises(r2.R2Refused):
        run(FakeStore(golden()), r2.R2Config(writer="a1", release_id="rel-1", seed_fault="qa_trace"))


def test_release_candidate_view_and_goal():
    store = FakeStore(golden(), release=_release())
    seen = []

    class W(FakeWriter):
        async def __call__(self, writer, inp, *, view, shared_inputs):
            seen.append(view)
            return await super().__call__(writer, inp, view=view, shared_inputs=shared_inputs)
    out, _, _ = run(store, r2.R2Config(writer="a1", release_id="rel-1"), writer=W(store))
    assert seen and all(v["platform"]["release_id"] == "rel-1" for v in seen)
    assert store.runs[out.run_id]["details"]["goal"] == "improve" and store.runs[out.run_id]["release_id"] == "rel-1"
    # a rules release that only matches its control cannot pass: futility
    assert out.stop_reason == "futility"


def test_tenant_release_runs_its_tenant_items_only():
    items = [_item(300 + i, "tune", writer="t9_social", tenant=TENANT_A) for i in range(3)]
    items += [_item(400 + i, "tune", writer="t9_social", tenant=TENANT_B) for i in range(3)]
    rel = _release(writer="t9_social", scope="tenant", tenant_id=TENANT_A, content_class="empty")
    store = FakeStore(items, release=rel)
    out, _, _ = run(store, r2.R2Config(writer="t9_social", release_id="rel-1"))
    assert {r["tenant_id"] for r in store.results} == {TENANT_A}


def test_payload_round_trip():
    conf = r2.R2Config(writer="a1", tiers=["water"], limit=3, cap_usd=2.0, seed_fault="qa_trace")
    assert r2.config_from_payload(r2.payload_of(conf)) == conf
    assert r2.config_from_payload({"writer": "a1", "junk": 1}).writer == "a1"
