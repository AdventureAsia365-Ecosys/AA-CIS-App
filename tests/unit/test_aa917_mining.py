"""AA-917 WM-12 — clustering and thresholds ($0), the run trigger, the budget guard, the drafter call (mocked gateway)
and the backfill row builders.

WM-12 Done-when: a tenant-A-only cluster never yields a platform rule; a platform rule from tenant evidence needs 3
tenants; the budget guard blocks a run when the month total is over cap.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.writing_memory import backfill as bf
from services.writing_memory import config as cfg
from services.writing_memory import drafter
from services.writing_memory import mining as M
from services.writing_memory.budget import MonthBudget, month_spend
from shared.cost_guard import BudgetExceeded

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
A, B, C, D = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abcd")
_n = iter(range(10_000))


def sig(tenant=A, piece=None, *, writer="t9_blog", scope=None, reason="ai_phrasing", actor="tenant", actor_id=None,
        status="pass", before="A truly breathtaking view.", after="A view.", gate=None, created=NOW):
    i = next(_n)
    return {"signal_id": f"s{i:05d}", "writer": writer, "channel": "blog", "scope": scope or ("tenant" if tenant else
                                                                                               "platform"),
            "tenant_id": tenant, "field": "content_text", "before_text": before, "after_text": after, "gate": gate,
            "problem": None, "reason_code": reason, "action": "tenant_edit", "actor_type": actor,
            "actor_id": actor_id or f"tenant:{tenant}", "fact_status": status, "source_id": piece or f"p{i}",
            "created_at": created}


def keys(clusters, label=None):
    return {(c.scope, c.tenant_id, c.basis) for c in clusters if label is None or c.label == label}


# ── thresholds ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_tenant_rule_needs_two_edits_on_two_pieces():
    one_piece = [sig(piece="p1"), sig(piece="p1")]
    assert not M.build_clusters(one_piece, a1_pieces_30d=0, now=NOW)
    two = [sig(piece="p1"), sig(piece="p2")]
    assert keys(M.build_clusters(two, a1_pieces_30d=0, now=NOW), "reason:ai_phrasing") == {("tenant", A, "tenant")}


def test_tenant_a_only_cluster_never_yields_a_platform_rule():
    many = [sig(piece=f"p{i}") for i in range(30)]
    got = M.build_clusters(many, a1_pieces_30d=0, now=NOW)
    assert got and all(c.scope == "tenant" and c.tenant_id == A for c in got)


def test_platform_rule_from_tenant_evidence_needs_three_tenants():
    two_tenants = [sig(A), sig(A), sig(B), sig(B)]
    assert ("platform", None, "platform_tenants") not in keys(M.build_clusters(two_tenants, a1_pieces_30d=0, now=NOW))
    three = two_tenants + [sig(C)]
    got = M.build_clusters(three, a1_pieces_30d=0, now=NOW)
    plat = [c for c in got if c.scope == "platform" and c.label == "reason:ai_phrasing"]
    assert len(plat) == 1 and plat[0].support()["tenants"] == 3 and got[0].scope == "platform"   # platform first


def test_only_pass_signals_and_only_human_tenant_signals_count():
    rows = [sig(status=s) for s in ("fail", "grey", "pending")] * 3
    rows += [sig(actor="system", actor_id="system:t9_write") for _ in range(5)]
    assert M.build_clusters(rows, a1_pieces_30d=0, now=NOW) == []


def test_platform_human_edits_need_two_reviewers():
    one = [sig(None, writer="a1", actor="admin", actor_id="r1") for _ in range(4)]
    assert not M.build_clusters(one, a1_pieces_30d=0, now=NOW)
    two = one + [sig(None, writer="a1", actor="admin", actor_id="r2")]
    assert keys(M.build_clusters(two, a1_pieces_30d=0, now=NOW), "reason:ai_phrasing") == \
        {("platform", None, "platform_reviewers")}


def test_a1_gate_repeat_needs_five_percent_of_recent_pieces():
    def a1(created=NOW):
        return sig(None, writer="a1", actor="system", actor_id="system:a1_brand_audit", reason=None,
                   gate="GENERIC_AI_WORDING", created=created)
    rows = [a1() for _ in range(4)]
    assert not M.build_clusters(rows, a1_pieces_30d=100, now=NOW)          # 4 < 5% of 100
    rows.append(a1())
    got = M.build_clusters(rows, a1_pieces_30d=100, now=NOW)
    assert keys(got) == {("platform", None, "a1_gate_repeat")} and got[0].label == "class:other.v1"
    old = [a1(NOW - timedelta(days=40)) for _ in range(10)]
    assert not M.build_clusters(old, a1_pieces_30d=10, now=NOW)            # outside the 30-day window


def test_phrase_clusters_from_removed_words():
    rows = [sig(piece=f"p{i}", reason=None, before=f"Our trip {i} is nestled in hills.",
                after=f"Our trip {i} is in hills.") for i in range(3)]
    got = M.build_clusters(rows, a1_pieces_30d=0, now=NOW)
    assert any(c.label == "phrase:nestled" and len(c.signals) == 3 for c in got)


def test_split_examples_holds_out_two():
    c = M.Cluster("t9_blog", "tenant", A, "reason:x", "tenant", [sig() for _ in range(12)])
    shown, held = M.split_examples(c)
    assert len(shown) == cfg.DRAFT_MAX_EXAMPLES and len(held) == 4
    c3 = M.Cluster("t9_blog", "tenant", A, "reason:x", "tenant", [sig() for _ in range(3)])
    assert [len(x) for x in M.split_examples(c3)] == [1, 2]


def test_example_of_is_masked():
    ex = M.example_of(sig(before="Meet at Lantern Bridge at 7am for $20.", after="Meet at Lantern Bridge."))
    assert "Lantern" not in ex["before"] and "[NAME]" in ex["before"] and "$20" not in ex["before"]


def test_lint_context_platform_gets_every_tenant_tenant_gets_other_names():
    names = {A: ["Alpha Trips"], B: ["Beta Travel"]}
    plat = M.Cluster("t9_blog", "platform", None, "reason:x", "platform_tenants")
    ten = M.Cluster("t9_blog", "tenant", A, "reason:x", "tenant")
    assert M.lint_context(plat, {1, 2}, names) == ({1, 2}, ["Alpha Trips", "Beta Travel"])
    assert M.lint_context(ten, {1, 2}, names) == (set(), ["Beta Travel"])


# ── the trigger ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_decide_run():
    ok = dict(runs_this_month=0, auto_runs_this_month=0, new_pass=10, open_waves=0)
    assert M.decide_run("auto", **ok) is None
    assert "new pass" in M.decide_run("auto", **{**ok, "new_pass": 9})
    assert "automatic runs" in M.decide_run("auto", **{**ok, "runs_this_month": 2, "auto_runs_this_month": 2})
    assert "wave" in M.decide_run("auto", **{**ok, "open_waves": 1})
    assert M.decide_run("manual", **{**ok, "new_pass": 0, "runs_this_month": 2, "open_waves": 1}) is None
    assert "runs this month" in M.decide_run("manual", **{**ok, "runs_this_month": 3})
    assert "unknown" in M.decide_run("weekly", **ok)


# ── the budget guard ────────────────────────────────────────────────────────────────────────────────────────────────

def test_budget_guard_blocks_a_run_over_the_month_cap():
    b = MonthBudget(spent_at_start=24.99)
    with pytest.raises(BudgetExceeded, match="month budget"):
        b.check(0.02)
    b = MonthBudget(spent_at_start=24.0)
    b.check(0.5)
    b.charge(0.98)
    with pytest.raises(BudgetExceeded):
        b.check(0.05)
    assert b.summary()["spent_usd"] == 24.98


@pytest.mark.asyncio
async def test_month_spend_sums_the_memory_stages():
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=3.25))
    assert await month_spend(conn) == 3.25
    sql, stages = conn.fetchval.await_args.args
    assert "date_trunc('month'" in sql and stages == list(cfg.MEMORY_STAGES)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_run_mine_makes_no_call_when_the_month_is_over_cap():
    rows = [sig(piece=f"p{i}") for i in range(6)]
    conn = SimpleNamespace(execute=AsyncMock(return_value="UPDATE 0"), fetch=AsyncMock(side_effect=[rows, [], [], [],
                                                                                                     []]),
                           fetchval=AsyncMock(side_effect=[0, 30.0]), transaction=_Tx)
    run_budget = SimpleNamespace(check=lambda e: None, charge=lambda u: None, summary=lambda: {})
    with patch("shared.cost_guard.load_run_budget", AsyncMock(return_value=run_budget)), \
            patch.object(drafter, "draft_rule", AsyncMock()) as draft:
        out = await M.run_mine(_Pool(conn), None, trigger="manual")
    draft.assert_not_awaited()
    assert out["calls"] == 0 and out["stopped"].startswith("budget") and out["clusters"] >= 1


# ── the drafter ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_draft():
    d = drafter.parse_draft(json.dumps({"kind": "lexicon", "rule_text": "breathtaking", "replacement": "",
                                        "match_phrases": ["breathtaking", " "]}))
    assert d == {"kind": "lexicon", "rule_text": "breathtaking", "replacement": None,
                 "match_phrases": ["breathtaking"]}
    d = drafter.parse_draft(json.dumps({"kind": "writer_instruction", "rule_text": "Cut it.", "replacement": "x",
                                        "match_phrases": ["it"]}))
    assert d["replacement"] is None                                   # only a lexicon rule replaces
    for bad in ('[]', '{"kind": "other", "rule_text": "x", "replacement": "", "match_phrases": []}',
                '{"kind": "lexicon", "rule_text": "", "replacement": "", "match_phrases": []}'):
        with pytest.raises(ValueError):
            drafter.parse_draft(bad)


def _resp(content):
    return SimpleNamespace(content=content, model_used="gpt-5.6-luna", input_tokens=900, output_tokens=60,
                           cost_usd=0.0003, stop_reason="stop", satellite_account="acc3", fallback_used=False,
                           provider="bedrock-satellite")


@pytest.mark.asyncio
async def test_draft_rule_writes_one_row_or_one_billed_unusable_row():
    good = json.dumps({"kind": "writer_instruction", "rule_text": "Cut stock openers.", "replacement": "",
                       "match_phrases": ["truly"]})
    with patch.object(drafter, "_generate_sync", return_value=_resp(good)), \
            patch.object(drafter, "record_call_with_pool", AsyncMock()) as rec, \
            patch.object(drafter, "record_unusable", AsyncMock()) as unusable:
        d = await drafter.draft_rule("pool", user_prompt="p", cluster_key="t9_blog|reason:x", tenant_id=A)
    assert d.rule_text == "Cut stock openers." and d.cost_usd == 0.0003
    kw = rec.await_args.kwargs
    assert kw["stage"] == "writing_memory_mine" and kw["role"] == "judge" and kw["quality_signal"]["json_parsed"]
    unusable.assert_not_awaited()
    with patch.object(drafter, "_generate_sync", return_value=_resp("not json")), \
            patch.object(drafter, "record_call_with_pool", AsyncMock()) as rec, \
            patch.object(drafter, "record_unusable", AsyncMock()) as unusable:
        with pytest.raises(ValueError):
            await drafter.draft_rule("pool", user_prompt="p", cluster_key="k", tenant_id=None)
    rec.assert_not_awaited()
    assert unusable.await_args.kwargs["stage"] == "writing_memory_mine"


def test_user_prompt_carries_masked_examples_only():
    p = drafter.build_user_prompt(writer="t9_blog", channels=["blog"], label="reason:ai_phrasing",
                                  label_meaning="generic AI phrasing", scope="platform",
                                  examples=[M.example_of(sig(before="The Lantern Bridge has 3 cafes.",
                                                                           after="It has cafes."))])
    assert "Lantern" not in p and "[NAME]" in p and "generic AI phrasing" in p


# ── backfill builders ───────────────────────────────────────────────────────────────────────────────────────────────

def test_audit_builder_casts_tenant_and_maps_the_mode():
    row = {"audit_id": "11111111-1111-1111-1111-111111111111", "tenant_id": A, "actor": f"admin:{B}",
           "action": "content_piece.admin_sentence_edited", "resource_type": "content_piece",
           "resource_id": "22222222-2222-2222-2222-222222222222", "created_at": NOW, "channel": "linkedin",
           "details": json.dumps({"before": "Old.", "after": "New.", "start": 0, "end": 4, "gate": "x",
                                  "problem": "p"})}
    [kw] = bf.audit_signals(row)
    assert (kw["writer"], kw["scope"], kw["tenant_id"], kw["action"], kw["actor_id"]) == \
        ("t9_social", "tenant", A, "sentence_manual", B)
    assert kw["locator"] == {"start": 0, "end": 4, "backfill": f"audit:{row['audit_id']}"}
    assert bf.audit_signals({**row, "tenant_id": "not-a-uuid"}) == []
    master = bf.audit_signals({**row, "tenant_id": "00000000-0000-0000-0000-000000000001",
                               "action": "tenant_tour_version.admin_sentence_cut", "resource_type":
                                   "tenant_tour_version", "details": json.dumps({"before": "x", "after": "",
                                                                                 "field": "summary"})})
    assert master[0]["scope"] == "platform" and master[0]["tenant_id"] is None and master[0]["writer"] == "t2"


def test_t2_builder_one_signal_per_changed_field():
    row = {"id": "v2", "tenant_id": A, "at": NOW,
           "before_rc": json.dumps({"summary": "Old.", "highlights": ["a", "b"], "same": "x"}),
           "after_rc": {"summary": "New.", "highlights": ["a"], "same": "x"}}
    got = {kw["field"]: kw for kw in bf.t2_edit_signals(row)}
    assert set(got) == {"summary", "highlights"} and got["highlights"]["before_text"] == '["a", "b"]'
    assert got["summary"]["locator"]["backfill"] == "t2:v2:summary" and got["summary"]["action"] == "tenant_edit"


def test_t9_builder_registered_gates_only_with_the_attempt_text():
    from services.quality import registry as qr
    row = {"piece_id": "p1", "tenant_id": A, "channel": "blog", "created_at": NOW,
           "repair_log": [{"attempt": 1, "gate_targeted": qr.F1_GROUNDING, "violations": ["adds 3"]},
                          {"attempt": 2, "gate_targeted": "gate_regression_guard", "violations": ["reverted"]},
                          {"attempt": 2, "repair_mode": "stock_cut", "sentences_cut": 1}],
           "discarded_attempts": [{"attempt_number": 1, "content_text": "Attempt one."}]}
    [kw] = bf.t9_attempt_signals(row)
    assert (kw["gate"], kw["before_text"], kw["actor_type"], kw["action"]) == \
        (qr.F1_GROUNDING, "Attempt one.", "system", "regenerate")


def test_lesson_builder():
    row = {"id": "q1", "content_id": "c1", "tenant_id": "00000000-0000-0000-0000-000000000001", "evaluated_at": NOW,
           "lessons_extracted": [{"pattern": "Generic opener", "failure_code": "GENERIC_AI_WORDING",
                                  "field": "summary", "example_before": "Embark on a journey.", "severity": "low"},
                                 "junk"]}
    [kw] = bf.a1_lesson_signals(row)
    assert (kw["writer"], kw["scope"], kw["gate"], kw["source_table"], kw["source_id"]) == \
        ("a1", "platform", "GENERIC_AI_WORDING", "generated_content", "c1")
