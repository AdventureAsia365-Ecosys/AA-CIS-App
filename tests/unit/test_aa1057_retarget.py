"""AA-1057: the retarget plan (GAP 2: Route Subjects off superseded Routes, requests off replaced atoms; GAP 1: the
Segment re-seed is only offered when the old tour is not paused), the stage modes, the one-off script's guards and the
Subject-rule SQL fragments of shared.tour_liveness. Invented names only."""
import importlib.util
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_shared import retarget as rt
from shared import tour_liveness as tl

T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"


def _subject(sid, route, first=1, last=3, segs=("a", "b", "c"), source="src-1", tenant=T1, channel="blog",
             state="used"):
    return {"subject_id": sid, "tenant_id": tenant, "channel": channel, "state": state, "route_id": route,
            "tour_id": "tour-old", "first_day": first, "last_day": last, "ordered_segment_ids": list(segs),
            "source_id": source}


def _route(rid, first=1, last=3, segs=("a", "b", "c"), source="src-1", tour="tour-new"):
    return {"route_id": rid, "tour_id": tour, "first_day": first, "last_day": last, "ordered_segment_ids": list(segs),
            "source_id": source}


# ── Route Subjects ───────────────────────────────────────────────────────────────────────────────────────────────

def test_same_day_span_in_the_family_is_the_identity_target():
    plan = rt.plan_route_subjects([_subject("s1", "r:v1")], [_route("r:v2"), _route("other", 4, 5)], [])
    assert plan == [{"subject_id": "s1", "tenant_id": T1, "channel": "blog", "state": "used", "old_route_id": "r:v1",
                     "new_route_id": "r:v2", "outcome": "retarget", "rule": "identity"}]


def test_without_the_span_the_route_sharing_most_segments_wins():
    current = [_route("x", 1, 2, segs=("a", "z")), _route("y", 2, 4, segs=("a", "b", "c", "d"))]
    d = rt.plan_route_subjects([_subject("s1", "old", segs=("a", "b", "c"))], current, [])[0]
    assert (d["outcome"], d["new_route_id"], d["rule"]) == ("retarget", "y", "overlap")


def test_an_equal_overlap_tie_or_two_same_span_routes_is_never_guessed():
    tie = [_route("x", 1, 2, segs=("a", "q")), _route("y", 2, 4, segs=("b", "r"))]
    assert rt.plan_route_subjects([_subject("s1", "old", segs=("a", "b"))], tie, [])[0]["outcome"] == "ambiguous"
    two = [_route("x", tour="v2"), _route("y", tour="v3")]
    d = rt.plan_route_subjects([_subject("s1", "old")], two, [])[0]
    assert d["outcome"] == "ambiguous" and d["new_route_id"] is None


def test_no_shared_segment_and_another_family_are_no_match():
    current = [_route("x", 4, 5, segs=("q",)), _route("y", source="src-other")]
    assert rt.plan_route_subjects([_subject("s1", "old")], current, [])[0]["outcome"] == "no_match"


def test_a_target_the_tenant_holds_in_that_channel_is_taken_and_only_one_stale_subject_moves():
    current = [_route("r:v2")]
    held = [(T1, "blog", "r:v2")]
    assert rt.plan_route_subjects([_subject("s1", "r:v1")], current, held)[0]["outcome"] == "taken"
    # another channel of the same tenant is free
    assert rt.plan_route_subjects([_subject("s1", "r:v1", channel="facebook")], current, held)[0]["outcome"] == \
        "retarget"
    # two stale Subjects of one tenant + channel aiming at one Route: the first (by id) moves, the second is taken
    out = rt.plan_route_subjects([_subject("s2", "r:v0"), _subject("s1", "r:v1")], current, [])
    assert [(d["subject_id"], d["outcome"]) for d in out] == [("s1", "retarget"), ("s2", "taken")]


def test_segment_ids_stored_as_a_json_string_are_read():
    s = _subject("s1", "old")
    s["ordered_segment_ids"] = '["a", "b"]'
    d = rt.plan_route_subjects([s], [_route("y", 7, 8, segs=("b",))], [])[0]
    assert d["outcome"] == "retarget" and d["rule"] == "overlap"


# ── requests ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _req(rid, atom, segment=None, key_state="served"):
    return {"request_id": rid, "tenant_id": T1, "atom_id": atom, "status": "approved", "channel": "facebook",
            "segment_id": segment, "key_state": key_state}


def _atom(aid, place, source="src-1", day=2, scope="platform"):
    return {"atom_id": aid, "tour_id": "t", "owner_scope": scope, "itinerary_day": day, "place": place,
            "activity_type": "walk", "card_id": None, "source_id": source}


