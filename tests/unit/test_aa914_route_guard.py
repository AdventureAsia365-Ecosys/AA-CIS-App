"""AA-914 — a blog request's Route Segments are read through one guard (services/acp_shared/route_guard.py).

S245: request snapshots taken 08–11/10 kept two Segment ids that later held another tour's Segment ("Dal Lake |
shikara ride", "a local Buddhist monastery") on a Mongolia blog. T9 skipped their atom text, but the Segments
themselves still reached the T8 question chain, `route_segment_count` and every regenerated request. Now every
reader keeps only the Segments the trip has a live atom in (merged ids followed first) and logs each drop, and a
pick / regenerate reads only the current Route.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from services.acp_shared import route_guard as rg
from services.acp_shared import slate
from services.acp_shared.subject_seed import Member

TRIP = "4e35f6b4-0000-4000-8000-000000000001"     # a Mongolia tour
OTHER = "9a9a9a9a-0000-4000-8000-000000000002"    # another tour
MONGOLIA = [f"mn{i}" for i in range(8)]
FOREIGN = "f60a56fb_dal_lake"                      # held another country's Segment; no member of TRIP
ORPHAN = "f734d599_monastery"                      # no longer an atom_segment row (or 0 members)


class _Conn:
    """A fake asyncpg connection over in-memory Segments, aliases, trip members and Routes, answering the
    three reads the guard and current_route make. Routes are filtered on `superseded_at IS NULL` only when
    the SQL asks for it, so a query without the filter would return the superseded row."""

    def __init__(self, segments=(), aliases=None, members=None, routes=None):
        self.segments = set(segments)
        self.aliases = dict(aliases or {})            # old id -> canonical id
        self.members = {k: set(v) for k, v in (members or {}).items()}  # segment -> {tour_id}
        self.routes = dict(routes or {})              # route_id -> (tour_id, [ids], superseded)
        self.sql: list[str] = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "WITH RECURSIVE chain" in sql:             # segment_matching._ALIAS_CHAIN_SQL
            out = []
            for start in args[0]:
                cur = start
                while cur in self.aliases and not self.members.get(cur):
                    cur = self.aliases[cur]
                out.append({"start_id": start, "segment_id": cur})
            return out
        if "trip_member" in sql:                      # route_guard._TRIP_MEMBER_SQL
            ids, tour = args[0], args[1]
            return [{"segment_id": s, "trip_member": tour in self.members.get(s, set())}
                    for s in ids if s in self.segments]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        assert "acp_contract.route" in sql
        r = self.routes.get(args[0])
        if r is None or ("superseded_at IS NULL" in sql and r[2]):
            return None
        return {"tour_id": r[0], "ordered_segment_ids": json.dumps(r[1])}


def _snapshot_conn(**kw):
    members = {s: {TRIP} for s in MONGOLIA}
    members[FOREIGN] = {OTHER}
    return _Conn(segments=MONGOLIA + [FOREIGN], members=members, **kw)


# ── the pure core ───────────────────────────────────────────────────────────────────────────────

def test_filter_drops_foreign_and_orphaned_ids_and_keeps_order():
    ids = ["a", "foreign", "b", "orphan"]
    kept, drops = rg.filter_route_segments(ids, {}, known={"a", "b", "foreign"}, trip_members={"a", "b"})
    assert kept == ["a", "b"]
    assert [(d.segment_id, d.reason) for d in drops] == [("foreign", rg.DROP_NOT_MEMBER),
                                                         ("orphan", rg.DROP_UNKNOWN)]


def test_filter_resolves_an_alias_and_drops_a_duplicate_after_it():
    kept, drops = rg.filter_route_segments(["old", "live", "x"], {"old": "live"}, known={"live", "x"},
                                           trip_members={"live", "x"})
    assert kept == ["live", "x"]
    assert drops == [rg.SegmentDrop("live", "live", rg.DROP_DUPLICATE)]


@pytest.mark.parametrize("value,expected", [(None, []), ("", []), ('["a", null, "b"]', ["a", "b"]),
                                            (["a", ""], ["a"])])
def test_parse_segment_ids(value, expected):
    assert rg.parse_segment_ids(value) == expected


# ── the guard over the DB reads ─────────────────────────────────────────────────────────────────

def test_snapshot_with_a_foreign_and_an_orphaned_id_keeps_only_the_trip_segments_and_logs_each_drop():
    """The S245 request: 10 stored ids, 8 Mongolian, 1 now another tour's Segment, 1 orphaned."""
    snapshot = MONGOLIA[:3] + [FOREIGN] + MONGOLIA[3:6] + [ORPHAN] + MONGOLIA[6:]
    with capture_logs() as logs:
        kept = asyncio.run(rg.trip_route_segment_ids(_snapshot_conn(), TRIP, json.dumps(snapshot),
                                                     tenant_id="t1", request_id="req-1", consumer="t8_angles"))
    assert kept == MONGOLIA
    drops = [e for e in logs if e["event"] == "route_segment_dropped"]
    assert [(e["segment_id"], e["reason"]) for e in drops] == [(FOREIGN, rg.DROP_NOT_MEMBER),
                                                               (ORPHAN, rg.DROP_UNKNOWN)]
    assert all(e["request_id"] == "req-1" and e["trip_id"] == TRIP and e["consumer"] == "t8_angles"
               and e["log_level"] == "warning" for e in drops)


