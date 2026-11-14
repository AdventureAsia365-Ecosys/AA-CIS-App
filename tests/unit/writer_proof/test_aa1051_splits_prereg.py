"""AA-1051 — the DEV / CAL / GATE rule (splits.py) and the pre-registration format, lock and draft (prereg.py).
Pure: no DB, no model. Invented sample ids only."""
from __future__ import annotations

import json
from collections import Counter

import pytest

from services.writer_proof import prereg as pr
from services.writer_proof import splits
from services.writer_proof import spend
from services.writing_memory import config as cfg


# ── splits ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_rule_is_stable_and_in_range():
    a = splits.bucket_of("zz-sample-1")
    assert a == splits.bucket_of("zz-sample-1") and 0 <= a < 1
    assert a == round(a, 6)                                # fits numeric(7,6) without rounding up to 1
    assert splits.split_of("zz-sample-1") == splits.side_of(a)


def test_rule_shares_are_about_30_30_40():
    c = Counter(splits.split_of(f"zz-{i:05d}") for i in range(20000))
    assert abs(c["cal"] / 20000 - 0.30) < 0.02
    assert abs(c["gate"] / 20000 - 0.30) < 0.02
    assert abs(c["dev"] / 20000 - 0.40) < 0.02


@pytest.mark.parametrize("bucket,side", [(0.0, "cal"), (0.299999, "cal"), (0.30, "gate"), (0.599999, "gate"),
                                         (0.60, "dev"), (0.999999, "dev")])
def test_side_bounds(bucket, side):
    assert splits.side_of(bucket) == side


def test_side_of_refuses_out_of_range():
    with pytest.raises(ValueError):
        splits.side_of(1.0)


def test_assign_is_one_row_per_sample_in_order():
    rows = splits.assign(["zz-b", "zz-a", "zz-b"])
    assert [r["sample_id"] for r in rows] == ["zz-a", "zz-b"]
    assert all(r["eval_split"] == splits.split_of(r["sample_id"]) for r in rows)


def test_summarise_counts_missing_and_drift():
    s1 = "zz-one"
    wrong = next(x for x in splits.SIDES if x != splits.split_of(s1))
    rows = [{"sample_id": s1, "writer": "t9_blog", "eval_split": wrong, "rule": splits.RULE},
            {"sample_id": "zz-two", "writer": "t9_blog", "eval_split": None},
            {"sample_id": "zz-three", "writer": "a1", "eval_split": "dev", "rule": "older-rule"}]
    out = splits.summarise(rows)
    assert out["missing"] == 1 and out["rule_drift"] == [s1]
    assert out["stored"] == {"a1": {"dev": 1}, "t9_blog": {wrong: 1}}
    assert sum(out["proposed_for_missing"]["t9_blog"].values()) == 1


class _Conn:
    """A fake asyncpg connection for write_missing: two items, one already placed."""

    def __init__(self):
        self.stored = {"zz-placed": ("gate", splits.RULE, 0.4)}
        self.items = [("zz-placed", "t9_blog"), ("zz-new", "a1")]
        self.inserts = []

    async def fetch(self, sql, set_id):
        assert "eval_golden_split" in sql
        return [{"sample_id": s, "writer": w, "trap": False,
                 "eval_split": (self.stored.get(s) or (None,))[0], "rule": (self.stored.get(s) or (None, None))[1],
                 "bucket": None} for s, w in self.items]

    async def execute(self, sql, set_id, ids, sides, buckets, rule, by):
        assert "ON CONFLICT (set_id, sample_id) DO NOTHING" in sql
        self.inserts.append((ids, sides, rule, by))
        for i, s, b in zip(ids, sides, buckets):
            self.stored[i] = (s, rule, b)


@pytest.mark.asyncio
async def test_write_missing_writes_only_items_without_a_side():
    c = _Conn()
    out = await splits.write_missing(c, "set-1", assigned_by="pytest")
    assert out["inserted"] == 1 and c.inserts[0][0] == ["zz-new"]
    assert c.inserts[0][2] == splits.RULE and out["summary"]["missing"] == 0
    again = await splits.write_missing(c, "set-1", assigned_by="pytest")
    assert again["inserted"] == 0 and len(c.inserts) == 1


