"""AA-1048 (Quality W2, S278) — pooled claim classes (services/ground_truth/pools.py) and their measurement in the R1
record (services/ground_truth/r1.py): the declaration is valid against the policy, the bar is the class bar applied to
the pool, a passing pool calibrates only its members, the members keep their own counts (split later), and train or
failing-instrument records pass no pool. Invented claims only, no model call."""
import pytest

from services.ground_truth import pools as pl
from services.ground_truth import r1

FACT = "pool.fact_unsupported"
MEMBERS = ("fact.embellish", "fact.inclusion", "fact.misbinding", "fact.number")


def _it(i, cls, label, writer="t9_blog", stratum="representative", split="held_out"):
    return r1.Item(f"{i:064x}", writer, cls, label, stratum, "a" * 64, "content_text", f"invented claim {i}", "", "",
                   (), split)


def _pooled_set(writer="t9_blog", split="held_out", per_member=(5, 6, 9, 1), hard=10):
    """21 violations spread over the four members (none reaches 20 alone, as the S275 held-out count), 10 of them
    hard, plus 200 ok claims of another class."""
    items, n = [], 0
    for cls, k in zip(MEMBERS, per_member):
        for _ in range(k):
            items.append(_it(n, cls, "violation", writer, "hard" if n < hard else "representative", split))
            n += 1
    items += [_it(1000 + j, "scope.framing", "ok", writer, split=split) for j in range(200)]
    return items


def _votes(items, *, miss=()):
    """Every violation caught except the hashes in `miss`; every ok claim cleared."""
    return {x.claim_hash: [x.label == "violation" and x.claim_hash not in miss] * 3 for x in items}


def test_declaration_is_valid_against_the_policy():
    assert pl.problems() == []
    p = pl.POOLS[FACT]
    assert set(p.members) == set(MEMBERS) and "t9_blog" in p.writers and p.decided
    assert pl.pool_of("t9_blog", "fact.inclusion").id == FACT
    assert pl.pool_of("t9_blog", "scope.framing") is None and pl.pool_of("a1", "fact.number") is None
    assert len(pl.pools_sha()) == 64


def test_problems_refuses_bad_declarations():
    classes = {"x.a": ("t9_blog",), "x.b": ("t9_blog",), "x.c": ("t2",)}
    ok = {"pool.ab": pl.Pool("pool.ab", ("x.a", "x.b"), ("t9_blog",), "tech lead")}
    assert pl.problems(ok, classes) == []
    bad = {
        "pool.one": pl.Pool("pool.one", ("x.a",), ("t9_blog",), "tech lead"),
        "x.b": pl.Pool("x.b", ("x.a", "x.b"), ("t9_blog",), "tech lead"),
        "pool.wr": pl.Pool("pool.wr", ("x.a", "x.c"), ("t9_blog",), "tech lead"),
        "pool.zz": pl.Pool("pool.zz", ("x.a", "x.q"), ("t9_blog",), ""),
    }
    text = " | ".join(pl.problems(bad, classes))
    assert "at least two distinct members" in text and "may not be a policy class id" in text
    assert "is not a class of writer t9_blog" in text and "'x.q' is not a policy class" in text
    assert "says who declared it" in text and "already in" in text          # x.a sits in several pools on t9_blog


def test_a_pool_passes_where_no_member_reaches_the_bar_alone():
    items = _pooled_set()
    rec = r1.measure(items, _votes(items))
    assert rec["instrument_conditions_met"] and rec["passing_classes"] == []
    pool = rec["pools"][FACT]
    assert pool["members"] == sorted(MEMBERS) and pool["decided"]
    assert pool["n_pos_by_writer"] == {"t9_blog": 21} and pool["tp_by_writer"] == {"t9_blog": 21}
    assert pool["passing_writers"] == ["t9_blog"] and pool["split_ready"] == []
    assert pool["members_detail"]["fact.misbinding"] == {"n_pos_by_writer": {"t9_blog": 9},
                                                         "tp_by_writer": {"t9_blog": 9}}
    assert rec["passing_pools"] == [f"t9_blog:{FACT}"] and rec["pools_sha"] == pl.pools_sha()
    # the pool calibrates its members on that writer, nothing else
    assert rec["calibrated"] == sorted(f"t9_blog:{m}" for m in MEMBERS)
    assert "t9_blog:scope.framing" not in rec["calibrated"]


def test_the_bar_is_not_lowered_for_a_pool():
    # 19 pooled violations: under n_pos 20
    items = _pooled_set(per_member=(5, 5, 8, 1))
    rec = r1.measure(items, _votes(items))
    assert rec["pools"][FACT]["n_pos_by_writer"] == {"t9_blog": 19} and rec["passing_pools"] == []
    # 21 violations, writer precision under the bar (fires on 40 ok claims): no pool passes
    items = _pooled_set()
    votes = _votes(items)
    votes.update({x.claim_hash: [True] * 3 for x in [i for i in items if i.label == "ok"][:40]})
    rec = r1.measure(items, votes)
    assert not rec["writers"]["t9_blog"]["precision_passes_bar"] and rec["passing_pools"] == []
    assert rec["calibrated"] == []


