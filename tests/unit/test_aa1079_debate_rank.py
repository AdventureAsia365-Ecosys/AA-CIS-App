"""AA-1079 (S277) — Debate is advisory, rank only, until the brand-fit judge is calibrated.

services/acp_shared/debate.py: `debate_mode()` (T7_DEBATE_MODE, rank when unset), `assess_candidates()` (the
judgement, nothing cut; `cache_only` for the $0 replay), `apply_signals()` (rank keeps everyone and attaches the
flags, cut = the AA-631 cap), `debate_report()` (the t7_slate job result); services/acp_shared/slate.py orders
flagged candidates after unflagged ones. Pure: no DB, no LLM. Invented places only.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import debate, slate
from services.acp_shared.debate import DebateSignal
from services.acp_shared.slate import Candidate, _choose_slate


def _seg(i: int, place: str | None = None) -> Candidate:
    return Candidate(segment_id=f"seg_{i}", route_id=None, score=float(i), demand=1500, questions=5, said=200,
                     place=place or f"Velmora Pass {i}", action="walk")


# ── the setting ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, mode", [(None, "rank"), ("", "rank"), ("rank", "rank"), ("cut", "cut"),
                                       (" CUT ", "cut"), ("drop", "rank"), ("enforce", "rank")])
def test_debate_mode_defaults_to_rank_and_only_an_explicit_cut_cuts(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv("T7_DEBATE_MODE", raising=False)
    else:
        monkeypatch.setenv("T7_DEBATE_MODE", raw)
    assert debate.debate_mode() == mode


# ── apply_signals (pure) ────────────────────────────────────────────────────────────────────────────────────────

FLAG_BF = DebateSignal(flags=("brand_fit",), severity=4.0, judge_score=6.0)
FLAG_CT = DebateSignal(flags=("contested",), severity=0.9, contested=0.9)
PASS = DebateSignal(judge_score=8.0)


def test_rank_keeps_every_candidate_in_order_and_attaches_the_flags():
    cands = [_seg(1), _seg(2), _seg(3), _seg(4)]
    out = debate.apply_signals(cands, [FLAG_BF, PASS, FLAG_CT, FLAG_BF], "rank")
    assert [c.segment_id for c in out] == ["seg_1", "seg_2", "seg_3", "seg_4"]
    assert [c.debate_flags for c in out] == [("brand_fit",), (), ("contested",), ("brand_fit",)]
    assert out[0].debate_severity == 4.0 and out[1].debate_severity is None
    assert out[1] is cands[1]                      # an unflagged candidate is the same object


def test_rank_drops_nothing_even_when_every_candidate_is_flagged():
    """The S276 shape: 499 of 502 flagged. Rank mode keeps all of them."""
    cands = [_seg(i) for i in range(10)]
    out = debate.apply_signals(cands, [FLAG_BF] * 10, "rank")
    assert len(out) == 10 and all(c.debate_flags for c in out)


def test_cut_is_the_aa631_cap_worst_first_order_kept():
    cands = [_seg(1), _seg(2), _seg(3), _seg(4)]
    sigs = [DebateSignal(flags=("brand_fit",), severity=1.0), DebateSignal(flags=("brand_fit",), severity=5.0),
            DebateSignal(flags=("brand_fit",), severity=3.0), PASS]
    out = debate.apply_signals(cands, sigs, "cut")
    assert [c.segment_id for c in out] == ["seg_1", "seg_4"]   # cap 2 of 4: the two worst (seg_2, seg_3) go
    assert all(c.debate_flags == () for c in out)              # cut mode attaches nothing (pre-AA-1079 shape)
    assert debate.would_cut(sigs) == 2


def test_apply_signals_needs_one_signal_per_candidate():
    with pytest.raises(ValueError):
        debate.apply_signals([_seg(1)], [], "rank")


# ── the Slate order ─────────────────────────────────────────────────────────────────────────────────────────────

def test_choose_slate_takes_unflagged_candidates_before_flagged_ones():
    cands = debate.apply_signals([_seg(1), _seg(2), _seg(3)], [FLAG_BF, PASS, FLAG_CT], "rank")
    chosen = _choose_slate(["facebook"], cands, [], hub_of={})
    assert [c.segment_id for c, _ in chosen["facebook"]] == ["seg_2", "seg_1", "seg_3"]


def test_choose_slate_order_is_unchanged_when_nothing_is_flagged():
    cands = [_seg(3), _seg(1), _seg(2)]
    chosen = _choose_slate(["facebook"], cands, [], hub_of={})
    assert [c.segment_id for c, _ in chosen["facebook"]] == ["seg_1", "seg_2", "seg_3"]


def test_a_flagged_candidate_still_wins_a_place_no_unflagged_one_has():
    """Rank only: a flagged Segment is the place's only candidate, so it still gets the seat (it would be cut before)."""
    cands = debate.apply_signals([_seg(1, place="Orrin Lake"), _seg(2, place="Tamsel Ridge")], [FLAG_BF, PASS],
                                 "rank")
    chosen = _choose_slate(["blog"], cands, [], hub_of={})
    assert {c.place for c, _ in chosen["blog"]} == {"Orrin Lake", "Tamsel Ridge"}


# ── debate_report (pure) ────────────────────────────────────────────────────────────────────────────────────────

def test_report_counts_flags_and_lists_the_worst_first():
    before = [_seg(1), _seg(2), _seg(3)]
    after = debate.apply_signals(before, [FLAG_CT, PASS, FLAG_BF], "rank")
    rep = debate.debate_report("rank", before, after)
    assert rep["candidates"] == 3 and rep["kept"] == 3 and rep["dropped"] == 0 and rep["flagged"] == 2
    assert rep["flagged_by"] == {"brand_fit": 1, "contested": 1}
    assert [s["id"] for s in rep["signals"]] == ["seg_3", "seg_1"]          # severity 4.0 before 0.9


