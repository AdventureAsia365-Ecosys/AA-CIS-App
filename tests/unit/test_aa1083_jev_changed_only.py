"""AA-1083 — a recompute asks Jev only about the Segments whose inputs changed, estimates its Jev spend first, is held
above a per-run cap until staff release it, and reports what it really spent.

* the Score stage (run_atom_ranking) of a recompute after one atomized tour asks Jev only about that tour's Segments
  (a3_segment_type + a3_demand), every other Segment keeps its stored verdicts;
* replay: with every verdict stored, the scoped Score writes exactly the rows the old full pass wrote;
* a Segment outside the scope with a newly bought keyword keeps the demand of its current Score row; one with no
  current row is asked;
* the PAA landing filters (a3_question_scope / a3_question_landing): a pair only out-of-scope Segments hold is read
  from stored verdicts (unverified = does not count), a pair an in-scope Segment holds is asked once;
* the stage wiring (RecomputeScope.jev_changed_only, the switch RECOMPUTE_JEV_SCOPE);
* the estimate counts the same new questions the run then asks; the cap holds, release runs, alert-only never holds;
* the recompute job: a held run ends stopped_budget with the estimate in its result and one bell; a released run is
  not held and carries its Jev spend; a released job is never folded into by a later enqueue.

No DB, no Jev: decide() and the stored-verdict read are faked over one in-memory verdict table. Places and keywords are
invented. Real Postgres for the SQL: tests/integration/test_aa1083_jev_recompute_sql.py.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import atom_ranking as ar
from services.jobs import recompute_job as rc
from services.recompute import RecomputeScope, jev_scope, run_stages
from services.recompute.errors import JEV_CAP_HELD
from shared.cost_guard import BudgetExceeded
from shared.llm_client.decide import Decision, Verdict
from tests.unit.jobs.test_aa1056_recompute_lock_and_sweep import _ctx, _job_pool, locks  # noqa: F401 (fixture)

T1, T2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

# Tour T1 (the atomized one) holds s1 + s2; tour T2 holds s3 + s4 (+ a disputed moment s5).
SEGMENTS = [
    {"segment_id": "s1", "canonical_place": "Velmora Falls", "canonical_action": "swim", "tour_ids": [T1],
     "said": 120, "activity_types": ["experience"]},
    {"segment_id": "s2", "canonical_place": "Ostrel Market", "canonical_action": "browse", "tour_ids": [T1],
     "said": 80, "activity_types": ["experience"]},
    {"segment_id": "s3", "canonical_place": "Taldrin Ridge", "canonical_action": "hike", "tour_ids": [T2],
     "said": 200, "activity_types": ["experience"]},
    {"segment_id": "s4", "canonical_place": "Brisk Lagoon", "canonical_action": "kayak", "tour_ids": [T2],
     "said": 60, "activity_types": ["experience"]},
    # the rule says transit, the atoms say experience: a disputed moment (a3_segment_type)
    {"segment_id": "s5", "canonical_place": "Corvane Harbour", "canonical_action": "transfer to the pier",
     "tour_ids": [T2], "said": 30, "activity_types": ["experience"]},
]
DEMAND = [("velmora falls", "US", 4000), ("velmora falls swimming", "US", 900), ("ostrel market food", "US", 300),
          ("taldrin ridge trail", "US", 700), ("taldrin ridge hike", "US", 500), ("brisk lagoon kayak", "US", 250)]
COUNTS = {"s1": 3, "s2": 1, "s3": 4, "s4": 0}


def _v(q, zone="accept", mode="enforce", choice=None):
    return Verdict(q, mode, zone, choice=choice)


def _dec(q, zone="accept", choice=None):
    return Decision(verdicts={q: _v(q, zone, choice=choice)})


def _moment(seg_id):
    s = next(r for r in SEGMENTS if r["segment_id"] == seg_id)
    return ar.landing_moment(s["canonical_place"], s["canonical_action"])


def _subjects_of(seg_ids):
    """Every decide() subject key a Segment can be asked under (type + demand)."""
    keys = set()
    for sid in seg_ids:
        m = _moment(sid)
        keys.add(ar.type_subject_key(m))
        s = next(r for r in SEGMENTS if r["segment_id"] == sid)
        keys |= {ar.demand_subject_key(m, kw) for kw, _v in
                 ar.demand_candidates(s["canonical_place"], s["canonical_action"], DEMAND, "US")}
    return keys


class Verdicts:
    """One in-memory decision table: decide() answers from it (and records the ask), stored_decisions() reads it for
    the subjects already `stored` (the decide() cache), nothing else."""

    def __init__(self, answers: dict[str, Decision], stored: set[str]):
        self.answers, self.stored, self.asked = answers, set(stored), []

    def answer(self, subject, keys):
        if subject in self.answers:
            return self.answers[subject]
        return Decision(verdicts={k: _v(k, "grey") for k in keys})      # Jev unsure: acts on nothing

    async def decide(self, stage, subject, state, keys, **kw):
        self.asked.append((stage, subject))
        self.stored.add(subject)
        return self.answer(subject, keys)

    async def stored_decisions(self, pool, question_keys, subject_keys):
        return {k: (self.answer(k, question_keys) if k in self.stored else None) for k in set(subject_keys)}

    async def cached_type_picks(self, pool, moments):
        out = []
        for m in moments:
            key = ar.type_subject_key(m)
            out.append((m, self.answer(key, [ar.TYPE_Q]).choice(ar.TYPE_Q) if key in self.stored else None))
        return out

    def patches(self):
        return (patch.object(ar, "decide", new=self.decide),
                patch.object(ar, "stored_decisions", new=self.stored_decisions),
                patch.object(ar, "_cached_type_picks", new=self.cached_type_picks))


class FakeDb:
    """The reads and writes run_atom_ranking makes for one market."""

    def __init__(self, previous: dict[str, int | None]):
        self.previous = previous
        self.written: list[tuple] = []

    def pool(self):
        db = self

        class Conn:
            async def fetch(self, sql, *args):
                if sql is ar.PREVIOUS_DEMAND_SQL:
                    return [{"segment_id": k, "demand_volume": v} for k, v in db.previous.items()]
                if "array_agg(DISTINCT ta.tour_id)" in sql:
                    return [dict(r) for r in SEGMENTS]
                if "FROM acp_contract.search_demand" in sql:
                    return [{"keyword": k, "market": m, "search_volume": v} for k, m, v in DEMAND]
                raise AssertionError(sql)

            async def fetchval(self, sql, *args):
                return 2

            async def execute(self, sql, *args):
                return "OK"

            async def executemany(self, sql, rows):
                db.written.extend(sorted(rows))

            @asynccontextmanager
            async def transaction(self):
                yield

        pool = MagicMock()

        @asynccontextmanager
        async def acquire():
            yield Conn()

        pool.acquire = acquire
        return pool


def _all_answers():
    """Jev's answers for this catalogue: s5's moment is an experience; the bare 'velmora falls' keyword is not owned by
    the swim moment (enforced reject), the next one is; every other first candidate is owned."""
    answers = {ar.type_subject_key(_moment("s5")): _dec(ar.TYPE_Q, choice="experience")}
    answers[ar.demand_subject_key(_moment("s1"), "velmora falls")] = _dec(ar.DEMAND_Q, "reject")
    for sid in ("s1", "s2", "s3", "s4", "s5"):
        s = next(r for r in SEGMENTS if r["segment_id"] == sid)
        for kw, _vol in ar.demand_candidates(s["canonical_place"], s["canonical_action"], DEMAND, "US"):
            answers.setdefault(ar.demand_subject_key(_moment(sid), kw), _dec(ar.DEMAND_Q, "accept"))
    return answers


async def _rank(verdicts, previous, jev_ids):
    db = FakeDb(previous)
    p1, p2, p3 = verdicts.patches()
    with p1, p2, p3:
        out = await ar.run_atom_ranking("US", db.pool(), COUNTS, jev_segment_ids=jev_ids)
    return db, out


# ── Score: one atomized tour -> Jev only about that tour's Segments ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_recompute_after_one_atomized_tour_asks_jev_only_about_that_tours_segments():
    answers = _all_answers()
    # T2's Segments were judged before (stored); a keyword newly bought for s3 has no verdict yet.
    stored = _subjects_of(["s3", "s4", "s5"]) - {ar.demand_subject_key(_moment("s3"), "taldrin ridge hike")}
    verdicts = Verdicts(answers, stored)
    previous = {"s1": 900, "s2": 300, "s3": 700, "s4": 250, "s5": None}
    db, out = await _rank(verdicts, previous, {"s1", "s2"})

    asked = {subject for _stage, subject in verdicts.asked}
    assert asked and asked <= _subjects_of(["s1", "s2"])
    assert not asked & _subjects_of(["s3", "s4", "s5"])
    assert out["segments_ranked"] == 5 and out["segments_excluded"] == 0     # s5 kept by its stored pick


@pytest.mark.asyncio
async def test_the_old_full_pass_still_asks_about_every_segment():
    verdicts = Verdicts(_all_answers(), stored=set())
    await _rank(verdicts, {}, None)
    asked = {subject for _stage, subject in verdicts.asked}
    assert asked & _subjects_of(["s3"]) and asked & _subjects_of(["s5"])


@pytest.mark.asyncio
async def test_replay_unchanged_segments_score_exactly_as_the_full_pass():
    """Every verdict stored (the state after the last full pass): the scoped run writes the same rows and asks only
    about the atomized tour's Segments, which the decide() cache would have answered for free anyway."""
    answers = _all_answers()
    full = Verdicts(answers, stored=set(answers))
    full_db, _ = await _rank(full, {}, None)
    scoped = Verdicts(answers, stored=set(answers))
    previous = {r[2]: r[9] for r in full_db.written}            # segment_id -> demand_volume of the full pass
    scoped_db, _ = await _rank(scoped, previous, {"s1"})
    assert scoped_db.written == full_db.written
    assert {s for _st, s in scoped.asked} <= _subjects_of(["s1"])


