"""AA-918 WM-15 — R0, the $0 replay of a writing rule over stored text, ledgers, repair logs and signals. No DB (a
fake pool serves the two reads), no model call (the F9 verdicts are the stored ones).

  * k follows the mining threshold config: one `cluster_threshold` for the miner and the R0 candidate filter;
  * the baseline sanity check: a stored text replayed against the ledger stored with it agrees gate by gate;
  * a ban: the phrase drops to 0 with 0 new hard stops -> l0_ok; the gate fires on the stored text;
  * a replacement that brings in a platform-truth word: a new hard stop -> not l0_ok;
  * a phrase in the seed is source-exempt; a phrase nowhere in stored text proves nothing;
  * past failures that quote the phrase (final ledger, repair_log, discarded attempts) are counted as prevented;
  * the result keeps counts and ids only (no text) and records $0.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from services.quality import registry as qr
from services.writing_memory import config as cfg
from services.writing_memory import mining
from services.writing_memory import replay as rp
from tests.unit import aa791_t9_fault_set as fs

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
TENANT = "11111111-1111-1111-1111-111111111111"
WI = {"seed": fs.SEED, "route_moments": [], "facts": "", "scope": fs.SCOPE, "brand_pack": dict(fs.PACK),
      "brand_rubric": "rubric", "angle": {"name": "First night"}, "goal": "awareness", "channel": "facebook",
      "keyword": None, "cta": fs.CTA, "trip": fs.TRIP, "destination": "Mongolia"}
TEXT = fs.CLEAN.replace("a quiet first night", "a truly special first night")


def _ledger(text: str) -> list:
    pr = rp.PieceReplay(WI, "facebook")
    return pr.run(text, pack=pr.pack, f9=rp._stored_f9([]), stock_blocking=True)


def _piece(pid="p1", text=TEXT, **kw) -> dict:
    return {"piece_id": pid, "tenant_id": TENANT, "channel": "facebook", "status": "approved", "attempt_number": 1,
            "content_text": text, "gate_ledger": _ledger(text), "repair_log": [], "discarded_attempts": [],
            "writer_input": WI, "seo_title": None, "meta_description": None, "slug": None, "created_at": NOW, **kw}


def _rule(phrase="truly special", replacement=None, basis="tenant", **kw) -> dict:
    return {"rule_id": "r1", "kind": "lexicon", "scope": "tenant", "tenant_id": TENANT, "writer": "t9_social",
            "rule_text": phrase, "replacement": replacement, "match_phrases": [phrase], "applies_to": [],
            "status": "candidate", "lint": {"ok": True, "basis": basis}, **kw}


def _signal(i, *, before=TEXT, after=None, actor="tenant", days=1, status="pass") -> dict:
    after = after if after is not None else before.replace("truly special ", "")
    return {"signal_id": f"s{i}", "writer": "t9_social", "channel": "facebook", "scope": "tenant",
            "tenant_id": TENANT, "field": "content_text", "before_text": before,
            "after_text": None if actor == "system" else after, "gate": None, "problem": None,
            "action": "tenant_edit", "actor_type": actor, "actor_id": f"tenant:{TENANT}", "fact_status": status,
            "source_table": "content_piece", "source_id": f"p{i}", "created_at": NOW - timedelta(days=days)}


class _Conn:
    def __init__(self, signals, pieces):
        self.signals, self.pieces, self.calls = signals, pieces, []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "shared.writing_signal" in sql:
            return self.signals
        if "acp_shared.content_piece" in sql:
            return [{**p, **{k: json.dumps(p[k]) for k in ("gate_ledger", "repair_log", "discarded_attempts",
                                                           "writer_input")}} for p in self.pieces]
        raise AssertionError(sql)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _r0(rule, signals=(), pieces=()):
    conn = _Conn(list(signals), list(pieces))
    return asyncio.run(rp.run_r0(_Pool(conn), rule, now=NOW)), conn


# ── k = the mining threshold ────────────────────────────────────────────────────────────────────────────────────────

def test_k_follows_the_mining_threshold_config(monkeypatch):
    assert mining.cluster_threshold("tenant")["signals"] == max(cfg.MIN_SUPPORT, cfg.TENANT_MIN_SIGNALS)
    assert mining.cluster_threshold("platform_tenants")["tenants"] == cfg.PLATFORM_MIN_TENANTS
    assert mining.cluster_threshold("platform_reviewers")["reviewers"] == cfg.PLATFORM_MIN_REVIEWERS
    assert mining.cluster_threshold("a1_gate_repeat", a1_pieces_30d=200)["pieces"] == 10    # 5% of 200
    sigs = [_signal(i) for i in range(3)]
    res, _ = _r0(_rule(), sigs)
    assert res["recurrence"]["k"]["signals"] == 2 and res["r2_eligible"]
    monkeypatch.setattr(cfg, "TENANT_MIN_SIGNALS", 4)
    monkeypatch.setattr(cfg, "TENANT_MIN_PIECES", 4)
    assert mining.cluster_threshold("tenant")["signals"] == 4
    res, _ = _r0(_rule(), sigs)
    assert res["recurrence"]["k"]["signals"] == 4 and not res["r2_eligible"] and res["r2_reason"]
    # the miner reads the same number: 3 tenant edits on 3 pieces no longer make a cluster
    assert not [c for c in mining.build_clusters(sigs, a1_pieces_30d=0, now=NOW) if c.scope == "tenant"]


def test_recurrence_counts_only_the_last_30_days_and_no_quarantined_signal():
    sigs = [_signal(1, days=2), _signal(2, days=45), _signal(3, days=3, status="fail")]
    res, _ = _r0(_rule(), sigs)
    assert res["signals"]["matched"] == 2 and res["recurrence"]["have"]["signals"] == 1 and not res["r2_eligible"]


# ── the baseline sanity check ───────────────────────────────────────────────────────────────────────────────────────

def test_baseline_reproduces_the_stored_ledger():
    res, _ = _r0(_rule(), pieces=[_piece("p1"), _piece("p2", text=fs.CLEAN)])
    b = res["baseline"]
    assert b["pieces_compared"] == 2 and b["pieces_agree"] == 2 and b["agreement"] == 1.0
    assert not any(g.startswith(("F9_", "F10_")) for g in b["by_gate"])           # stubbed gates are not compared


def test_baseline_reports_a_disagreeing_gate():
    led = _ledger(TEXT)
    for g in led:
        if g["gate"] == qr.F11_BRAND_NAME:
            g.update(passed=False, violations=["stored failure"])
    res, _ = _r0(_rule(), pieces=[_piece(gate_ledger=led)])
    assert res["baseline"]["by_gate"][qr.F11_BRAND_NAME] == {"compared": 1, "agree": 0, "stored_fail": 1,
                                                              "replay_fail": 0}
    assert res["baseline"]["pieces_agree"] == 0


# ── the L0 verdict ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_ban_drops_the_phrase_with_no_new_hard_stop():
    # p2's final text is clean, but signal 2 (a tenant edit of p2) holds the writer's text with the phrase
    res, conn = _r0(_rule(), [_signal(1), _signal(2)], [_piece("p1"), _piece("p2", text=fs.CLEAN)])
    assert res["phrase"] == {"before": 2, "after": 0}
    assert res["new_hard_stops"] == {} and res["pieces"]["fires"] == 2 and res["fire_piece_ids"] == ["p1", "p2"]
    assert res["l0_ok"] and res["l0_reason"] is None and res["cost_usd"] == 0.0
    # tenant rule: the reads are filtered to its tenant
    assert all(str(args[2]) == TENANT for _, args in conn.calls)


def test_a_replacement_that_brings_a_hard_stop_fails():
    res, _ = _r0(_rule(replacement="cheap"), [_signal(1)], [_piece()])
    assert res["new_hard_stops"] == {qr.F2_PLATFORM_TRUTH: 1}
    assert not res["l0_ok"] and "new hard stops" in res["l0_reason"]


def test_a_phrase_from_the_seed_is_exempt_and_nothing_to_prove_without_text():
    res, _ = _r0(_rule("mountain range"), pieces=[_piece(text=fs.CLEAN)])
    assert res["pieces"]["source_exempt"] == 1 and res["pieces"]["fires"] == 0
    res, _ = _r0(_rule("never written"), pieces=[_piece()])
    assert res["phrase"]["before"] == 0 and not res["l0_ok"] and "nothing to prove" in res["l0_reason"]


def test_the_writers_text_before_an_edit_is_replayed_too():
    # the tenant cut the phrase: the stored final text no longer has it, the edit signal's before text does
    res, _ = _r0(_rule(), [_signal(1)], [_piece("p1", text=fs.CLEAN)])
    assert res["phrase"]["before"] == 1 and res["pieces"]["fires"] == 1 and res["l0_ok"]


def test_non_lexicon_and_non_t9_rules_have_no_l0_verdict():
    res, conn = _r0(_rule(kind="writer_instruction"), [_signal(1)], [_piece()])
    assert not res["l0_applicable"] and not res["l0_ok"] and "lexicon" in res["l0_reason"]
    res, conn = _r0(_rule(writer="t2"), [])
    assert res["pieces"]["pieces"] == 0 and "no L0 reader" in res["l0_reason"]
    assert not any("content_piece" in sql for sql, _ in conn.calls)


def test_prevented_failures_quote_the_phrase():
    discarded = [{"attempt_number": 1, "content_text": TEXT,
                  "gate_ledger": [{"gate": "F15_voice", "passed": False, "blocking": True, "violations": [
                      "stock phrase 'truly special' — cut it: 'Sod Camp is truly special'"]}]}]
    repair = [{"attempt": 1, "gate_targeted": "F9_cta_fact", "repairable": True,
               "violations": ["unsupported: 'a truly special first night'"]}]
    res, _ = _r0(_rule(), pieces=[_piece(text=fs.CLEAN, discarded_attempts=discarded, repair_log=repair)])
    assert res["prevented"]["by_gate"] == {"F15_voice": 1, "F9_cta_fact": 1}
    assert res["prevented"]["blocking"] == 2 and res["prevented"]["piece_ids"] == ["p1"]
    assert res["phrase"]["before"] == 1                    # the discarded attempt's text holds it


def test_the_result_holds_no_text():
    res, _ = _r0(_rule(), [_signal(1)], [_piece()])
    dumped = json.dumps(res, default=str)
    assert "truly special" not in dumped and "Sod Camp" not in dumped
    assert res["rule_sha"] == rp.rule_sha(_rule())


def test_rule_sha_changes_with_the_text_not_the_status():
    r = _rule()
    assert rp.rule_sha({**r, "status": "active"}) == rp.rule_sha(r)
    assert rp.rule_sha({**r, "rule_text": "hidden gem"}) != rp.rule_sha(r)
    assert rp.rule_sha({**r, "replacement": "special"}) != rp.rule_sha(r)


@pytest.mark.parametrize("text, phrase, rep, out", [
    ("It is truly special, really.", "truly special", None, "It is, really."),
    ("A Truly Special night.", "truly special", "special", "A special night."),
    ("truly specialist", "truly special", None, "truly specialist"),
])
def test_counterfactual(text, phrase, rep, out):
    assert rp.counterfactual(text, phrase, rep) == out


def test_a_job_cache_runs_each_stored_text_once_without_the_rule(monkeypatch):
    pieces, cache, calls = [_piece("p1"), _piece("p2", text=fs.CLEAN)], {}, []
    real = rp.PieceReplay.run

    def counting(self, text, **kw):
        calls.append(kw["pack"] is self.pack)
        return real(self, text, **kw)
    monkeypatch.setattr(rp.PieceReplay, "run", counting)
    for rule in (_rule(), _rule("never written")):
        conn = _Conn([], pieces)
        asyncio.run(rp.run_r0(_Pool(conn), rule, now=NOW, cache=cache))
    # 2 rule-off runs (one per stored text, shared) + 2 for the phrase of the first rule (on + counterfactual)
    assert calls.count(True) == 2 and calls.count(False) == 2