def test_an_aliased_id_resolves_to_its_live_segment():
    conn = _Conn(segments={"live", "mn0"}, aliases={"merged_old": "live"}, members={"live": {TRIP}, "mn0": {TRIP}})
    with capture_logs() as logs:
        kept = asyncio.run(rg.trip_route_segment_ids(conn, TRIP, ["merged_old", "mn0"], request_id="r"))
    assert kept == ["live", "mn0"]
    assert not [e for e in logs if e["event"] == "route_segment_dropped"]
    [moved] = [e for e in logs if e["event"] == "route_segment_alias_resolved"]
    assert moved["moved"] == {"merged_old": "live"}


def test_no_trip_drops_everything_without_a_read():
    conn = _Conn()
    with capture_logs() as logs:
        assert asyncio.run(rg.trip_route_segment_ids(conn, None, ["a", "b"], request_id="r")) == []
    assert [e["reason"] for e in logs] == [rg.DROP_NO_TRIP] * 2 and conn.sql == []


def test_empty_snapshot_reads_nothing():
    conn = _Conn()
    assert asyncio.run(rg.trip_route_segment_ids(conn, TRIP, None)) == []
    assert asyncio.run(rg.request_route_segment_ids(MagicMock(), {"route_segment_ids": None},
                                                    tenant_id="t", consumer="t9_write")) == []
    assert conn.sql == []


# ── current Route only ──────────────────────────────────────────────────────────────────────────

def test_current_route_never_returns_a_superseded_version():
    conn = _Conn(routes={"r:v7": (TRIP, ["mn0", FOREIGN], True), "r:v8": (TRIP, ["mn0", "mn1"], False)})
    assert asyncio.run(rg.current_route(conn, "r:v7")) is None
    assert asyncio.run(rg.current_route(conn, "r:v8")) == {"tour_id": TRIP, "segment_ids": ["mn0", "mn1"]}
    assert asyncio.run(rg.current_route(conn, None)) is None
    assert all("superseded_at IS NULL" in q for q in conn.sql)


def _anchor(conn):
    """slate._resolve_representative_atom's Route branch, with the Segment-member reads stubbed."""
    async def members(_conn, ids, _tenant):
        return {ids[0]: [Member(atom_id=f"atom-{ids[0]}", tour_id=TRIP, place="Mörön", country="Mongolia",
                                evidence="e", card_id=None)]}

    conn.fetchval = AsyncMock(return_value="Mörön")
    with patch.object(slate, "load_segment_members", new=members):
        return asyncio.run(slate._resolve_representative_atom(conn, "tenant", None, "r:v7"))


def test_pick_on_a_superseded_route_is_refused():
    conn = _snapshot_conn(routes={"r:v7": (TRIP, MONGOLIA, True)})
    assert _anchor(conn) == (None, None, None)


def test_pick_snapshot_drops_a_foreign_segment_and_anchors_on_the_first_kept_one():
    conn = _snapshot_conn(routes={"r:v7": (TRIP, [FOREIGN] + MONGOLIA[:3], False)})
    atom_id, trip_id, segment_ids = _anchor(conn)
    assert segment_ids == MONGOLIA[:3] and trip_id == TRIP and atom_id == "atom-mn0"


def test_pick_with_no_segment_left_on_the_tour_is_refused():
    conn = _snapshot_conn(routes={"r:v7": (TRIP, [FOREIGN, ORPHAN], False)})
    assert _anchor(conn) == (None, None, None)


# ── T8 / T9 / regenerate wiring ─────────────────────────────────────────────────────────────────

