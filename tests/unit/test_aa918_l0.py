"""AA-918 WM-16 — the L0 lexicon fast path: learned lexicon rules join the T9 word layers before the prompt and the
gates (app ADR 0010 decisions 3, 4, 6, 12). No DB, no model call.

  * a replacement that adds a number, place, time, price or service is rejected; a neutral one passes;
  * selection: tenant before platform, a declared signature word / CTA / brand / trip-name phrase is skipped, a bad
    phrase is skipped, a channel outside applies_to is skipped;
  * release 0 is a no-op: no rule -> the same pack object, the same prompt lines, no writer_input key;
  * with a rule: the brand block's "Never use" line names it, the WORDS line says the swap, the lexicon gate blocks
    the phrase (and a phrase from the seed stays exempt);
  * the loader fails open to no rule; activation refuses every not-ready state with its code.
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_shared.brand_pack import render_brand_block
from services.acp_shared.signature_vocabulary import t9_words_line
from services.quality import registry as qr
from services.writing_memory import l0
from services.writing_memory.replay import rule_sha
from tests.unit import aa791_t9_fault_set as fs

TENANT = "11111111-1111-1111-1111-111111111111"


def _rule(phrase="truly special", replacement=None, scope="platform", tenant=None, kind="lexicon", rid="r1",
          applies_to=(), writer="t9_social", status="active", lint=None):
    return {"rule_id": rid, "kind": kind, "scope": scope, "tenant_id": tenant, "writer": writer,
            "rule_text": phrase, "replacement": replacement, "match_phrases": [phrase], "applies_to": list(applies_to),
            "status": status, "lint": lint if lint is not None else {"ok": True}, "created_at": "2026-10-25"}


# ── the replacement check (ADR 0010 decision 3) ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase, replacement, what", [
    ("lunch", "lunch included", "service"),              # the decision's own example (promise word)
    ("a stay", "a 3-night stay", "number"),
    ("views", "views of Kyoto", "place"),
    ("start", "start at 7am", "time"),
    ("an early start", "a sunrise start", "time"),
    ("the trip", "the trip from $500", "price"),
    ("a quiet night", "a quiet night in a lodge", "service"),
    ("the ride", "the ride with private transfer", "service"),
])
def test_a_replacement_that_adds_a_specific_is_rejected(phrase, replacement, what):
    out = l0.replacement_check(phrase, replacement)
    assert not out["ok"], (phrase, replacement, what)
    assert out["added"]


@pytest.mark.parametrize("phrase, replacement", [("truly special", "special"), ("hidden gem", "quiet spot"),
                                                 ("nestled", "set"), ("a must-see", "worth seeing")])
def test_a_neutral_replacement_passes(phrase, replacement):
    assert l0.replacement_check(phrase, replacement) == {"ok": True, "detail": "", "added": []}


def test_a_ban_passes_the_replacement_check():
    assert l0.replacement_check("truly special", None)["ok"]


@pytest.mark.parametrize("phrase, ok", [("truly special", True), ("hidden gem", True), ("[NAME] views", False),
                                        ("", False), ("ab", False), ("the", False), ("and you", False),
                                        ("'You'", False), ("the very best", True),
                                        ("never open a post with a question about the weather", False)])
def test_phrase_problem(phrase, ok):
    assert (l0.phrase_problem(phrase) is None) == ok


def test_phrase_of_strips_quotes():
    assert l0.phrase_of({"rule_text": " 'truly   special' "}) == "truly special"


# ── selection and the pack ──────────────────────────────────────────────────────────────────────────────────────────

def test_select_rules_precedence_and_skips():
    pack = {**fs.PACK, "custom_validators": None, "signature_words": ["curated", "Off the beaten path"]}
    rules = [
        _rule("truly special", scope="platform", rid="p1"),
        _rule("truly special", replacement="special", scope="tenant", tenant=TENANT, rid="t1"),
        _rule("curated", rid="p2"),                                    # a signature word the brand declared
        _rule("plan this trip", rid="p3"),                             # in the CTA
        _rule("Trail Pulse", rid="p4"),                                # in the brand name
        _rule("Gobi Desert", rid="p5"),                                # in the trip name
        _rule("[NAME] views", rid="p6"),                               # a mask token never matches
        _rule("hidden gem", rid="p7", applies_to=["linkedin"]),        # not for facebook
        _rule("dreamy", rid="p8", kind="writer_instruction"),
        _rule("lunch", replacement="lunch included", rid="p9"),        # adds a service
        _rule("off the beaten path", rid="p10"),     # listed by the tenant in signature_words, not a POSITIONING word
    ]
    applied, skipped = l0.select_rules(rules, pack=pack, cta=fs.CTA, brand_name="Trail Pulse Adventures",
                                       trip_name=fs.TRIP, channel="facebook")
    assert [e["rule_id"] for e in applied] == ["t1"]          # the tenant rule wins over the platform one
    reasons = {e["rule_id"]: e["reason"] for e in skipped}
    assert set(reasons) == {"p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9", "p10"}
    assert "same phrase" in reasons["p1"] and "signature word" in reasons["p2"]
    assert "CTA" in reasons["p3"] and "brand name" in reasons["p4"] and "trip name" in reasons["p5"]
    assert "masked token" in reasons["p6"] and "linkedin" in reasons["p7"] and "lexicon" in reasons["p8"]
    assert "adds a specific" in reasons["p9"]
    assert "signature word" in reasons["p10"]


# ── a replacement never uses a word the same write forbids (review round) ──────────────────────────────────────────

@pytest.mark.parametrize("phrase, replacement, extra, word", [
    ("a bargain", "a cheap trip", (), "cheap"),                       # platform truth
    ("very nice", "stunning", (), "stunning"),                        # stock superlative
    ("unique", "truly unique", (), "unique"),                         # its own phrase
    ("hidden gem", "quiet corner", ("quiet",), "quiet"),              # the tenant's list / another applied phrase
    ("hidden gem", "quiet spot", (), None),
])
def test_replacement_forbidden(phrase, replacement, extra, word):
    assert l0.replacement_forbidden(phrase, replacement, extra) == word


def test_a_replacement_on_the_tenants_forbidden_list_is_skipped():
    pack = {**fs.PACK, "forbidden_words": ["quiet"]}
    applied, skipped = l0.select_rules([_rule("hidden gem", replacement="quiet spot", rid="a"),
                                        _rule("a bargain", replacement="a cheap trip", rid="b")],
                                       pack=pack, channel="facebook")
    assert applied == []
    reasons = {e["rule_id"]: e["reason"] for e in skipped}
    assert "'quiet'" in reasons["a"] and "'cheap'" in reasons["b"]


def test_chained_rules_never_contradict_each_other():
    # A -> B while B is banned by rule b: A is skipped, b applies
    applied, skipped = l0.select_rules([_rule("hidden gem", replacement="quiet spot", rid="a"),
                                        _rule("quiet spot", rid="b")], pack=dict(fs.PACK), channel="facebook")
    assert [e["rule_id"] for e in applied] == ["b"]
    assert skipped[0]["rule_id"] == "a" and "'quiet spot'" in skipped[0]["reason"]
    # A -> B -> C with C banned: B is skipped, then A is fine again (B's phrase is no longer forbidden)
    applied, skipped = l0.select_rules([_rule("hidden gem", replacement="nice place", rid="a"),
                                        _rule("nice place", replacement="calm place", rid="b"),
                                        _rule("calm place", rid="c")], pack=dict(fs.PACK), channel="facebook")
    assert [e["rule_id"] for e in applied] == ["a", "c"] and [e["rule_id"] for e in skipped] == ["b"]
    # A -> B and B -> A: the first (precedence order) stays
    applied, skipped = l0.select_rules([_rule("hidden gem", replacement="quiet spot", rid="a"),
                                        _rule("quiet spot", replacement="hidden gem", rid="b")],
                                       pack=dict(fs.PACK), channel="facebook")
    assert [e["rule_id"] for e in applied] == ["a"] and [e["rule_id"] for e in skipped] == ["b"]
    # what reaches the prompt: no "Write 'X'" line names a word the brand block forbids
    pack = l0.apply_to_pack(dict(fs.PACK), applied)
    for e in pack["learned_lexicon"]:
        assert not any(l0.replacement_forbidden(e["phrase"], e["replacement"], [w])
                       for w in pack["forbidden_words"])


def test_without_learned_undoes_apply_to_pack():
    own = {**fs.PACK, "forbidden_words": ["luxury", "truly special"]}
    applied = [{"rule_id": "a", "scope": "platform", "phrase": "truly special", "replacement": None},
               {"rule_id": "b", "scope": "platform", "phrase": "hidden gem", "replacement": "quiet spot"}]
    pack = l0.apply_to_pack(own, applied)
    assert [e["added"] for e in pack["learned_lexicon"]] == [False, True]
    assert l0.without_learned(pack) == own                  # the tenant's own "truly special" stays
    assert l0.without_learned(own) is own and l0.without_learned(None) is None


def test_release_zero_is_a_no_op():
    pack = dict(fs.PACK)
    assert l0.apply_to_pack(pack, []) is pack
    assert t9_words_line(pack) == t9_words_line(dict(fs.PACK))
    assert render_brand_block(pack) == render_brand_block(dict(fs.PACK))
    from services.acp_content_writing import service as svc
    assert "writing_rules" not in svc.writer_input_snapshot({"brand_audience": pack, "channel": "facebook"})


def test_applied_rules_reach_the_prompt_and_the_gate():
    pack = {**fs.PACK, "forbidden_words": ["dreamy"]}
    applied, _ = l0.select_rules([_rule("truly special"), _rule("must-see", replacement="worth seeing", rid="r2")],
                                 pack=pack, cta=fs.CTA, brand_name=fs.BRAND, trip_name=fs.TRIP)
    out = l0.apply_to_pack(pack, applied)
    assert pack["forbidden_words"] == ["dreamy"]                              # the input pack is not mutated
    assert out["forbidden_words"] == ["dreamy", "truly special", "must-see"]
    assert [e["rule_id"] for e in out["learned_lexicon"]] == ["r1", "r2"]
    block = render_brand_block(out)
    assert "Never use these words or phrases: dreamy, truly special, must-see" in block
    words = t9_words_line(out)
    assert words.endswith("Write 'worth seeing' instead of 'must-see'.")
    assert words.startswith(t9_words_line(pack))                              # the old line is kept as it was
    text = "Sod Camp is truly special, a quiet first night in a ger near Lake Khövsgöl. " + fs.CTA + "."
    lex = qg.gate_lexicon(text, fs.SEED, out, include_stock=False)
    assert not lex["passed"] and any("truly special" in v for v in lex["violations"])
    assert qg.gate_lexicon(text, fs.SEED, pack, include_stock=False)["passed"]   # without the rule: passes
    # the source exemption every forbidden word has: a phrase the seed holds is never flagged
    assert qg.gate_lexicon(text, fs.SEED + "\nIt is truly special.", out, include_stock=False)["passed"]


def test_writer_input_records_the_rules():
    from services.acp_content_writing import service as svc
    rec = {"writer": "t9_social", "applied": [{"rule_id": "r1"}], "skipped": []}
    assert svc.writer_input_snapshot({"brand_audience": {}, "writing_rules": rec})["writing_rules"] == rec


# ── the loader ──────────────────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_l0_for_write_no_rule_keeps_the_pack():
    pack = dict(fs.PACK)
    out, rec = asyncio.run(l0.l0_for_write(_Pool(_Conn()), tenant_id=TENANT, channel="facebook", pack=pack,
                                           cta=fs.CTA, trip_name=fs.TRIP))
    assert out is pack and rec is None


def test_l0_for_write_reads_the_owner_and_applies():
    conn = _Conn([_rule("truly special")])
    out, rec = asyncio.run(l0.l0_for_write(_Pool(conn), tenant_id=TENANT, channel="linkedin", pack=dict(fs.PACK),
                                           cta=fs.CTA, trip_name=fs.TRIP))
    assert rec["writer"] == "t9_social" and [e["rule_id"] for e in rec["applied"]] == ["r1"]
    assert "truly special" in out["forbidden_words"]
    sql, args = conn.calls[0]
    assert args[0] == "t9_social" and args[1] == ["active"] and str(args[2]) == TENANT   # platform + own tenant


def test_l0_for_write_fails_open_to_no_rule():
    pack = dict(fs.PACK)
    out, rec = asyncio.run(l0.l0_for_write(_Pool(_Conn(error=RuntimeError("relation does not exist"))),
                                           tenant_id=TENANT, channel="blog", pack=pack, cta=fs.CTA, trip_name=None))
    assert out is pack and rec is None


def test_non_t9_writers_have_no_l0_reader():
    assert asyncio.run(l0.load_active(_Pool(_Conn([_rule()])), "a1", None)) == []


# ── activation ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _ready(**kw):
    r = _rule(status="candidate", **kw)
    r["lint"] = {"ok": True, "r0": {"rule_sha": rule_sha(r), "l0_ok": True}}
    return r


@pytest.mark.parametrize("change, code", [
    ({"kind": "writer_instruction"}, "not_lexicon"),
    ({"writer": "a1"}, "no_l0_reader"),
    ({"status": "rejected"}, "not_activatable"),
    ({"status": "active"}, "not_activatable"),
    ({"lint": {"ok": False}}, "lint"),
    ({"rule_text": "[NAME] views"}, "phrase"),
    ({"replacement": "lunch included"}, "replacement_adds"),
    ({"lint": {"ok": True}}, "r0_missing"),
    ({"rule_text": "hidden gem"}, "r0_stale"),          # edited after its R0 run
])
def test_activation_refuses_what_is_not_ready(change, code):
    r = {**_ready(), **change}
    assert l0.activation_problem(r)[0] == code


def test_activation_refuses_a_replacement_the_writer_may_not_use():
    code, why = l0.activation_problem(_ready(replacement="stunning"))
    assert code == "replacement_forbidden" and "stunning" in why


def test_activation_needs_a_passing_r0():
    r = _ready()
    r["lint"]["r0"]["l0_ok"] = False
    r["lint"]["r0"]["l0_reason"] = "new hard stops: F2_platform_truth x1"
    code, why = l0.activation_problem(r)
    assert code == "r0_failed" and "F2_platform_truth" in why
    assert l0.activation_problem(_ready()) is None
    assert l0.activation_problem({**_ready(), "status": "approved"}) is None


def test_run_quality_gates_takes_stored_f9_without_a_judge_call():
    f9 = [qg._result(qr.F9_CTA_FACT, ["stored verdict"]), qg._result(qr.F9_BRAND_STYLE, [], blocking=False)]
    with patch.object(qg, "gate_brand_voice", side_effect=AssertionError("judge called")):
        out = qg.run_quality_gates(content_text=fs.CLEAN, atom_text=fs.SEED, cta=fs.CTA, brand_rubric_text="",
                                   channel="facebook", brand_pack=dict(fs.PACK), f9_results=f9)
    by = {g["gate"]: g for g in out["gate_ledger"]}
    assert by[qr.F9_CTA_FACT]["violations"] == ["stored verdict"] and not out["passed"]
