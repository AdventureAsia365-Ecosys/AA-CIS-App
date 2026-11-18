"""AA-1051 S284 — the W5 null run on golden aa729@1.5.0 ($0 part): the rebased addendum merge that adds 14 panel-labelled
t9_blog items (6 on GATE), the new registered pre-registration `w5-null-t9-blog-v7-g150`, and the R2 wiring that lets a
reverify run be the null run's control (its estimate counts both writes, a fixed-sample reverify reaches every item,
the null run takes the newest reverify of the version). Invented text only; no DB, no model call."""
from __future__ import annotations

import asyncio

import pytest

from scripts.aa729 import common, labels
from services.writer_proof import prereg as pr
from services.writer_proof import runner, splits
from services.writing_memory import config as cfg
from services.writing_memory import r2
from tests.unit.test_aa1051_r2_split_prereg import PreregStore, _conf, _store_with_sides, _w5
from tests.unit.test_aa729_golden_set import _addendum
from tests.unit.test_aa919_r2_runner import BASELINE_RUN, make_judge, run

NEW_ID = "w5-null-t9-blog-v7-g150"
# aa729@1.4.0: the 20 t9_blog items t9b-001..020, 6 of them on GATE by the split rule
GATE_140 = ["t9b-006", "t9b-008", "t9b-013", "t9b-014", "t9b-019", "t9b-020"]
# aa729@1.5.0: the 14 addendum blogs take t9b-021..034 (merge renumbers after the base's highest number)
ADDED_150 = [f"t9b-{i:03d}" for i in range(21, 35)]
GATE_ADDED_150 = ["t9b-024", "t9b-025", "t9b-028", "t9b-029", "t9b-033", "t9b-034"]
REV_RUN = "4e7e0000-0000-4000-8000-000000000001"


# ── the golden version: which ids land on GATE, and the rebased merge ─────────────────────────────────────────────

def test_the_split_rule_puts_six_of_the_fourteen_new_blog_ids_on_gate():
    assert [s for s in ADDED_150 if splits.split_of(s) == "gate"] == GATE_ADDED_150
    assert [f"t9b-{i:03d}" for i in range(1, 21) if splits.split_of(f"t9b-{i:03d}") == "gate"] == GATE_140
    assert len(GATE_140 + GATE_ADDED_150) == 12 == pr.min_items("null")


def _rebase_case():
    """A base two versions on from the version its addendum was frozen against (labels only were added since)."""
    base, add = _addendum("1.0.0")
    base["fixture_version"] = "1.2.0"
    base["label_history"] = [dict(from_version="1.0.0", to_version="1.1.0", source="agent_panel", applied=3),
                             dict(from_version="1.1.0", to_version="1.2.0", source="agent_panel_brand", applied=5)]
    return base, add


def test_merge_refuses_an_addendum_of_an_earlier_version_without_rebase():
    base, add = _rebase_case()
    with pytest.raises(ValueError, match="--rebase"):
        labels.merge(base, add, approved_by="owner", drop_unlabelled=True)


def test_merge_with_rebase_accepts_an_earlier_version_of_the_lineage_and_records_it():
    base, add = _rebase_case()
    assert labels.lineage(base) == {"1.0.0", "1.1.0", "1.2.0"}
    new, info = labels.merge(base, add, approved_by="owner", drop_unlabelled=True, rebase=True)
    assert new["fixture_version"] == "1.3.0" and info["rebased_from"] == "1.0.0"
    assert new["label_history"][-1]["rebased_from"] == "1.0.0"
    assert new["selection"]["addenda"][-1]["rebased_from"] == "1.0.0"
    assert info["eval_split"] == {sid: splits.split_of(sid) for sid in info["added"]}
    assert info["gate_by_writer"] == labels.eval_gate_counts(new["samples"])


@pytest.mark.parametrize("change, msg", [
    (lambda b, a: a.update(addendum_of="0.9.0"), "extends"),                       # not in the lineage
    (lambda b, a: a["samples"][0]["refs"].update(piece_id=b["samples"][16]["refs"]["piece_id"]), "already holds"),
])
def test_rebase_still_refuses_a_foreign_base_and_a_duplicate_output(change, msg):
    base, add = _rebase_case()
    change(base, add)
    with pytest.raises(ValueError, match=msg):
        labels.merge(base, add, approved_by="owner", drop_unlabelled=True, rebase=True)