def test_report_in_cut_mode_shows_what_was_dropped():
    before = [_seg(i) for i in range(4)]
    after = debate.apply_signals(before, [FLAG_BF, FLAG_BF, PASS, PASS], "cut")
    rep = debate.debate_report("cut", before, after)
    assert rep["dropped"] == 2 and rep["flagged"] == 0 and rep["signals"] == []


def test_report_caps_the_listed_signals_but_counts_all(monkeypatch):
    monkeypatch.setattr(debate, "REPORT_SIGNALS_MAX", 2)
    before = [_seg(i) for i in range(5)]
    rep = debate.debate_report("rank", before, debate.apply_signals(before, [FLAG_BF] * 5, "rank"))
    assert rep["flagged"] == 5 and len(rep["signals"]) == 2


# ── apply_debate / assess_candidates ────────────────────────────────────────────────────────────────────────────

PROFILE = {"brand_core_idea": "slow travel", "brand_customer_segment": "couples", "brand_customer_mindset": "calm",
           "brand_voice_examples": ["unhurried"], "_version": 2}


def _conn(contested=None):
    async def fetchrow(sql, *args):
        if "atom_segment" in sql:
            return {"contested": contested}
        return None
    return type("C", (), {"fetchrow": AsyncMock(side_effect=fetchrow)})()


def test_apply_debate_in_rank_mode_keeps_a_low_brand_fit_candidate(monkeypatch):
    monkeypatch.delenv("T7_DEBATE_MODE", raising=False)
    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "_cached_brand_fit", AsyncMock(return_value={"judge_score": 5.0, "feedback": ""})):
        out = asyncio.run(debate.apply_debate(uuid4(), [_seg(1), _seg(2)], _conn()))
    assert len(out) == 2 and all(c.debate_flags == ("brand_fit",) for c in out)
    assert out[0].debate_severity == pytest.approx(5.0)


def test_cache_only_assessment_never_asks_jev_or_the_llm():
    decide, judge, store, log = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    cache = AsyncMock(side_effect=[{"judge_score": 6.0, "feedback": ""}, None])
    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "_cached_brand_fit", cache), \
         patch.object(debate, "decide", decide), patch.object(debate, "score_candidate_fit", judge), \
         patch.object(debate, "_store_brand_fit_cache", store), patch.object(debate, "record_call", log):
        sigs = asyncio.run(debate.assess_candidates(uuid4(), [_seg(1), _seg(2)], _conn(), cache_only=True,
                                                    any_brand_version=True))
    assert [s.brand_fit_source for s in sigs] == ["cache", "cache_miss"]
    assert sigs[0].flags == ("brand_fit",) and sigs[1].flags == ()
    assert decide.await_count == 0 and judge.call_count == 0 and store.await_count == 0 and log.await_count == 0
    assert all(call.kwargs.get("any_brand_version") is True for call in cache.await_args_list)


def test_contested_flags_before_brand_fit_is_asked():
    cache = AsyncMock()
    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "_cached_brand_fit", cache):
        sigs = asyncio.run(debate.assess_candidates(uuid4(), [_seg(1)], _conn(contested=0.95)))
    assert sigs[0].flags == ("contested",) and cache.await_count == 0


def test_a_cache_read_error_passes_the_candidate_and_bills_nothing_in_a_replay():
    billed = AsyncMock()
    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "_cached_brand_fit", AsyncMock(side_effect=OSError("down"))), \
         patch.object(debate, "record_billed_failure", billed):
        sigs = asyncio.run(debate.assess_candidates(uuid4(), [_seg(1)], _conn(), cache_only=True))
    assert sigs[0].flags == () and sigs[0].brand_fit_source == "error" and billed.await_count == 0


# ── propose_slate: the job result carries the report ────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self):
        self.executed = []

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_propose_slate_records_the_debate_report_and_proposes_flagged_candidates(monkeypatch):
    monkeypatch.delenv("T7_DEBATE_MODE", raising=False)
    segments = [_seg(1), _seg(2)]

    async def fake_debate(_tid, candidates, _conn):
        return debate.apply_signals(candidates, [FLAG_BF] * len(candidates), debate.debate_mode())

    conn = _FakeConn()
    with patch.object(slate, "_tenant_market_codes", AsyncMock(return_value=(["US"], []))), \
         patch.object(slate, "_tenant_enabled_channels", AsyncMock(return_value=["facebook"])), \
         patch.object(slate, "_fetch_thin_walk_segments", AsyncMock(return_value={})), \
         patch.object(slate, "_fetch_held_subjects", AsyncMock(return_value={})), \
         patch.object(slate, "_segments_held_via_alias", AsyncMock(return_value=set())), \
         patch.object(slate, "_fetch_segment_candidates", AsyncMock(return_value=segments)), \
         patch.object(slate, "_fetch_route_candidates", AsyncMock(return_value=[])), \
         patch.object(slate, "_fetch_segment_hub_map", AsyncMock(return_value={})), \
         patch("services.acp_shared.debate.apply_debate", fake_debate):
        result = asyncio.run(slate.propose_slate(uuid4(), _FakePool(conn)))
    assert result["debate"]["mode"] == "rank"
    assert result["debate"]["segments"]["flagged"] == 2 and result["debate"]["segments"]["dropped"] == 0
    assert result["proposed_per_channel"]["facebook"] == 2
    inserted = [a for s, a in conn.executed if s.lstrip().startswith("INSERT INTO acp_shared.subject")]
    assert len(inserted) == 2
    assert all("debate" not in a[3] and "brand_fit" not in a[3] for a in inserted)   # tenant-visible reason stays