@pytest.mark.asyncio
async def test_out_of_scope_demand_walks_stored_verdicts_then_keeps_its_current_row():
    answers = _all_answers()
    m3, m4 = _moment("s3"), _moment("s4")
    answers[ar.demand_subject_key(m3, "taldrin ridge trail")] = _dec(ar.DEMAND_Q, "reject")
    # s3: stored reject of its first keyword, no verdict for the second -> keeps its current Score demand (700)
    # s4: every candidate stored -> claimed at today's volume (as the full pass would)
    stored = {ar.demand_subject_key(m3, "taldrin ridge trail"), ar.demand_subject_key(m4, "brisk lagoon kayak")}
    verdicts = Verdicts(answers, stored)
    segs = [("s3", "Taldrin Ridge", "hike"), ("s4", "Brisk Lagoon", "kayak")]
    with verdicts.patches()[0], verdicts.patches()[1]:
        got = await ar.resolve_demand(None, segs, DEMAND, "US", ask_ids={"s1"}, previous={"s3": 700, "s4": 9})
    assert got == {"s3": {"US": 700}, "s4": {"US": 250}} and verdicts.asked == []


@pytest.mark.asyncio
async def test_out_of_scope_demand_with_no_current_row_is_asked():
    verdicts = Verdicts(_all_answers(), stored=set())
    with verdicts.patches()[0], verdicts.patches()[1]:
        got = await ar.resolve_demand(None, [("s4", "Brisk Lagoon", "kayak")], DEMAND, "US", ask_ids={"s1"},
                                      previous={})
    assert got == {"s4": {"US": 250}}
    assert verdicts.asked == [(ar.DEMAND_STAGE, ar.demand_subject_key(_moment("s4"), "brisk lagoon kayak"))]