def test_a_unique_same_place_atom_is_a_repoint_and_no_reseed():
    out = rt.plan_requests([_req("q1", "old-1", segment="seg-1")], {"old-1": _atom("old-1", "Lumen Lake")},
                           [_atom("new-1", "Lumen Lake")])
    assert out[0]["outcome"] == "repoint" and out[0]["new_atom_id"] == "new-1" and out[0]["may_reseed"] is False


def test_no_same_place_atom_on_a_segment_subject_may_reseed_unless_the_tour_is_paused():
    old = {"old-1": _atom("old-1", "Lumen Lake")}
    live = rt.plan_requests([_req("q1", "old-1", segment="seg-1", key_state="retired")], old, [])
    assert live[0]["outcome"] == "no_match" and live[0]["may_reseed"] is True
    paused = rt.plan_requests([_req("q1", "old-1", segment="seg-1", key_state="paused")], old, [])
    assert paused[0]["outcome"] == "paused" and paused[0]["may_reseed"] is False
    no_segment = rt.plan_requests([_req("q1", "old-1", key_state="retired")], old, [])
    assert no_segment[0]["outcome"] == "no_match" and no_segment[0]["may_reseed"] is False


def test_an_atom_that_is_live_again_is_left_out():
    out = rt.plan_requests([_req("q1", "a-1")], {"a-1": _atom("a-1", "Lumen Lake")}, [_atom("a-1", "Lumen Lake")])
    assert out == []


def test_plan_summary_counts_outcomes_and_pins_the_changed_ids():
    plan = rt.Plan(subjects=[{"subject_id": "s1", "outcome": "retarget"}, {"subject_id": "s2", "outcome": "taken"}],
                   requests=[{"request_id": "q1", "outcome": "reseed"}, {"request_id": "q2", "outcome": "paused"}])
    s = plan.summary()
    assert s["subjects"]["stale"] == 2 and s["subjects"]["retarget"] == 1 and s["subjects"]["taken"] == 1
    assert s["requests"]["reseed"] == 1 and s["requests"]["paused"] == 1
    assert s["subjects"]["pin"] == rt.fingerprint(["s1"]) and s["requests"]["pin"] == rt.fingerprint(["q1"])
    assert rt.fingerprint(["b", "a"]) == rt.fingerprint(["a", "b"]) and len(rt.fingerprint([])) == 16


def test_restore_rows_carry_ids_and_old_values_only():
    plan = rt.Plan(subjects=[{"subject_id": "s1", "outcome": "retarget", "old_route_id": "r1", "new_route_id": "r2"}],
                   requests=[{"request_id": "q1", "outcome": "repoint", "old_atom_id": "a1", "new_atom_id": "a2"}])
    assert rt.restore_rows(plan) == {
        "subjects": [{"subject_id": "s1", "route_id": "r1", "new_route_id": "r2"}],
        "requests": [{"request_id": "q1", "atom_id": "a1", "new_atom_id": "a2", "outcome": "repoint"}]}


# ── the stage: modes ─────────────────────────────────────────────────────────────────────────────────────────────

def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _conn():
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_Tx())
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


def test_mode_reads_the_env(monkeypatch):
    monkeypatch.delenv(rt.MODE_ENV, raising=False)
    assert rt.mode() == "apply"
    monkeypatch.setenv(rt.MODE_ENV, "REPORT")
    assert rt.mode() == "report"
    monkeypatch.setenv(rt.MODE_ENV, "nonsense")
    assert rt.mode() == "apply"


@pytest.mark.asyncio
async def test_off_reads_nothing_and_report_writes_nothing(monkeypatch):
    conn = _conn()
    build = AsyncMock(return_value=rt.Plan(subjects=[{"subject_id": "s1", "outcome": "retarget",
                                                       "old_route_id": "r1", "new_route_id": "r2"}]))
    monkeypatch.setattr(rt, "build_plan", build)
    assert await rt.run_retarget(_pool(conn), "off") == {"mode": "off"}
    build.assert_not_awaited()
    out = await rt.run_retarget(_pool(conn), "report")
    assert out["mode"] == "report" and out["subjects"]["retarget"] == 1 and out["changed"] == {"subjects": 0,
                                                                                               "requests": 0}
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_writes_each_change_guarded_on_the_old_value(monkeypatch):
    conn = _conn()
    plan = rt.Plan(
        subjects=[{"subject_id": "s1", "outcome": "retarget", "old_route_id": "r1", "new_route_id": "r2"},
                  {"subject_id": "s2", "outcome": "taken", "old_route_id": "r3", "new_route_id": "r2"}],
        requests=[{"request_id": "q1", "outcome": "repoint", "old_atom_id": "a1", "new_atom_id": "a2"},
                  {"request_id": "q2", "outcome": "reseed", "old_atom_id": "a3", "new_atom_id": "a4",
                   "new_trip_id": T2, "new_route_segment_ids": ["x", "y"]},
                  {"request_id": "q3", "outcome": "no_match", "old_atom_id": "a5", "new_atom_id": None}])
    monkeypatch.setattr(rt, "build_plan", AsyncMock(return_value=plan))
    out = await rt.run_retarget(_pool(conn), "apply")
    assert out["changed"] == {"subjects": 1, "requests": 2}
    calls = [c.args for c in conn.execute.await_args_list]
    assert calls[0] == (rt.RETARGET_SUBJECT_SQL, "s1", "r1", "r2")
    assert calls[1] == (rt.REPOINT_REQUEST_SQL, "q1", "a1", "a2")
    assert calls[2] == (rt.RESEED_REQUEST_SQL, "q2", "a3", "a4", T2, '["x", "y"]')
    assert len(calls) == 3
    assert "AND s.route_id = $2" in rt.RETARGET_SUBJECT_SQL and "AND atom_id = $2" in rt.RESEED_REQUEST_SQL