def test_an_unrebased_merge_reports_no_rebase():
    base, add = _addendum("1.0.0")
    new, info = labels.merge(base, add, approved_by="owner", drop_unlabelled=True)
    assert info["rebased_from"] is None and "rebased_from" not in new["label_history"][-1]


def test_eval_gate_counts_skips_traps_and_counts_per_writer():
    def s(sid, stratum, kind, channel=None):
        ctx = {"channel": channel} if channel else {}
        return {"sample_id": sid, "stratum": stratum, "kind": kind, "channel": channel, "input": {"context": ctx},
                "label": common.make_label("pass", "agent_panel")}
    samples = [s(g, "t9_blog", "t9", "blog") for g in GATE_140] + [s("t9b-021", "t9_blog", "t9", "blog")]
    samples.append(s("inj-t9-zz", "injection", "injection_t9", "blog"))
    assert labels.eval_gate_counts(samples) == {"t9_blog": 6}


# ── the pre-registration ─────────────────────────────────────────────────────────────────────────────────────────

def test_the_new_null_run_is_registered_on_golden_150_with_the_accepted_values():
    pre = pr.load(NEW_ID)
    assert pre.registered and pr.read_lock()[NEW_ID] == pre.sha256 and pr.problems(pre.doc) == []
    d = pre.doc
    assert d["workflow"] == "null" and d["golden_set"] == {"name": "aa729", "version": "1.5.0"}
    assert d["sample"] == {"rule": "seeded_hash", "n": 12, "noise": 0} and d["sample_ids"] == []
    assert d["decision"]["margin_abs"] == 4.0 and d["decision"]["metric"] == "fail_claims_per_item"
    assert d["power"]["writer_sd"] == 4.6 and d["power"]["min_power"] == 0.8
    assert d["judge"]["instrument"] == "aa1048-two-step-v7" and d["judge"]["advisory"] is True
    assert d["cap_usd"] == 5.0 <= pr.workflow_cap("null")
    assert runner.CAMPAIGN_ORDER[0] == NEW_ID


def test_the_v7_file_stays_registered_and_only_the_golden_version_and_note_differ():
    old, new = pr.load("w5-null-t9-blog-v7"), pr.load(NEW_ID)
    assert old.sha256 == pr.read_lock()["w5-null-t9-blog-v7"]          # never edited (a new attempt is a new id)
    differ = {k for k in set(old.doc) | set(new.doc) if old.doc.get(k) != new.doc.get(k)}
    assert differ == {"id", "registered_on", "golden_set", "note"}


def test_the_rule_draws_all_twelve_gate_blogs_of_golden_150():
    items = [{"sample_id": s, "writer": "t9_blog", "trap": False, "eval_split": splits.split_of(s)}
             for s in [f"t9b-{i:03d}" for i in range(1, 35)]]
    items.append({"sample_id": "inj-t9-zz", "writer": "t9_blog", "trap": True, "eval_split": "gate"})
    got = pr.resolve(pr.load(NEW_ID), items)
    assert got.doc["sample_ids"] == sorted(GATE_140 + GATE_ADDED_150)
    assert got.doc["sample_resolved"] == {"rule": "seeded_hash", "gate_items_available": 12, "asked": 12,
                                          "picked": 12}


# ── R2: a reverify run as the null run's control ──────────────────────────────────────────────────────────────────

def test_a_reverify_counts_both_writes_of_an_item():
    assert r2.writes_per_item("reverify") == 2 and r2.writes_per_item("r2") == 1
    one, two = r2.item_estimate("t9_blog", [0.1] * 20), r2.item_estimate("t9_blog", [0.1] * 20, writes=2)
    assert two["item_usd"] == pytest.approx(2 * one["item_usd"]) and two["writes"] == 2
    assert two["reserve_usd"] == pytest.approx(two["item_usd"] + 2 * cfg.JUDGE_CALL_ESTIMATE_USD)


def test_full_sample_runs():
    assert r2.full_sample(_conf(run_kind="reverify", eval_split="gate", sample_ids=["s-106"]))
    assert not r2.full_sample(_conf(run_kind="reverify"))
    assert not r2.full_sample(_conf(eval_split="gate", sample_ids=["s-106"]))
    assert r2.full_sample(_w5())
    stats = {"n": 3, "new_fails": 3, "fixed": 0}
    assert r2.tune_step(_conf(run_kind="reverify", sample_ids=["s-106"]), stats, "non_inferior", False) == \
        r2.PREREG_FULL_SAMPLE