def test_t8_route_digest_reads_the_guarded_ids():
    from services.acp_angle_gate import service as ag
    req = {"request_id": "req-1", "tenant_id": "t1", "atom_id": "a1", "trip_id": TRIP, "channel": "blog",
           "goal": None, "status": "pending_goal", "dfs_paa_snapshot": None, "subject_id": None,
           "route_segment_ids": json.dumps([FOREIGN] + MONGOLIA[:2])}
    guard = AsyncMock(return_value=MONGOLIA[:2])
    seen = {}

    async def stop(_tenant, r, _pool):  # _route_moments: record what it got, then stop the run
        seen["ids"] = r["route_segment_ids"]
        raise RuntimeError("stop")

    with patch.object(ag, "_fetch_request_row", new=AsyncMock(return_value=req)), \
         patch.object(ag, "request_route_segment_ids", new=guard), \
         patch.object(ag, "ensure_place_cards", new=AsyncMock()), \
         patch.object(ag, "_fetch_atom_for_tenant", new=AsyncMock(return_value={"text": "seed"})), \
         patch.object(ag, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch.object(ag, "_route_moments", new=stop), pytest.raises(RuntimeError):
        asyncio.run(ag.set_goal_and_generate("t1", "req-1", "promotion", MagicMock()))
    assert guard.await_args.kwargs == {"tenant_id": "t1", "consumer": "t8_angles"}
    assert seen["ids"] == MONGOLIA[:2]


def test_t8_question_chain_gets_only_the_guarded_segments():
    from services.acp_angle_gate import service as ag
    chain = AsyncMock(return_value=[])
    with patch.object(ag, "collect_real_questions", new=chain):
        asyncio.run(ag._blog_search_demand({"atom_id": "a1", "route_segment_ids": MONGOLIA[:2]}, None, MagicMock()))
    assert chain.await_args.kwargs["segment_ids"] == MONGOLIA[:2]


def test_t9_counts_and_writes_from_the_guarded_ids_only():
    from services.acp_content_writing import service as cw
    req = {"request_id": "req-1", "trip_id": TRIP, "route_segment_ids": [FOREIGN] + MONGOLIA[:2], "status": "approved",
           "channel": "blog", "angles": [{"chosen": True, "option_id": None}], "cta": "Ask us", "goal": "inspire",
           "atom_id": "a1", "dfs_paa_snapshot": None}
    fetch = AsyncMock(side_effect=RuntimeError("stop"))
    with patch.object(cw.angle_gate_service, "fetch_request", new=AsyncMock(return_value=req)), \
         patch.object(cw, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch.object(cw, "request_route_segment_ids", new=AsyncMock(return_value=MONGOLIA[:2])), \
         patch.object(cw, "_fetch_route_segments", new=fetch), pytest.raises(RuntimeError):
        asyncio.run(cw.start_write("t1", "req-1", MagicMock()))
    assert fetch.await_args.args[2] == MONGOLIA[:2]


class _RegenConn(_Conn):
    def __init__(self, src, **kw):
        members = {s: {TRIP} for s in MONGOLIA}
        members[FOREIGN] = {OTHER}
        super().__init__(segments=MONGOLIA + [FOREIGN], members=members, **kw)
        self.src = src

    async def fetchrow(self, sql, *args):
        if "angle_gate_request r" in sql:
            return self.src
        return await super().fetchrow(sql, *args)


def _regen_ids(src, routes=None):
    from api.routers import admin_content_quality as cq
    return asyncio.run(cq._regenerate_route_segment_ids(_RegenConn(src, routes=routes), "req-1"))


def test_regenerate_copies_the_guarded_snapshot_when_the_route_is_superseded():
    src = {"tenant_id": "t1", "trip_id": TRIP, "route_segment_ids": json.dumps([FOREIGN] + MONGOLIA[:3]),
           "route_id": "r:v7"}
    assert _regen_ids(src, routes={"r:v7": (TRIP, [FOREIGN, "mn7"], True)}) == MONGOLIA[:3]


def test_regenerate_reads_the_current_route_when_there_is_one():
    src = {"tenant_id": "t1", "trip_id": TRIP, "route_segment_ids": json.dumps([FOREIGN, "mn0"]), "route_id": "r:v8"}
    assert _regen_ids(src, routes={"r:v8": (TRIP, ["mn4", FOREIGN, "mn5"], False)}) == ["mn4", "mn5"]


def test_regenerate_keeps_null_for_a_segment_request_and_refuses_an_empty_route():
    from fastapi import HTTPException
    assert _regen_ids({"tenant_id": "t1", "trip_id": TRIP, "route_segment_ids": None, "route_id": None}) is None
    with pytest.raises(HTTPException) as e:
        _regen_ids({"tenant_id": "t1", "trip_id": TRIP, "route_segment_ids": json.dumps([FOREIGN, ORPHAN]),
                    "route_id": None})
    assert e.value.status_code == 409
