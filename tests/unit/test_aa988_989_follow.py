"""AA-988 / AA-989 (S252) — what follows a Master publish: dead segment members, the orphan-segment GC and its lock,
the $0 place-card relink inside atomize, atom ids in the place components hash, and component ids that survive a
place rebuild (TripPlanner pins). No DB, no model."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from services.acp_contract import segment_gc as gc
from services.acp_contract import segment_matching as sm
from services.acp_shared import place_cards as pc
from services.places import components as cm
from services.places import days as days_mod
from services.places import names
from tests.unit.test_aa772_place_card_cost import FakeDB, FakeLLM, _atom

TOUR = "11111111-1111-1111-1111-111111111111"


# ── fakes ───────────────────────────────────────────────────────────────────────────────────

class _Conn:
    """Records every statement; `fetch_answers` maps a SQL fragment to rows."""

    def __init__(self, fetch_answers=None, fetchval=0, fail_on=None):
        self.calls: list[tuple[str, tuple]] = []
        self.fetch_answers = fetch_answers or {}
        self.fetchval_answer = fetchval
        self.fail_on = fail_on
        self.rolled_back = None

    def _log(self, sql, args):
        flat = " ".join(sql.split())
        self.calls.append((flat, args))
        if self.fail_on and self.fail_on in flat:
            raise asyncpg.exceptions.LockNotAvailableError("canceling statement due to lock timeout")

    async def execute(self, sql, *args):
        self._log(sql, args)
        return "DELETE 3"

    async def executemany(self, sql, rows):
        self._log(sql, (list(rows),))

    async def fetch(self, sql, *args):
        self._log(sql, args)
        for frag, rows in self.fetch_answers.items():
            if frag in sql:
                return rows
        return []

    async def fetchval(self, sql, *args):
        self._log(sql, args)
        return self.fetchval_answer

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                conn.rolled_back = exc_type is not None
                return False
        return _Tx()

    def sql(self):
        return [c[0] for c in self.calls]


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


# ── AA-988 S1: dead members of the tour, under the shared lock ─────────────────────────────

def test_segment_run_drops_dead_members_of_its_tour_inside_the_shared_lock():
    atoms = [{"atom_id": "a1", "tour_id": TOUR, "itinerary_day": 1, "place": "Punakha Dzong", "action": "visit",
              "activity_type": "culture", "country": "Bhutan"}]
    conn = _Conn({"ta.tour_id = $1": atoms})
    with patch.object(sm, "_apart_pairs", AsyncMock(return_value=(frozenset(), 0))):
        out = asyncio.run(sm.run_segment_matching(TOUR, _pool(conn)))
    sql = conn.sql()
    assert sql[0].startswith("SELECT pg_advisory_lock_shared(hashtext($1))")
    assert conn.calls[0][1] == (gc.SEGMENT_WRITE_LOCK_KEY,)
    assert sql[-1].startswith("SELECT pg_advisory_unlock_shared(hashtext($1))")
    dead = [c for c in conn.calls if "(ta.deleted OR ta.is_empty_marker)" in c[0]]
    assert len(dead) == 1 and dead[0][1] == (TOUR,)            # this tour only, by tour id
    assert "DELETE FROM acp_contract.atom_segment_member m USING acp_contract.tour_atoms ta" in dead[0][0]
    # inside the write transaction: after the member INSERT plan started, before the country check
    assert sql.index(dead[0][0]) > next(i for i, s in enumerate(sql) if "INSERT INTO acp_contract.atom_segment (" in s)
    assert out["dead_members_removed"] == 3


def test_segment_run_with_no_matchable_atom_still_drops_dead_members_and_releases_the_lock():
    conn = _Conn()
    out = asyncio.run(sm.run_segment_matching(TOUR, _pool(conn)))
    assert out["dead_members_removed"] == 3 and out["segments_written"] == 0
    assert any("(ta.deleted OR ta.is_empty_marker)" in s for s in conn.sql())
    assert conn.sql()[-1].startswith("SELECT pg_advisory_unlock_shared")


def test_the_shared_lock_is_released_when_the_run_fails():
    conn = _Conn()

    @gc.holds_segment_lock_shared
    async def boom(tour_id, pool):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        asyncio.run(boom(TOUR, _pool(conn)))
    assert [s.split("(")[0] for s in conn.sql()] == ["SELECT pg_advisory_lock_shared",
                                                     "SELECT pg_advisory_unlock_shared"]


# ── AA-988 S2: segment_gc ───────────────────────────────────────────────────────────────────

def _row(sid, **refs):
    return {"segment_id": sid, **{c: refs.get(c, False) for c in gc.REFERENCE_CLASSES}}


ORPHANS = [_row("s_dead"), _row("s_alias", alias=True), _row("s_subj", subject=True, brand_fit=True),
           _row("s_live", live_member=True), _row("s_dead2")]


def test_classify_deletes_only_zero_reference_orphans():
    plan = gc.classify(ORPHANS)
    assert plan["orphans"] == 5 and plan["delete_ids"] == ["s_dead", "s_dead2"]
    assert plan["kept"] == {"live_member": 1, "ranking": 0, "alias": 1, "subject": 1, "brand_fit": 1,
                            "subject_route": 0, "angle_request": 0}
    assert plan["kept_ids"] == ["s_alias", "s_subj", "s_live"]


def test_orphans_sql_checks_every_reference_class():
    sql = " ".join(gc.ORPHANS_SQL.split())
    for frag in ("acp_contract.v_active_tour_atoms", "NOT t.deleted AND NOT t.is_empty_marker",
                 "acp_contract.atom_ranking WHERE superseded_at IS NULL", "FROM acp_contract.atom_ranking)",
                 "segment_id_old", "segment_id_canonical", "acp_shared.subject", "acp_shared.debate_brand_fit_cache",
                 "sj.route_id = r.route_id", "g.route_segment_ids", "r.superseded_at IS NULL"):
        assert frag in sql, frag
    # the probe's orphan rule is the outer filter (no active member, no current ranking, no current route)
    assert "WHERE am.segment_id IS NULL AND cr.segment_id IS NULL AND ct.segment_id IS NULL" in sql


def test_gc_report_mode_reads_under_the_exclusive_lock_and_writes_nothing():
    conn = _Conn({"WITH active_m": ORPHANS}, fetchval=4)
    out = asyncio.run(gc.run_segment_gc(_pool(conn), mode="report"))
    sql = conn.sql()
    assert sql[0] == f"SET LOCAL lock_timeout = '{gc.LOCK_TIMEOUT}'"
    assert sql[1] == "SELECT pg_advisory_xact_lock(hashtext($1))"
    assert not any(s.startswith("DELETE") for s in sql)
    assert out["status"] == "report" and out["orphans"] == 5 and out["deletable"] == 2 and out["kept"] == 3
    assert out["dead_members"] == 4 and out["sample_deletable"] == ["s_dead", "s_dead2"]


def test_gc_apply_deletes_dead_members_then_segments_in_one_transaction():
    conn = _Conn({"WITH active_m": ORPHANS}, fetchval=4)
    out = asyncio.run(gc.run_segment_gc(_pool(conn), mode="apply"))
    deletes = [(s, a) for s, a in conn.calls if s.startswith("DELETE")]
    assert [d[0].split(" USING")[0].split(" s ")[0] for d in deletes] == [
        "DELETE FROM acp_contract.atom_segment_member m", "DELETE FROM acp_contract.atom_segment"]
    assert all(a == (["s_dead", "s_dead2"],) for _, a in deletes)
    assert "NOT EXISTS (SELECT 1 FROM acp_contract.atom_segment_member m" in deletes[1][0]   # re-check
    assert out["status"] == "applied" and out["deleted_segments"] == 3 and conn.rolled_back is False


def test_gc_apply_holds_a_delete_set_above_the_cap():
    conn = _Conn({"WITH active_m": ORPHANS})
    out = asyncio.run(gc.run_segment_gc(_pool(conn), mode="apply", max_delete=1))
    assert out["status"] == "held_volume" and not any(s.startswith("DELETE") for s in conn.sql())


def test_gc_skips_when_a_segment_run_holds_the_lock_too_long():
    conn = _Conn({"WITH active_m": ORPHANS}, fail_on="pg_advisory_xact_lock")
    out = asyncio.run(gc.run_segment_gc(_pool(conn), mode="apply"))
    assert out == {"mode": "apply", "status": "skipped", "reason": "lock_busy"}


def test_gc_mode_defaults_to_report(monkeypatch):
    monkeypatch.delenv(gc.MODE_ENV, raising=False)
    assert gc.gc_mode() == "report"
    monkeypatch.setenv(gc.MODE_ENV, "APPLY")
    assert gc.gc_mode() == "apply"
    monkeypatch.setenv(gc.MODE_ENV, "yes please")
    assert gc.gc_mode() == "report"
    assert asyncio.run(gc.run_segment_gc(object(), mode="off")) == {"mode": "off", "status": "off"}


# ── AA-989 C1: $0 card relink in atomize ──────────────────────────────────────────────────────

class _TourDB(FakeDB):
    """FakeDB + the relink's tour read and its S262 carry-over reads (`replaced`: the predecessors' card rows)."""

    def __init__(self, atoms, replaced=(), **kw):
        super().__init__(atoms, **kw)
        self.tour_reads = []
        self.replaced = [dict(r) for r in replaced]

    async def fetch(self, sql, *args):
        if "LIMIT $2" in sql and "v_active_tour_atoms" in sql:
            self.tour_reads.append(args)
            return [{"atom_id": a} for a in self.atoms]
        if "k.miss_reason = 'needs_draft'" in sql:
            return [{"atom_id": a, "itinerary_day": self.atoms[a]["itinerary_day"], "place": self.atoms[a]["place"]}
                    for a in args[0] if self.links.get(a) == {"card_id": None, "miss_reason": pc.NEEDS_DRAFT}]
        if "rt.source_group_id" in sql:
            assert args == (TOUR,) and "pc.status <> 'rejected'" in sql and "NOT EXISTS" in sql
            return self.replaced
        return await super().fetch(sql, *args)


def test_relink_links_existing_cards_records_misses_and_never_drafts():
    from datetime import datetime, timedelta, timezone
    card = {"card_id": "c1", "place_key": "khovsgol|mongolia", "display_name": "Khövsgöl Lake",
            "status": "auto_checked", "country": "Mongolia", "locality": None}
    db = _TourDB([_atom("n1", "Lake Khövsgöl"), _atom("n2", "Nong Khouay"), _atom("n3", "Tsetserleg"),
                  _atom("n4", "standard ger camp")],
                 cards=[card], aliases={("lake khovsgol", "mongolia"): "c1"})
    db.misses[("nong khouay", "mongolia")] = {"reason": "no_name", "rule_version": pc.RULE_VERSION,
                                               "prompt_sha": pc.PROMPT_SHA, "attempts": 1,
                                               "retry_after": datetime.now(timezone.utc) + timedelta(days=5)}
    llm = FakeLLM()
    with patch.object(pc, "LLMClient", return_value=llm) as client, \
            patch.object(pc, "build_card_for_group", new=AsyncMock()) as build:
        out = asyncio.run(pc.relink_tour_place_cards(db.pool(), TOUR))
    assert db.tour_reads == [(TOUR, pc.RELINK_MAX_ATOMS)]
    build.assert_not_awaited()
    client.assert_not_called()
    assert llm.drafts == [] and out["drafted"] == 0
    assert db.links["n1"] == {"card_id": "c1", "miss_reason": None} and out["linked_existing"] == 1
    assert db.links["n2"] == {"card_id": None, "miss_reason": "no_name"} and out["miss_recorded"] == 1
    assert db.links["n4"]["miss_reason"] in ("unnamed", "logistics_place")
    # would need a paid draft: left for later, but recorded (S262) so the AA-991 probe does not read it as lost
    assert db.links["n3"] == {"card_id": None, "miss_reason": "needs_draft"} and out["over_cap"] == 1
    assert out["needs_draft_recorded"] == 1 and out["carried_over"] == 0


def _day_atom(atom_id, place, day):
    return dict(_atom(atom_id, place), itinerary_day=day)


def _relink(db):
    llm = FakeLLM()
    with patch.object(pc, "LLMClient", return_value=llm) as client:
        out = asyncio.run(pc.relink_tour_place_cards(db.pool(), TOUR))
    client.assert_not_called()
    assert llm.drafts == []
    return out


def test_relink_carries_over_the_one_card_of_a_replaced_atom_for_free():
    """S262 — a respelt pass ('Zorvan pass (3550m)' after 'Zorvan Pass') and a generic name linked by an earlier draft
    get the card their replaced predecessor carried: no call, no NEEDS_DRAFT row left."""
    db = _TourDB([_day_atom("n1", "Zorvan pass (3550m)", 6), _day_atom("n2", "City Palace", 3)],
                 replaced=[{"atom_id": "o1", "itinerary_day": 6, "place": "Zorvan Pass", "card_id": "c9"},
                           {"atom_id": "o2", "itinerary_day": 3, "place": "the City Palace", "card_id": "c7"}])
    out = _relink(db)
    assert out["over_cap"] == 2 and out["needs_draft_recorded"] == 2 and out["carried_over"] == 2
    assert db.links["n1"] == {"card_id": "c9", "miss_reason": None}
    assert db.links["n2"] == {"card_id": "c7", "miss_reason": None}
    assert not db.aliases                                         # tour history, not a country-wide alias


def test_relink_never_guesses_between_two_prior_cards():
    """Two predecessors with two cards on the atom's day: it stays NEEDS_DRAFT for the paid build."""
    db = _TourDB([_day_atom("n1", "City Palace", 3)],
                 replaced=[{"atom_id": "o1", "itinerary_day": 3, "place": "City Palace", "card_id": "c7"},
                           {"atom_id": "o2", "itinerary_day": 3, "place": "City Palace", "card_id": "c8"}])
    out = _relink(db)
    assert out["carried_over"] == 0 and db.links["n1"] == {"card_id": None, "miss_reason": "needs_draft"}