@pytest.mark.asyncio
async def test_a_disputed_moment_outside_the_scope_keeps_its_stored_pick():
    answers = _all_answers()
    key5 = ar.type_subject_key(_moment("s5"))
    rows = [dict(r) for r in SEGMENTS]
    for stored, expected in ((set(), "transit"), ({key5}, None)):
        verdicts = Verdicts(answers, stored)
        p1, _p2, p3 = verdicts.patches()
        with p1, p3:
            out = await ar.resolve_exclusions(None, rows, ask_ids={"s1"})
        assert verdicts.asked == [] and out["s5"] == expected       # no stored pick = the rule's answer
    verdicts = Verdicts(answers, set())
    p1, _p2, p3 = verdicts.patches()
    with p1, p3:
        out = await ar.resolve_exclusions(None, rows, ask_ids={"s5"})
    assert verdicts.asked == [(ar.TYPE_STAGE, key5)] and out["s5"] is None


# ── PAA landing: stored-only Segments ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_landing_filters_ask_only_pairs_an_in_scope_segment_holds():
    rows = [{"segment_id": "s1", "canonical_place": "Velmora Falls", "canonical_action": "swim",
             "countries": ["Zentoria"]},
            {"segment_id": "s3", "canonical_place": "Taldrin Ridge", "canonical_action": "hike",
             "countries": ["Zentoria"]},
            {"segment_id": "s4", "canonical_place": "Brisk Lagoon", "canonical_action": "kayak",
             "countries": ["Zentoria"]}]
    shared_q, stored_q, new_q = "Is Zentoria safe to visit?", "How tall is Taldrin Ridge?", "Can you kayak at Brisk?"
    cands = {"s1": {shared_q}, "s3": {shared_q, stored_q}, "s4": {new_q}}
    scope_key = ar.scope_subject_key
    answers = {scope_key("Zentoria", q): Decision(verdicts={ar.SCOPE_FOREIGN_Q: _v(ar.SCOPE_FOREIGN_Q, "grey"),
                                                            ar.SCOPE_HERE_Q: _v(ar.SCOPE_HERE_Q, "grey")})
               for q in (shared_q, stored_q, new_q)}
    verdicts = Verdicts(answers, stored={scope_key("Zentoria", stored_q)})
    p1, p2, _p3 = verdicts.patches()
    with p1, p2:
        stats = await ar.filter_candidates_by_country_scope(None, rows, cands, stored_only=frozenset({"s3", "s4"}))
    # the shared pair is asked once (s1 is in scope); s3's own pair is stored; s4's new pair is unverified -> dropped
    assert verdicts.asked == [(ar.SCOPE_STAGE, scope_key("Zentoria", shared_q))]
    assert cands == {"s1": {shared_q}, "s3": {shared_q, stored_q}, "s4": set()}
    assert stats == {"scope_pairs": 3, "scope_dropped": 1, "scope_asked": 1}

    land = ar.landing_subject_key
    answers = {land(_moment("s3"), stored_q): _dec(ar.LANDING_Q, "reject")}
    verdicts = Verdicts(answers, stored={land(_moment("s3"), stored_q)})
    p1, p2, _p3 = verdicts.patches()
    with p1, p2:
        stats = await ar.filter_candidates_by_landing(None, rows, cands, stored_only=frozenset({"s3", "s4"}))
    # (s3, shared) has no stored landing verdict -> dropped for s3 only; (s3, stored) stored reject -> dropped
    assert verdicts.asked == [(ar.LANDING_STAGE, land(_moment("s1"), shared_q))]
    assert cands == {"s1": {shared_q}, "s3": set(), "s4": set()}
    assert stats["landing_asked"] == 1