@pytest.mark.asyncio
async def test_a_unique_race_on_one_subject_skips_it(monkeypatch):
    import asyncpg
    conn = _conn()
    conn.execute = AsyncMock(side_effect=[asyncpg.UniqueViolationError("dup"), "UPDATE 1"])
    plan = rt.Plan(subjects=[{"subject_id": "s1", "outcome": "retarget", "old_route_id": "r1", "new_route_id": "r2"},
                             {"subject_id": "s3", "outcome": "retarget", "old_route_id": "r4", "new_route_id": "r5"}])
    assert await rt.apply_plan(conn, plan) == {"subjects": 1, "requests": 0}


# ── the one-off script ───────────────────────────────────────────────────────────────────────────────────────────

def _script():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1057_retarget_once.py")
    spec = importlib.util.spec_from_file_location("aa1057_retarget_once", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_apply_needs_both_expected_counts():
    s = _script()
    assert s.parse([]).apply is False
    with pytest.raises(SystemExit):
        s.parse(["--apply", "--expect-subjects", "7"])
    a = s.parse(["--apply", "--expect-subjects", "7", "--expect-requests", "3"])
    assert (a.expect_subjects, a.expect_requests) == (7, 3)


@pytest.mark.asyncio
async def test_script_apply_refuses_when_the_plan_moved_and_verifies_on_a_new_connection(monkeypatch, capsys):
    s = _script()
    plan = rt.Plan(subjects=[{"subject_id": "s1", "outcome": "retarget", "old_route_id": "r1", "new_route_id": "r2",
                              "rule": "identity"}])
    monkeypatch.setattr(rt, "build_plan", AsyncMock(side_effect=[plan, plan, rt.Plan()]))
    monkeypatch.setattr(rt, "apply_plan", AsyncMock(return_value={"subjects": 1, "requests": 0}))
    with pytest.raises(SystemExit):
        await s.apply(_conn(), 2, 0, None)
    fresh = MagicMock()
    fresh.close = AsyncMock()
    out = await s.apply(_conn(), 1, 0, None, reconnect=AsyncMock(return_value=fresh))
    assert out["changed"] == {"subjects": 1, "requests": 0} and out["after"]["subjects"]["stale"] == 0
    assert out["planned"]["rules"] == {"identity": 1}
    assert "AA1057-RESTORE" in capsys.readouterr().out
    fresh.close.assert_awaited_once()


# ── the Subject-rule fragments ───────────────────────────────────────────────────────────────────────────────────

def test_subject_rule_fragments_use_the_served_member_rule_and_follow_merged_ids():
    alive = tl.segment_served_member_sql("r", exclude_tours="SELECT tour_id FROM t")
    assert "acp_contract.atom_segment_alias" in alive and "segment_id_canonical" in alive
    assert tl.live_master_sql("pt_sm") in alive and tl.live_source_sql("rt_sm") in alive
    assert "NOT ta_sm.deleted" in alive and "NOT ta_sm.is_empty_marker" in alive
    assert "ta_sm.tour_id NOT IN (SELECT tour_id FROM t)" in alive
    about = tl.requests_about_tours_sql("SELECT tour_id FROM t")
    assert about.count("EXISTS") >= 3 and "COALESCE(r.trip_id, ta.tour_id) IN (SELECT tour_id FROM t)" in about
    # the representative-tour rule is still the base of every request-tour read
    assert tl.request_tour_sql() in tl.REQUEST_TOURS_SQL
    assert "segment_id IS NOT NULL" in tl.LIVE_POST_TOUR_STATE_SQL
