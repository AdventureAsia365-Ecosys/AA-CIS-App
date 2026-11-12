"""AA-1081 (S278) — T7 slate run time: the Debate brand-fit judge with bounded concurrency, and a cache that survives
an atomize which did not change a Segment.

1. `debate_concurrency()` (env `T7_DEBATE_CONCURRENCY`, default 8, 1..32).
2. `assess_candidates()` gives the same signals, in the same order, one at a time and 8 at a time, with a judge whose
   replies finish out of order; the Slate built on them is identical. At most `concurrency` judge calls are in flight,
   the judge runs in a worker thread with the caller's context, the cache writes go through the one connection one at
   a time, and one failing call keeps today's rule (that candidate passes, the others are judged).
3. `export.handler.changed_segments()` / the atomize chain: only a Segment whose label or member atoms changed loses
   its cache rows; an unchanged Segment is a cache hit on the next Slate (no judge call).

Pure: no DB, no LLM. Invented places and ids only.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services.acp_shared import debate
from services.acp_shared.slate import Candidate, _choose_slate
from services.content_generation.brand_fit import BrandFitResult
from services.export import handler

PROFILE = {"brand_core_idea": "slow travel", "brand_customer_segment": "couples", "brand_customer_mindset": "calm",
           "brand_voice_examples": ["unhurried"], "_version": 4}
PLACES = ["Velmora Pass", "Orrin Lake", "Tamsel Ridge", "Caldra Bay", "Ostrel Gorge", "Brennic Falls",
          "Quillon Steppe", "Harrow Dunes", "Lindmar Fjord", "Selvar Caves", "Ithren Valley", "Morrow Peak"]


def _seg(i: int) -> Candidate:
    return Candidate(segment_id=f"seg_{i:02d}", route_id=None, score=float(i % 7) + i / 100, demand=1500,
                     questions=5, said=200, place=PLACES[i % len(PLACES)] + f" {i}", action="walk")


def _score_of(i: int) -> float:
    return [8.5, 6.0, 9.0, 4.5, 7.5, 3.0][i % 6]


class _Conn:
    """One asyncpg-like connection: contested per Segment, a cache read, the evidence read, the cache write. Raises
    when two statements overlap (asyncpg: "another operation is in progress")."""

    def __init__(self, contested=None, cached=None):
        self.contested = contested or {}
        self.cached = cached or {}
        self.busy = False
        self.stored: list[str] = []

    async def _enter(self):
        if self.busy:
            raise RuntimeError("another operation is in progress")
        self.busy = True
        await asyncio.sleep(0)
        self.busy = False

    async def fetchrow(self, sql, *args):
        await self._enter()
        if "FROM acp_contract.atom_segment" in sql:
            return {"contested": self.contested.get(args[0])}
        if "debate_brand_fit_cache" in sql:
            return self.cached.get(args[1])
        return None

    async def fetch(self, sql, *args):
        await self._enter()
        return [{"text": f"Evidence line for {args[0]}."}]

    async def execute(self, sql, *args):
        await self._enter()
        self.stored.append(args[1])
        self.cached[args[1]] = {"judge_score": args[4], "feedback": args[8]}


class _Jev:
    def __init__(self, verdict: str):
        self.verdict = verdict

    def accepted(self, q):
        return self.verdict == "accept"

    def rejected(self, q):
        return self.verdict == "reject"


def _jev_for(candidate) -> str:
    n = int(candidate.segment_id.split("_")[1])
    return "accept" if n % 11 == 5 else "reject" if n % 11 == 7 else "grey"


class _FakeJudge:
    """score_candidate_fit stand-in: a reply time that makes later calls finish first, a score per Segment, one Segment
    that raises. Counts calls in flight (thread safe)."""

    def __init__(self, delay_s=0.02, fail=frozenset()):
        self.delay_s = delay_s
        self.fail = fail
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0
        self.threads: set[str] = set()

    def __call__(self, brand_profile, topic, *, stage):
        assert stage == "t7_debate_judge"
        n = int(topic["place"].rsplit(" ", 1)[1])
        with self.lock:
            self.in_flight += 1
            self.calls += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delay_s * (1 + (n * 7) % 5) / 5)     # later Segments can finish first
            if n in self.fail:
                raise ValueError("judge reply is not JSON")
            score = _score_of(n)
            return BrandFitResult(brand_fit_score=score, cross_brand_distinct=score, mission_present=True,
                                  feedback=f"fb {n}", judge_score=score, model_used="fake-judge", cost_usd=0.0001,
                                  input_tokens=100, output_tokens=20, stop_reason="stop", account=None,
                                  fallback_used=False)
        finally:
            with self.lock:
                self.in_flight -= 1


def _run(candidates, conn, judge, *, concurrency, record=None, billed=None):
    async def prefilter(tenant_id, candidate, brand_profile, topic):
        await asyncio.sleep(0)
        return _Jev(_jev_for(candidate))

    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "t7_prefilter", prefilter), \
         patch.object(debate, "score_candidate_fit", judge), \
         patch.object(debate, "record_call", record or AsyncMock()), \
         patch.object(debate, "record_billed_failure", billed or AsyncMock()):
        return asyncio.run(debate.assess_candidates(uuid4(), candidates, conn, concurrency=concurrency))


# ── 1. the setting ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, value", [(None, 8), ("", 8), ("8", 8), ("1", 1), (" 4 ", 4), ("0", 8), ("-3", 8),
                                        ("many", 8), ("32", 32), ("200", 32)])
def test_debate_concurrency_defaults_to_8_and_stays_within_1_to_32(monkeypatch, raw, value):
    if raw is None:
        monkeypatch.delenv("T7_DEBATE_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("T7_DEBATE_CONCURRENCY", raw)
    assert debate.debate_concurrency() == value


# ── 2. serial vs concurrent ─────────────────────────────────────────────────────────────────────────────────────

def _pool(n=36):
    return [_seg(i) for i in range(n)]


def _fixture_conn():
    # every 9th Segment contested, every 4th already in the cache
    return _Conn(contested={f"seg_{i:02d}": 0.9 for i in range(0, 36, 9)},
                 cached={f"seg_{i:02d}": {"judge_score": 5.0, "feedback": ""} for i in range(2, 36, 4)})


def test_concurrent_signals_and_slate_are_identical_to_the_serial_run():
    cands = _pool()
    serial_conn, conc_conn = _fixture_conn(), _fixture_conn()
    serial = _run(cands, serial_conn, _FakeJudge(fail={13}), concurrency=1)
    conc_judge = _FakeJudge(fail={13})
    concurrent = _run(cands, conc_conn, conc_judge, concurrency=8)

    assert concurrent == serial
    assert {s.brand_fit_source for s in serial} == {"cache", "jev_accept", "jev_reject", "llm", "error", None}
    assert conc_judge.max_in_flight > 1                         # really overlapped
    assert sorted(conc_conn.stored) == sorted(serial_conn.stored)

    for mode in ("rank", "cut"):
        a = debate.apply_signals(cands, serial, mode)
        b = debate.apply_signals(cands, concurrent, mode)
        assert a == b
        channels = ["blog", "facebook", "instagram"]
        assert _choose_slate(channels, a, [], hub_of={}) == _choose_slate(channels, b, [], hub_of={})


def test_judge_calls_in_flight_never_exceed_the_setting():
    judge = _FakeJudge(delay_s=0.03)
    _run(_pool(40), _Conn(), judge, concurrency=3)
    assert 1 < judge.max_in_flight <= 3


def test_the_setting_is_read_when_no_width_is_passed(monkeypatch):
    monkeypatch.setenv("T7_DEBATE_CONCURRENCY", "2")
    judge = _FakeJudge(delay_s=0.03)
    _run(_pool(24), _Conn(), judge, concurrency=None)
    assert judge.max_in_flight == 2


def test_concurrent_run_is_faster_than_serial_with_a_slow_judge():
    """The offline benchmark in miniature: 40 cache misses, 50 ms a judge call."""
    cands = _pool(40)
    t0 = time.perf_counter()
    _run(cands, _Conn(), _FakeJudge(delay_s=0.05), concurrency=1)
    serial_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    _run(cands, _Conn(), _FakeJudge(delay_s=0.05), concurrency=8)
    concurrent_s = time.perf_counter() - t0
    assert concurrent_s < serial_s / 3


def test_a_failing_call_lets_its_candidate_pass_and_the_others_are_still_judged():
    billed = AsyncMock()
    cands = [_seg(i) for i in (1, 2, 3, 4)]          # all grey for Jev
    sigs = _run(cands, _Conn(), _FakeJudge(fail={2}), concurrency=4, billed=billed)
    assert [s.brand_fit_source for s in sigs] == ["llm", "error", "llm", "llm"]
    assert sigs[1].flags == ()
    assert billed.await_count == 1 and billed.await_args.kwargs["stage"] == "t7_debate_judge"


def test_each_judge_call_gets_its_cost_row_with_the_billing_fields():
    record = AsyncMock()
    cands = [_seg(i) for i in (1, 2, 3)]
    _run(cands, _Conn(), _FakeJudge(), concurrency=3, record=record)
    assert record.await_count == 3
    for call in record.await_args_list:
        kw = call.kwargs
        assert kw["stage"] == "t7_debate_judge" and kw["role"] == "judge" and kw["model"] == "fake-judge"
        assert "pricing_mode" in kw and "connection_key" in kw        # **billed_fields(result)


def test_the_judge_runs_off_the_event_loop_with_the_callers_context():
    tenant_var: contextvars.ContextVar = contextvars.ContextVar("aa1081_job_tenant", default=None)
    seen = []

    def judge(brand_profile, topic, *, stage):
        seen.append((tenant_var.get(), threading.current_thread() is threading.main_thread()))
        return _FakeJudge()(brand_profile, topic, stage=stage)

    async def main():
        tenant_var.set("tenant-under-test")
        return await debate.assess_candidates(uuid4(), [_seg(1), _seg(3)], _Conn(), concurrency=2)

    async def prefilter(*a):
        return _Jev("grey")

    with patch.object(debate, "_fetch_brand_profile", AsyncMock(return_value=PROFILE)), \
         patch.object(debate, "has_brand_signals", return_value=True), \
         patch.object(debate, "t7_prefilter", prefilter), patch.object(debate, "score_candidate_fit", judge), \
         patch.object(debate, "record_call", AsyncMock()):
        asyncio.run(main())
    assert seen == [("tenant-under-test", False)] * 2


def test_no_cache_miss_means_no_thread_and_no_judge():
    conn = _Conn(cached={f"seg_{i:02d}": {"judge_score": 8.0, "feedback": ""} for i in range(6)})
    judge = _FakeJudge()
    with patch.object(debate, "ThreadPoolExecutor") as pool:
        sigs = _run(_pool(6), conn, judge, concurrency=8)
    assert judge.calls == 0 and pool.call_count == 0
    assert all(s.brand_fit_source == "cache" for s in sigs)


# ── 3. the cache survives an atomize that changed no Segment ───────────────────────────────────────────────────

STATE = {"seg_a": ("Orrin Lake", "walk", ("atom_1", "atom_2")), "seg_b": ("Caldra Bay", "swim", ("atom_3",))}


def test_changed_segments_names_only_a_changed_label_or_member_set():
    after = dict(STATE, seg_b=("Caldra Bay", "swim", ("atom_3", "atom_9")))
    assert handler.changed_segments(STATE, STATE, ["seg_a", "seg_b"]) == []
    assert handler.changed_segments(STATE, after, ["seg_a", "seg_b"]) == ["seg_b"]
    relabel = dict(STATE, seg_a=("Orrin Lake", "kayak", ("atom_1", "atom_2")))
    assert handler.changed_segments(STATE, relabel, ["seg_a", "seg_b"]) == ["seg_a"]


def test_a_segment_read_on_one_side_only_counts_as_changed():
    after = dict(STATE, seg_new=("Selvar Caves", "explore", ("atom_7",)))
    assert handler.changed_segments(STATE, after, ["seg_a", "seg_b", "seg_new"]) == ["seg_new"]
    assert handler.changed_segments(STATE, {"seg_a": STATE["seg_a"]}, ["seg_a", "seg_b"]) == ["seg_b"]
    assert handler.changed_segments({}, {}, ["seg_x"]) == ["seg_x"]


def _state_rows(state):
    return [{"segment_id": s, "canonical_place": p, "canonical_action": a, "atom_ids": list(ids)}
            for s, (p, a, ids) in state.items()]


class _AtomizeDb:
    """The atomize chain's pool: tour_segment_ids, segment_states before / after the segment stage, the cache DELETE."""

    def __init__(self, before, after):
        self.states = [before, after]
        self.deleted: list = []

    async def fetch(self, sql, *args):
        if "array_agg" in sql:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return [r for r in _state_rows(state) if r["segment_id"] in args[0]]
        return [{"segment_id": s} for s in sorted(STATE)]

    async def execute(self, sql, *args):
        if "debate_brand_fit_cache" in sql:
            self.deleted.append(list(args[0]))
            return "DELETE 1"
        return "OK"

    async def close(self):
        return None