def test_relink_with_no_waiting_atom_skips_the_predecessor_read():
    db = _TourDB([_atom("n4", "standard ger camp")],
                 replaced=[{"atom_id": "o1", "itinerary_day": 1, "place": "standard ger camp", "card_id": "c1"}])
    out = _relink(db)
    assert out["carried_over"] == 0 and db.links["n4"]["card_id"] is None


def test_a_failed_carry_over_keeps_the_needs_draft_rows():
    db = _TourDB([_day_atom("n1", "Zorvan pass", 6)])
    with patch.object(pc, "plan_carry_over", side_effect=RuntimeError("bad")):
        out = _relink(db)
    assert out["carried_over"] == "failed: RuntimeError" and out["needs_draft_recorded"] == 1
    assert db.links["n1"] == {"card_id": None, "miss_reason": "needs_draft"}


@pytest.mark.parametrize("waiting, replaced, expected", [
    # the same place_norm on another day: one card → carried
    ({"place": "Lake Quellin", "itinerary_day": 4},
     [{"place": "the Lake Quellin", "itinerary_day": 2, "card_id": "c1"}], "c1"),
    # the same name twice with two cards → the one on the same day
    ({"place": "City Palace", "itinerary_day": 5},
     [{"place": "City Palace", "itinerary_day": 2, "card_id": "c1"},
      {"place": "City Palace", "itinerary_day": 5, "card_id": "c2"}], "c2"),
    # two cards, neither on the same day → no guess
    ({"place": "City Palace", "itinerary_day": 7},
     [{"place": "City Palace", "itinerary_day": 2, "card_id": "c1"},
      {"place": "City Palace", "itinerary_day": 5, "card_id": "c2"}], None),
    # respelt (core name) only counts on the same day
    ({"place": "Zorvan Pass (3550m)", "itinerary_day": 6},
     [{"place": "Zorvan Pass", "itinerary_day": 5, "card_id": "c1"}], None),
    ({"place": "Zorvan Pass (3550m)", "itinerary_day": 6},
     [{"place": "Zorvan Pass", "itinerary_day": 6, "card_id": "c1"}], "c1"),
    # two predecessor atoms linked to the SAME card are one card
    ({"place": "Lake Quellin", "itinerary_day": 1},
     [{"place": "Lake Quellin", "itinerary_day": 1, "card_id": "c1"},
      {"place": "Lake Quellin", "itinerary_day": 3, "card_id": "c1"}], "c1"),
    # nothing alike → no link
    ({"place": "Lake Quellin", "itinerary_day": 1},
     [{"place": "Zorvan Pass", "itinerary_day": 1, "card_id": "c1"}], None),
])
def test_plan_carry_over_narrows_like_atom_repoint(waiting, replaced, expected):
    plan = pc.plan_carry_over([dict(waiting, atom_id="n1")], replaced)
    assert plan.get("n1") == expected