# ── pre-registration ─────────────────────────────────────────────────────────────────────────────────────────────

def _doc(**over) -> dict:
    d = json.loads(pr.path_for("w5-null-t9-blog").read_text())
    d.update(status="registered", registered_by="tech_lead", registered_on="2026-11-20",
             golden_set={"name": "aa729", "version": "1.4.0"},
             sample_ids=sorted(f"zz-gate-{i:02d}" for i in range(12)), noise_sample_ids=[])
    d.update(over)
    return d


@pytest.mark.parametrize("pid", ["w5-null-t9-blog", "w5-t9", "w5-s1", "w5-t2"])
def test_shipped_drafts_are_valid_drafts_within_d12(pid):
    pre = pr.load(pid)
    assert not pre.registered and pre.doc["eval_split"] == "gate"
    assert pre.doc["cap_usd"] == pr.workflow_cap(pre.workflow)
    with pytest.raises(pr.PreregRefused, match="draft"):
        pr.require_registered(pre)


def test_d12_caps_add_up_to_the_total():
    assert sum(pr.workflow_cap(w) for w in pr.WORKFLOWS) == pr.total_cap() == 33.0
    assert spend.W5_STAGE == cfg.W5_STAGE
    from services.jobs.writing_memory_job import R2
    assert spend.R2_JOB_KIND == R2 and spend.R2_RUN_CAP_USD == cfg.R2_RUN_CAP_USD


def test_registered_doc_is_valid_and_sha_ignores_formatting():
    d = _doc()
    assert pr.problems(d) == []
    reformatted = json.loads(json.dumps(d, indent=4))
    assert pr.sha256_of(reformatted) == pr.sha256_of(d)
    assert pr.sha256_of(_doc(decision={**d["decision"], "margin_abs": 0.2})) != pr.sha256_of(d)


@pytest.mark.parametrize("over,needle", [
    ({"cap_usd": 5.01}, "cap_usd"),
    ({"workflow": "t7"}, "workflow"),
    ({"eval_split": "dev"}, "gate"),
    ({"sample_ids": ["zz-b", "zz-a"]}, "sorted"),
    ({"sample_ids": ["zz-a"] * 2}, "distinct"),
    ({"sample_ids": sorted(f"zz-gate-{i:02d}" for i in range(11))}, ">= 12"),
    ({"noise_sample_ids": ["zz-other"]}, "subset"),
    ({"judge": {"model_key": ""}}, "pin the judge"),
    ({"release_id": "not-a-uuid"}, "release_id"),
    ({"golden_set": {"name": "aa729"}}, "version"),
    ({"registered_on": "soon"}, "registered_on"),
    ({"power": {"min_power": 0.3}}, "min_power"),
])
def test_problems_refuse(over, needle):
    assert any(needle in p for p in pr.problems(_doc(**over)))


def test_decision_and_metric_problems():
    d = _doc()
    assert any("margin_abs" in p for p in pr.problems(_doc(decision={**d["decision"], "margin_abs": 0})))
    assert any("relative" in p or "margin_abs" in p
               for p in pr.problems(_doc(decision={**d["decision"], "margin_abs": "5%"})))
    assert any("primary metric" in p for p in pr.problems(_doc(decision={**d["decision"], "metric": "hard_stops"})))
    two = [dict(m, primary=True) for m in d["metrics"][:2]]
    assert any("exactly one" in p for p in pr.problems(_doc(metrics=two)))
    bad = d["metrics"] + [{"name": "made_up", "kind": "computed", "direction": "lower"}]
    assert any("not a computed metric" in p for p in pr.problems(_doc(metrics=bad)))
    prim = [{"name": "cost_per_object_usd", "kind": "computed", "direction": "lower", "primary": True}]
    assert any("paired" in p for p in pr.problems(_doc(metrics=prim, decision={**d["decision"],
                                                                                "metric": "cost_per_object_usd"})))