def _atomize(db):
    with patch("services.export.handler.open_job_pool", AsyncMock(return_value=db)), \
         patch("services.acp_contract.a3_atomize.run_a3_atomize",
               AsyncMock(return_value={"status": "success", "atom_count": 3})), \
         patch("services.recompute.run_stages", AsyncMock(return_value={})), \
         patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("job-1", True))), \
         patch("services.acp_shared.place_cards.relink_tour_place_cards", AsyncMock(return_value={})), \
         patch("services.places.enqueue.after_atomize", AsyncMock(return_value={})):
        return asyncio.run(handler._run_a3_atomize_background(
            tour_id="00000000-0000-0000-0000-00000000a081", rewritten={}, country="Elsewhere", version_id="v2"))


def test_a_reread_that_gives_the_same_atoms_keeps_every_cache_row():
    """The S278 case: a new version of the tour, every day re-read (status success), the same atoms come back."""
    db = _AtomizeDb(STATE, STATE)
    out = _atomize(db)
    assert db.deleted == [] and out["brand_fit_segments_changed"] == 0 and out["brand_fit_cache_cleared"] == 0


def test_a_reread_that_changes_one_segment_clears_only_that_one():
    db = _AtomizeDb(STATE, dict(STATE, seg_a=("Orrin Lake", "walk", ("atom_1", "atom_5"))))
    out = _atomize(db)
    assert db.deleted == [["seg_a"]] and out["brand_fit_segments_changed"] == 1