def _atomize(relink, after, order):
    from services.export import handler

    async def _relink(pool, tour_id):
        order.append("relink")
        return await relink(pool, tour_id)

    async def _after(pool, **kw):
        order.append("place_extract")
        return await after(pool, **kw)

    pool = AsyncMock()
    with patch("services.acp_contract.a3_atomize.run_a3_atomize", AsyncMock(return_value={"status": "success"})), \
            patch("services.acp_contract.segment_matching.run_segment_matching",
                  AsyncMock(side_effect=lambda *a: order.append("segment") or {})), \
            patch("services.export.handler.tour_segment_ids", AsyncMock(return_value=["s1"])), \
            patch("services.export.handler.clear_brand_fit_cache", AsyncMock(return_value=0)), \
            patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("j", True))), \
            patch("services.acp_shared.place_cards.relink_tour_place_cards", _relink), \
            patch("services.places.enqueue.after_atomize", _after), \
            patch("services.export.handler.open_job_pool", AsyncMock(return_value=pool)):
        return asyncio.run(handler._run_a3_atomize_background(TOUR, {"days": []}, "Bhutan", "v1"))


def test_atomize_relinks_cards_after_segments_and_before_place_extract():
    order = []
    out = _atomize(AsyncMock(return_value={"linked_existing": 2}), AsyncMock(return_value={"place_extract_job": "p"}),
                   order)
    assert order == ["segment", "relink", "place_extract"]
    assert out["place_cards_relink"] == {"linked_existing": 2} and out["place_extract_job"] == "p"