@pytest.mark.asyncio
async def test_stored_decisions_reads_active_questions_in_chunks(monkeypatch):
    seen = []

    async def platform_verdicts(conn, qkeys, subjects):
        seen.append(len(subjects))
        return {(q, s): _v(q, "reject") for q in qkeys for s in subjects if s != "k-missing"}

    async def active_questions(conn, qkeys):
        return [q for q in qkeys if q != "off_q"]

    monkeypatch.setattr(ar, "STORED_LOOKUP_CHUNK", 2)
    pool = FakeDb({}).pool()
    with patch("shared.llm_client.decide.platform_verdicts", new=platform_verdicts), \
            patch("shared.llm_client.decide.active_questions", new=active_questions):
        out = await ar.stored_decisions(pool, [ar.LANDING_Q, "off_q"], ["k1", "k2", "k3", "k-missing", "k1"])
    assert seen == [2, 2]
    assert out["k-missing"] is None and out["k1"].rejected(ar.LANDING_Q) and set(out["k2"].verdicts) == {ar.LANDING_Q}


# ── stage wiring ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("changed_only, expected", [(True, {"s1", "s2", "c9"}), (False, None)])
async def test_landing_and_score_get_the_jev_scope(changed_only, expected):
    seen = {}

    async def landing(pool, segment_ids, progress, *, jev_segment_ids=None):
        seen["landing"] = jev_segment_ids
        return {}

    async def ranking(market, pool, counts, *, jev_segment_ids=None):
        seen.setdefault("score", []).append(jev_segment_ids)
        return {}

    with patch("services.acp_contract.atom_ranking.precompute_question_landings", landing), \
            patch("services.acp_contract.atom_ranking.run_atom_ranking", ranking), \
            patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1, "UK": 2}), \
            patch("services.recompute.segment_catchup.run_segment_catchup",
                  AsyncMock(return_value={"segment_ids": ["c9"]})):
        out = await run_stages(["segment_catchup", "landing", "score"],
                               RecomputeScope(segment_ids={"s1", "s2"}, jev_changed_only=changed_only), pool=object())
    assert seen["landing"] == expected and seen["score"] == [expected, expected]
    assert out["landing"]["jev_segments"] == (None if expected is None else 3)