def test_a_null_run_takes_no_release_and_no_improve_rule():
    p = pr.problems(_doc(release_id="12345678-1234-1234-1234-123456789abc"))
    assert any("null run takes no release_id" in x for x in p)
    p = pr.problems(_doc(decision={"rule": "improve", "metric": "item_fail_rate", "margin_abs": 0.15, "alpha": 0.05,
                                   "noise_se_k": 2.0},
                         power={"min_power": 0.8, "writer_sd": None, "claims_per_item": 60, "effect_abs": 0.3}))
    assert any("null run decides non_inferior" in x for x in p)


@pytest.mark.parametrize("eff,needle", [(None, "needs power.effect_abs"), (0.1, "must be > decision.margin_abs"),
                                        (-1, "effect_abs must be null or a number > 0"), (0.3, None)])
def test_an_improve_rule_needs_an_effect_above_the_margin(eff, needle):
    d = json.loads(pr.path_for("w5-t9").read_text())
    d["power"] = {**d["power"], "effect_abs": eff}
    p = pr.problems(d)
    assert (needle is None and p == []) or any(needle in x for x in p), p


def test_load_refuses_a_missing_file_and_a_wrong_name(tmp_path):
    with pytest.raises(pr.PreregRefused, match="missing"):
        pr.load("w5-nothing-here")
    f = tmp_path / "zz-other-name.json"
    f.write_text(json.dumps(_doc()))
    with pytest.raises(pr.PreregRefused, match="file name is the id"):
        pr.load(str(f))
    g = tmp_path / "w5-null-t9-blog.json"
    g.write_text("{not json")
    with pytest.raises(pr.PreregRefused, match="not JSON"):
        pr.load(str(g))


def test_lock_refuses_an_edit_after_the_first_result():
    pre = pr.from_doc(_doc())
    assert pr.lock_problem(pre, []) is None
    assert pr.lock_problem(pre, [{"run_id": "r1", "sha256": "f" * 64, "results": 0}]) is None   # no result yet
    assert pr.lock_problem(pre, [{"run_id": "r1", "sha256": pre.sha256, "results": 9}]) is None
    why = pr.lock_problem(pre, [{"run_id": "r1", "sha256": "f" * 64, "results": 1}])
    assert why and "edited after its first result" in why


def test_pick_sample_is_seeded_and_sorted():
    pool = [f"zz-{i:03d}" for i in range(50)]
    a = pr.pick_sample(pool, 12, seed="w5-null-t9-blog")
    assert a == pr.pick_sample(list(reversed(pool)), 12, seed="w5-null-t9-blog") and a == sorted(a)
    assert a != pr.pick_sample(pool, 12, seed="w5-t9") and len(a) == 12


def test_draft_takes_only_gate_non_trap_items_of_the_writer():
    template = json.loads(pr.path_for("w5-null-t9-blog").read_text())
    items = ([{"sample_id": f"zz-g-{i:02d}", "writer": "t9_blog", "trap": False, "eval_split": "gate"}
              for i in range(20)]
             + [{"sample_id": "zz-trap", "writer": "t9_blog", "trap": True, "eval_split": "gate"},
                {"sample_id": "zz-cal", "writer": "t9_blog", "trap": False, "eval_split": "cal"},
                {"sample_id": "zz-a1", "writer": "a1", "trap": False, "eval_split": "gate"}])
    out = pr.draft(template, items=items, golden_version="1.4.0", noise=4)
    assert len(out["sample_ids"]) == 12 and all(s.startswith("zz-g-") for s in out["sample_ids"])
    assert set(out["noise_sample_ids"]) <= set(out["sample_ids"]) and len(out["noise_sample_ids"]) == 4
    assert out["status"] == "draft" and out["golden_set"]["version"] == "1.4.0"
    assert out["draft_note"] == {"gate_items_available": 20, "asked": 12, "picked": 12}
    assert pr.problems(out) == []
    short = pr.draft(template, items=items[:5], golden_version="1.4.0")
    assert short["draft_note"]["picked"] == 5          # a registered file would be refused (< 12)