def test_a_failed_relink_is_recorded_and_place_extract_still_runs():
    order = []
    out = _atomize(AsyncMock(side_effect=RuntimeError("db gone")),
                   AsyncMock(return_value={"place_extract_job": "p"}), order)
    assert out["place_cards_relink"].startswith("failed: RuntimeError")
    assert order[-1] == "place_extract" and out["segment_score_route"] == "ok"


def test_atomize_scopes_the_recompute_to_segments_before_and_after_the_segment_stage():
    from services.export import handler
    enq = AsyncMock(return_value=("j", True))
    with patch("services.acp_contract.a3_atomize.run_a3_atomize", AsyncMock(return_value={"status": "success"})), \
            patch("services.acp_contract.segment_matching.run_segment_matching", AsyncMock(return_value={})), \
            patch("services.export.handler.tour_segment_ids",
                  AsyncMock(side_effect=[["old", "kept"], ["kept", "new"]])), \
            patch("services.export.handler.clear_brand_fit_cache", AsyncMock(return_value=0)) as clear, \
            patch("services.jobs.recompute_job.enqueue_recompute", enq), \
            patch("services.acp_shared.place_cards.relink_tour_place_cards", AsyncMock(return_value={})), \
            patch("services.places.enqueue.after_atomize", AsyncMock(return_value={})), \
            patch("services.export.handler.open_job_pool", AsyncMock(return_value=AsyncMock())):
        asyncio.run(handler._run_a3_atomize_background(TOUR, {"days": []}, "Bhutan", "v1"))
    # the Segments the replaced atoms left still get their cache cleared and their score re-landed
    assert enq.await_args.kwargs["segment_ids"] == ["kept", "new", "old"]
    assert clear.await_args.args[1] == ["kept", "new", "old"]