@pytest.mark.parametrize("value, on", [(None, True), ("changed", True), ("all", False), ("OFF", False), ("x", True)])
def test_the_switch(value, on):
    env = {} if value is None else {jev_scope.SWITCH_ENV: value}
    assert jev_scope.scope_enabled(env) is on


# ── estimate, cap, release ─────────────────────────────────────────────────────────────────────────────────────────

def test_hold_reason():
    est = jev_scope.Estimate(new_calls={"a3_demand": 30_000}, unit_usd={"a3_demand": 0.00002})
    assert round(est.usd, 4) == 0.6 and est.calls == 30_000
    reason = jev_scope.hold_reason(est, 0.5, True, False)
    assert reason.startswith(f"{JEV_CAP_HELD}: ") and "$0.60" in reason and "$0.50" in reason
    assert jev_scope.hold_reason(est, 0.5, True, released=True) is None       # staff released it
    assert jev_scope.hold_reason(est, 0.5, False, False) is None              # alert-only row
    assert jev_scope.hold_reason(est, None, True, False) is None              # no cap
    assert jev_scope.hold_reason(est, 0.6, True, False) is None               # at the cap runs
    d = est.as_dict()
    assert d["new_calls"] == 30_000 and d["by_stage"]["a3_demand"]["usd"] == 0.6


@pytest.mark.asyncio
async def test_plan_run_holds_above_the_cap_and_a_release_runs_it():
    big = jev_scope.Estimate(new_calls={"a3_question_landing": 40_000}, unit_usd={"a3_question_landing": 0.000016})
    with patch.object(jev_scope, "read_cap", AsyncMock(return_value=(0.5, True))), \
            patch.object(jev_scope, "estimate", AsyncMock(return_value=big)) as est:
        held = await jev_scope.plan_run(None, scope="platform", tour_id=None, segment_ids=["s1"], released=None)
        ran = await jev_scope.plan_run(None, scope="platform", tour_id=None, segment_ids=["s1"],
                                       released={"by": "admin:u1", "estimate_usd": 0.64})
    assert held["held"].startswith(JEV_CAP_HELD) and held["estimate"]["usd"] == 0.64 and held["cap_usd"] == 0.5
    assert "held" not in ran and ran["released"]["by"] == "admin:u1"
    assert est.await_args.kwargs == {"segment_ids": {"s1"}, "jev_changed_only": True}