def test_a_pool_with_no_caught_violation_does_not_pass():
    # the members are missed; the writer's precision and the hard subset come from caught non-member violations
    items = _pooled_set(hard=0) + [_it(5000 + j, "scope.framing", "violation", stratum="hard") for j in range(20)]
    votes = _votes(items, miss={x.claim_hash for x in items if x.claim_class in MEMBERS})
    rec = r1.measure(items, votes)
    assert rec["writers"]["t9_blog"]["precision_passes_bar"] and rec["instrument_conditions_met"]
    assert rec["pools"][FACT]["tp_by_writer"] == {"t9_blog": 0} and rec["passing_pools"] == []


def test_a_train_or_failing_record_passes_no_pool():
    items = _pooled_set(split="train")
    rec = r1.measure(items, _votes(items))
    assert rec["split"] == "train" and rec["pools"][FACT]["passes_bar"]       # the columns still show
    assert rec["passing_pools"] == [] and rec["calibrated"] == []
    # held-out, but the hard subset misses: instrument conditions fail
    items = _pooled_set()
    hard = {x.claim_hash for x in items if x.stratum == "hard"}
    rec = r1.measure(items, _votes(items, miss={sorted(hard)[0]}))
    assert not rec["instrument_conditions_met"] and rec["passing_pools"] == []


def test_the_pool_counts_each_writer_on_its_own_violations():
    items = _pooled_set(per_member=(5, 5, 5, 0))                                  # t9_blog 15
    items += [_it(9000 + j, "fact.inclusion", "violation", "t9_social") for j in range(10)]
    items += [_it(9500 + j, "scope.framing", "ok", "t9_social") for j in range(50)]
    rec = r1.measure(items, _votes(items))
    assert rec["pools"][FACT]["n_pos_by_writer"] == {"t9_blog": 15, "t9_social": 10}
    assert rec["passing_pools"] == []                       # 25 summed over writers is not 20 on one writer


def test_split_ready_names_members_that_reach_the_bar_alone():
    items = _pooled_set(per_member=(2, 2, 22, 1))
    rec = r1.measure(items, _votes(items))
    assert rec["pools"][FACT]["split_ready"] == ["t9_blog:fact.misbinding"]
    assert rec["passing_classes"] == ["t9_blog:fact.misbinding"]
    assert rec["passing_pools"] == [f"t9_blog:{FACT}"]


def test_report_recommends_on_calibrated_keys_and_plan_counts_pool_positives():
    items = _pooled_set()
    luna = r1.Instrument("gpt-6-luna-openai", "openai-direct", "extraction")
    llama = r1.Instrument("llama4-maverick", "acc2", "extraction")
    run_result = {"votes": {luna.id: _votes(items), llama.id: {}}, "stopped": None}
    rep = r1.report(items, run_result, [luna, llama])
    assert rep["recommended"] == luna.id and rep["pools_sha"] == pl.pools_sha()
    assert rep["records"][llama.id]["calibrated"] == []
    plan = r1.plan(items, [luna])
    assert plan["pool_positives"] == {f"t9_blog:{FACT}": 21}


def test_custom_pools_and_positives():
    custom = {"pool.xy": pl.Pool("pool.xy", ("fact.name", "fact.number"), ("t2",), "test")}
    rows = [("t2", "fact.name"), ("t2", "fact.number"), ("t2", "fact.inclusion"), ("t9_blog", "fact.number")]
    assert pl.positives(rows, custom) == {"t2:pool.xy": 2}
    assert pl.positives(rows) == {f"t2:{FACT}": 2, f"t9_blog:{FACT}": 1}
    items = _pooled_set()
    rec = r1.measure(items, _votes(items), pools=custom)
    assert set(rec["pools"]) == {"pool.xy"} and rec["passing_pools"] == []


def test_calibrated_keys_reads_only_passing_pools():
    rec = {"passing_classes": ["t2:fact.name"], "passing_pools": ["t9_blog:pool.p"],
           "pools": {"pool.p": {"members": ["fact.a", "fact.b"]}, "pool.q": {"members": ["fact.c"]}}}
    assert pl.calibrated_keys(rec) == ["t2:fact.name", "t9_blog:fact.a", "t9_blog:fact.b"]
    assert pl.calibrated_keys({}) == []


@pytest.mark.parametrize("member", MEMBERS)
def test_claim_shadow_enforce_map_needs_the_key_a_pool_calibrates(monkeypatch, member):
    """A pool's record feeds claim_shadow.CALIBRATED through calibrated_keys: a member can then be enforced (with its
    override), a non-member of the same writer never."""
    from services.quality import claim_shadow as cs

    items = _pooled_set()
    inst = r1.Instrument("gpt-6-luna-openai", "openai-direct", "extraction")
    rec = r1.measure(items, _votes(items))
    monkeypatch.setattr(cs, "CALIBRATED", {inst.id: frozenset(rec["calibrated"])})
    monkeypatch.setattr(cs, "ENFORCE_OVERRIDES", {f"t9_blog:{member}": "enforce", "t9_blog:scope.framing": "enforce"})
    assert cs.mode_for("t9_blog", member, inst) == "enforce"
    assert cs.mode_for("t9_blog", "scope.framing", inst) == "warn"
    assert cs.mode_for("t2", member, inst) == "warn"