# ── AA-989 B2: atom ids in the components hash ───────────────────────────────────────────────

def test_components_hash_sees_an_atom_id_change_with_the_same_text():
    a = [{"atom_id": "old_1", "itinerary_day": 1, "place": "Paro Dzong", "activity_type": "culture", "text": "Visit"}]
    b = [dict(a[0], atom_id="new_1")]
    assert cm.components_hash(a) != cm.components_hash(b)
    assert cm.components_hash(a) == cm.components_hash([dict(a[0])])


def test_backfill_staleness_sees_an_atom_only_change():
    from services.places import backfill
    tour = {"tour_id": "t1", "aa_itineraries": "Day 1 — Paro"}
    build = {"status": "ok", "tour_id": "t1", "rule_version": days_mod.RULE_VERSION,
             "days_hash": days_mod.days_hash("Day 1 — Paro"), "components_hash": "h1"}
    assert not backfill.is_stale(tour, build)                   # days only: fresh
    assert not backfill.is_stale(tour, build, "h1")
    assert backfill.is_stale(tour, build, "h2")                 # same text, other atoms: stale


# ── AA-989: component ids survive a rebuild (TripPlanner saved pins) ─────────────────────────

def _comp(day, place, activity="cultural_heritage", card=None, link=None):
    return cm.Component(component_id=names.component_id("src", day, place, "Bhutan", activity), day_index=day,
                        name=place, link_name=link or place, name_norm=names.name_norm(link or place),
                        country="Bhutan", country_norm="bhutan", locality_norm="", locality=None, activity=activity,
                        intensity_level="leisurely", duration_hint="full_day", season_months=[1], text_extract=place,
                        place_card_id=card)