def test_a_reverify_ranks_its_regenerated_control_first():
    assert min(r2.CONTROL_RANKS["reverify"], key=r2.CONTROL_RANKS["reverify"].get) == ("control", "regenerate")
    assert min(r2.CONTROL_RANKS[None], key=r2.CONTROL_RANKS[None].get) == ("candidate", "regenerate")


def _gate_ids(store):
    return sorted(it["sample_id"] for it in store._items if it.get("eval_split") == "gate")


def test_a_fixed_sample_reverify_regenerates_the_control_of_every_item_even_when_tune_would_stop():
    """An uncounted judge makes R2 stop after tune (informative); a reverify of a fixed sample goes on, so the holdout
    items of the sample get their control too."""
    def go(sample):
        store = _store_with_sides()
        cal = [it["item_id"] for it in store._items if it.get("eval_split") == "cal"]
        conf = _conf(run_kind="reverify", eval_split="gate", sample_ids=_gate_ids(store) if sample else None,
                     require_calibrated=False)
        run(store, conf, judge=make_judge(wrong_on=cal))
        holdout = {it["item_id"] for it in store._items if it.get("eval_split") == "gate" and it["split"] == "holdout"}
        return {r["item_id"] for r in store.results if r["arm"] == "control" and r["mode"] == "regenerate"}, holdout
    with_sample, holdout = go(True)
    without, _ = go(False)
    assert holdout and holdout <= with_sample and not (holdout & without)


class _RevStore(PreregStore):
    """A version with a finished reverify run whose regenerated controls cover every gate item."""

    def __init__(self, items, rev=REV_RUN, **kw):
        super().__init__(items, **kw)
        self.reverify_run = rev
        self.run_infos[REV_RUN] = {"run_kind": "reverify", "golden_set_id": "5e700000-0000-4000-8000-000000000001"}

    async def control_sources(self, run_id, items):
        if run_id != REV_RUN:
            return await super().control_sources(run_id, items)
        want = {i["item_id"] for i in items}
        return {i["item_id"]: {"result_id": f"rev-{i['item_id']}", "item_id": i["item_id"], "mode": "regenerate",
                               "arm": "control", "output": {"name": "Reverify text", "summary": "A calm text."}}
                for i in self._items if i["item_id"] in want and not i["trap"]}


def test_the_null_run_takes_the_newest_reverify_of_the_version_as_its_control():
    items = _store_with_sides()._items
    store = _RevStore(items)
    run(store, _w5(sample_ids=_gate_ids(store)))
    details = next(iter(store.runs.values()))["details"]
    assert details["control_run_id"] == REV_RUN and details["control_run_stale"] is None
    assert details["controls"]["found"] == details["controls"]["planned"] > 0
    rejudged = [r for r in store.results if r["arm"] == "control" and r["mode"] == "rejudge"
                and str(r.get("scored_result_id", "")).startswith("rev-")]
    assert rejudged


def test_other_workflows_and_a_version_without_a_reverify_keep_the_release_control():
    items = _store_with_sides()._items
    t9 = _RevStore(items)
    run(t9, _w5(sample_ids=_gate_ids(t9), prereg_id="w5-t9", prereg_workflow="t9"))
    assert next(iter(t9.runs.values()))["details"]["control_run_id"] == BASELINE_RUN
    none = _RevStore(items, rev=None)
    run(none, _w5(sample_ids=_gate_ids(none)))
    assert next(iter(none.runs.values()))["details"]["control_run_id"] == BASELINE_RUN


def test_the_reverify_control_sql_reads_finished_reverify_runs_of_one_writer_and_version():
    from services.writing_memory.r2_w5 import REVERIFY_CONTROL_SQL
    sql = " ".join(REVERIFY_CONTROL_SQL.split())
    for part in ("run_kind = 'reverify'", "writer = $1", "golden_set_id = $2::uuid", "finished_at IS NOT NULL",
                 "ORDER BY finished_at DESC"):
        assert part in sql


def test_pg_store_reads_the_reverify_run_with_writer_then_set():
    seen = {}

    class _Conn:
        async def fetchval(self, sql, *args):
            seen["args"] = args
            return REV_RUN

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    got = asyncio.run(r2.PgStore(_Pool()).reverify_control_run("set-1", "t9_blog"))
    assert got == REV_RUN and seen["args"] == ("t9_blog", "set-1")