def test_unreadable_segment_states_fall_back_to_clearing_every_segment_of_the_tour():
    db = _AtomizeDb(STATE, STATE)
    with patch("services.export.handler.segment_states", AsyncMock(side_effect=OSError("db gone"))):
        out = _atomize(db)
    assert db.deleted == [["seg_a", "seg_b"]] and out["segment_score_route"] == "ok"


def test_an_unchanged_segment_is_a_cache_hit_on_the_next_slate():
    """End to end in miniature: Slate 1 judges and stores, an atomize re-read changes nothing, Slate 2 calls no
    judge (S278: 319-326 judge calls per refresh, none served from the cache)."""
    cache_conn = _Conn()
    cands = [_seg(i) for i in (1, 2, 3, 4)]               # all grey for Jev: four judge calls
    first = _FakeJudge()
    sigs_1 = _run(cands, cache_conn, first, concurrency=8)
    assert first.calls == 4 and sorted(cache_conn.stored) == [c.segment_id for c in cands]

    db = _AtomizeDb(STATE, STATE)
    _atomize(db)
    for seg_ids in db.deleted:                             # what the atomize dropped (nothing here)
        for s in seg_ids:
            cache_conn.cached.pop(s, None)

    second = _FakeJudge()
    sigs_2 = _run(cands, cache_conn, second, concurrency=8)
    assert second.calls == 0
    assert [s.brand_fit_source for s in sigs_2] == ["cache"] * 4
    assert [s.judge_score for s in sigs_2] == [s.judge_score for s in sigs_1]


# ── 4. the $0 offline benchmark (scripts/quality/bench_aa1081_debate_concurrency.py) ───────────────────────────

def _bench():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "quality" / "bench_aa1081_debate_concurrency.py"
    spec = importlib.util.spec_from_file_location("bench_aa1081_debate_concurrency", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_benchmark_shows_the_drop_and_the_same_signals():
    out = _bench().run_benchmark(n=24, widths=(1, 8), reply_s=2.2, scale=0.005)
    serial, wide = out["runs"]
    assert serial["same_signals"] and wide["same_signals"]
    assert wide["wall_s"] < serial["wall_s"] / 3 and wide["speedup"] > 3