@pytest.mark.asyncio
async def test_plan_run_scopes_a_tour_run_to_its_segments_and_never_blocks_on_a_failed_estimate():
    with patch.object(jev_scope, "read_cap", AsyncMock(return_value=(0.5, True))), \
            patch.object(jev_scope, "tour_segment_scope", AsyncMock(return_value={"s1", "s2"})), \
            patch.object(jev_scope, "estimate", AsyncMock(return_value=jev_scope.Estimate())) as est:
        out = await jev_scope.plan_run(None, scope="tour", tour_id=T1, segment_ids=None, released=None)
    assert est.await_args.kwargs["segment_ids"] == {"s1", "s2"} and "held" not in out
    with patch.object(jev_scope, "read_cap", AsyncMock(side_effect=RuntimeError("db gone"))):
        out = await jev_scope.plan_run(None, scope="platform", tour_id=None, segment_ids=None, released=None)
    assert "held" not in out and out["estimate_error"].startswith("RuntimeError")


@pytest.mark.asyncio
async def test_the_estimate_counts_the_questions_the_run_then_asks(monkeypatch):
    """Score side (type + demand) for one market: the estimate of new questions equals what the run asks."""
    answers = _all_answers()
    stored = _subjects_of(["s3", "s4", "s5"]) - {ar.demand_subject_key(_moment("s3"), "taldrin ridge hike")}
    previous = {"s1": 900, "s3": 700, "s4": 250, "s5": None}           # s2 is new to the Score

    class Conn:
        async def fetch(self, sql, *args):
            if sql is ar.LANDING_SEGMENTS_SQL:
                return [{**r, "questions_count": 0, "countries": ["Zentoria"], "atom_ids": []} for r in SEGMENTS]
            if sql is ar.PAA_ROWS_SQL:
                return []
            if sql is jev_scope.DEMAND_ROWS_SQL:
                return [{"keyword": k, "market": m, "search_volume": v} for k, m, v in DEMAND]
            if sql is ar.PREVIOUS_DEMAND_SQL:
                return [{"segment_id": k, "demand_volume": v} for k, v in previous.items()]
            raise AssertionError(sql)

    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield Conn()

    pool.acquire = acquire
    verdicts = Verdicts(answers, stored)
    p1, p2, p3 = verdicts.patches()
    monkeypatch.setattr("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1})
    with p1, p2, p3, patch.object(jev_scope, "unit_costs", AsyncMock(return_value={})):
        est = await jev_scope.estimate(pool, segment_ids={"s1"}, jev_changed_only=True)
    assert verdicts.asked == []                                             # the estimate never asks
    db = FakeDb(previous)
    with p1, p2, p3:
        await ar.run_atom_ranking("US", db.pool(), COUNTS, jev_segment_ids={"s1"})
    asked = {s for _st, s in verdicts.asked}
    by_stage = {st: len({s for x, s in verdicts.asked if x == st}) for st in (ar.TYPE_STAGE, ar.DEMAND_STAGE)}
    assert est.new_calls[ar.TYPE_STAGE] == by_stage[ar.TYPE_STAGE] == 0
    # s1 walks 'velmora falls' (rejected) then 'velmora falls swimming'; the estimate counts the first ask of a walk
    assert est.new_calls[ar.DEMAND_STAGE] == 2 and by_stage[ar.DEMAND_STAGE] == 3
    assert asked <= _subjects_of(["s1", "s2"])
    assert est.segments == 1


@pytest.mark.asyncio
async def test_release_held_and_job_spend_shapes():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await jev_scope.release_held(pool, "j1", "admin:u1") is None
    pool.fetchrow = AsyncMock(return_value={"id": "j1", "jev_release": json.dumps({"by": "admin:u1"})})
    assert await jev_scope.release_held(pool, "j1", "admin:u1") == {
        "job_id": "j1", "status": "queued", "jev_release": {"by": "admin:u1"}}
    assert pool.fetchrow.await_args.args[1:] == ("j1", "admin:u1", JEV_CAP_HELD)

    class Conn:
        async def fetch(self, sql, *args):
            assert sql is jev_scope.JOB_SPEND_SQL and args[0] == "j1"
            return [{"stage": "a3_demand", "calls": 10, "refused": 1, "usd": 0.0002},
                    {"stage": "a3_question_landing", "calls": 5, "refused": 0, "usd": 0.0001}]

    @asynccontextmanager
    async def acquire():
        yield Conn()

    pool.acquire = acquire
    spend = await jev_scope.job_jev_spend(pool, "j1")
    assert spend["calls"] == 15 and spend["refused"] == 1 and spend["usd"] == 0.0003
    assert set(spend["by_stage"]) == {"a3_demand", "a3_question_landing"}


# ── the recompute job ──────────────────────────────────────────────────────────────────────────────────────────────

HELD = {"scope": "changed", "cap_usd": 0.5, "hard_stop": True, "released": None,
        "estimate": {"new_calls": 40_000, "usd": 0.64, "segments": 2, "by_stage": {}},
        "held": f"{JEV_CAP_HELD}: Jev estimate $0.64 (40000 new questions) is above the $0.50 cap per recompute run."}


@pytest.mark.asyncio
async def test_a_held_recompute_stops_budget_with_its_estimate_and_one_bell(locks):  # noqa: F811
    body = AsyncMock(return_value={"ranking": {}, "route": {}})
    bell = AsyncMock()
    with patch("services.export.handler.recompute_rankings_and_routes", body), \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())), \
            patch.object(jev_scope, "plan_run", AsyncMock(return_value=dict(HELD))), \
            patch.object(jev_scope, "ring_held_bell", bell):
        ctx = _ctx({"scope": "platform", "reason": f"a3_atomize:{T1}", "segment_ids": ["s1", "s2"]})
        with pytest.raises(BudgetExceeded, match=f"^{JEV_CAP_HELD}: "):
            await rc.run(ctx)
    body.assert_not_awaited()
    assert ctx.result["held"] is True and ctx.result["jev"]["estimate"]["usd"] == 0.64
    assert bell.await_count == 1 and not locks.held