def _old(cid, day, name, link=None, dest=None, card=None):
    return {"component_id": cid, "day_index": day, "name": name, "name_norm": names.name_norm(link or name),
            "destination_id": dest, "place_card_id": card}


def test_carry_over_keeps_ids_of_respelt_retyped_and_moved_places():
    unchanged = _comp(1, "Paro Dzong")
    retyped = _comp(1, "Paro Town", activity="culinary")             # was cultural_heritage
    respelt = _comp(2, "Taktsang Monastery", activity="trekking", card="c1", link="Paro Taktsang")
    moved = _comp(4, "Dochula Pass")                                  # day 3 → 4
    brand_new = _comp(5, "Punakha Dzong")
    comps = [unchanged, retyped, respelt, moved, brand_new]
    old = [_old(unchanged.component_id, 1, "Paro Dzong"),
           _old("old-town", 1, "Paro Town"),
           _old("old-nest", 2, "Tiger's Nest", link="Paro Taktsang", dest="d1", card="c1"),
           _old("old-pass", 3, "Dochula Pass", dest="d3"),
           _old("old-gone", 3, "Hongtsho Village", dest="d9")]
    dest = {respelt.component_id: "d1", moved.component_id: "d3", brand_new.component_id: "d5"}
    mapping, counts = cm.carry_over_ids(comps, dest, old)
    assert mapping == {retyped.component_id: "old-town", respelt.component_id: "old-nest",
                       moved.component_id: "old-pass"}
    assert counts["ids_kept"] == 1 and counts["ids_carried"] == 3 and counts["ids_new"] == 1
    assert counts["ids_dropped"] == 1                                # the place the rewrite removed
    assert counts["ids_carried_by"] == {"spelling": 1, "destination": 1, "destination_any_day": 1}
    new_dest = cm.apply_carry_over(comps, dest, mapping)
    assert [c.component_id for c in comps] == [unchanged.component_id, "old-town", "old-nest", "old-pass",
                                               brand_new.component_id]
    assert new_dest == {"old-nest": "d1", "old-pass": "d3", brand_new.component_id: "d5"}


def test_carry_over_never_guesses_between_two_candidates():
    a, b = _comp(2, "Hoi An Ancient Town"), _comp(2, "Hoi An Night Market")
    old = [_old("old-x", 2, "Hoi An", dest="d1")]
    mapping, counts = cm.carry_over_ids([a, b], {a.component_id: "d1", b.component_id: "d1"}, old)
    assert mapping == {} and counts["ids_dropped"] == 1 and counts["ids_new"] == 2


def test_place_rebuild_persists_the_previous_ids_so_saved_pins_still_resolve(monkeypatch):
    """The TripPlanner proof: a trip pinned the S1 v1 component "Tiger's Nest" (id old-nest). The v2 rewrite calls it
    "Taktsang Monastery" (same place card); after the rebuild the source still has a row with id old-nest, which is
    what catalog.v_tour_component serves and what the trip's add_component event resolves against."""
    from services.places import pipeline, store
    text = "Day 1 — Paro\nParo Dzong.\n\nDay 2 — Paro\nHike to the monastery."
    old_nest_id = names.component_id("src", 2, "Tiger's Nest", "Bhutan", "trekking")
    atoms = [{"atom_id": "v2_a1", "itinerary_day": 1, "place": "Paro Dzong", "activity_type": "culture",
              "text": "Paro Dzong — fortress"},
             {"atom_id": "v2_a2", "itinerary_day": 2, "place": "Taktsang Monastery", "activity_type": "trek",
              "text": "Hike up", "card_id": "c1", "card_display_name": "Paro Taktsang", "card_country": "Bhutan",
              "card_destination_id": "d1"}]
    build = {"source_id": "src", "tour_id": "t2", "status": "ok", "rule_version": days_mod.RULE_VERSION,
             "days_hash": days_mod.days_hash(text), "components_hash": "v1-hash", "counts": {}}
    previous = [_old(names.component_id("src", 1, "Paro Dzong", "Bhutan", "cultural_heritage"), 1, "Paro Dzong"),
                _old(old_nest_id, 2, "Tiger's Nest", link="Paro Taktsang", dest="d1", card="c1")]
    persisted = {}

    async def _replace(conn, source_id, tour_id, days, places, vectors):
        persisted["ids"] = [p[0] for p in places]
        persisted["dest"] = {p[0]: p[9] for p in places}

    fakes = {
        "get_build": AsyncMock(return_value=build), "tour_atoms": AsyncMock(return_value=atoms),
        "has_embedding_column": AsyncMock(return_value=False),
        "existing_days": AsyncMock(return_value=[
            {"day_index": 1, "title": "Paro", "overnight_place": "Paro", "places": [], "extracted_by": "rule",
             "is_transit_day": False},
            {"day_index": 2, "title": "Paro", "overnight_place": "Paro", "places": [], "extracted_by": "rule",
             "is_transit_day": False}]),
        "resolved_names": AsyncMock(return_value={}), "known_destinations": AsyncMock(return_value={}),
        "existing_places": AsyncMock(return_value=previous), "destination_briefs": AsyncMock(return_value={}),
        "lock_build": AsyncMock(), "live_tour_for_source": AsyncMock(return_value="t2"),
        "lock_destinations": AsyncMock(), "insert_names": AsyncMock(), "replace_source_rows": _replace,
        "write_build": AsyncMock(), "pending_names_due": AsyncMock(return_value=0),
    }
    for name, fn in fakes.items():
        monkeypatch.setattr(store, name, fn)
    tour = {"tour_id": "t2", "source_id": "src", "country": "Bhutan", "aa_itineraries": text, "aa_name": "Bhutan"}
    res = asyncio.run(pipeline.extract_tour_places(_pool(_Conn()), tour, pipeline.ExtractDeps()))
    assert res["status"] == "ok" and res["ids_carried"] == 1 and res["ids_dropped"] == 0
    assert old_nest_id in persisted["ids"] and len(persisted["ids"]) == 2
    assert names.component_id("src", 2, "Taktsang Monastery", "Bhutan", "trekking") not in persisted["ids"]
    assert persisted["dest"][old_nest_id] == "d1"