@pytest.mark.asyncio
async def test_a_released_recompute_runs_and_reports_its_jev_spend(locks):  # noqa: F811
    plan = AsyncMock(return_value={"scope": "changed", "cap_usd": 0.5, "released": {"by": "admin:u1"},
                                   "estimate": {"new_calls": 40_000, "usd": 0.64}})
    spend = {"calls": 812, "refused": 0, "usd": 0.013, "by_stage": {}}
    with patch("services.export.handler.recompute_rankings_and_routes",
               AsyncMock(return_value={"ranking": {}, "route": {}})) as body, \
            patch("services.export.handler.open_job_pool", AsyncMock(side_effect=lambda: _job_pool())), \
            patch.object(jev_scope, "plan_run", plan), \
            patch.object(jev_scope, "job_jev_spend", AsyncMock(return_value=spend)):
        out = await rc.run(_ctx({"scope": "platform", "reason": "r", "segment_ids": ["s1"],
                                 "jev_release": {"by": "admin:u1"}}, "job-r"))
    body.assert_awaited_once()
    assert plan.await_args.kwargs["released"] == {"by": "admin:u1"}
    assert out["jev"]["spend"] == spend and "held" not in out and out["segments_relanded"] == 1


@pytest.mark.asyncio
async def test_a_released_job_is_never_folded_into():
    assert "NOT (payload ? 'jev_release')" in rc._COALESCE_SQL
    pool = MagicMock()
    released = {"id": "j-rel", "payload": {"scope": "tour", "tour_id": T1, "jev_release": {"by": "a"}}}
    with patch.object(rc.queue, "list_jobs", AsyncMock(return_value=[released])), \
            patch.object(rc, "enqueue", AsyncMock(return_value=("j-new", True))) as enq:
        got = await rc.enqueue_recompute(pool, scope="tour", reason="atom_edit", tour_id=T1)
    assert got == ("j-new", True) and enq.await_count == 1
