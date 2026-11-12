"""AA-1081 (S278) — the atomize-side check that keeps the T7 Debate brand-fit cache of an unchanged Segment, on the
real schema (services/export/handler.py).

1. `segment_states()` (`_SEGMENT_STATE_SQL`): per Segment its label and every member atom id (any tour, deleted atoms
   included: the segment stage drops their member rows), sorted; a Segment without members has an empty list; an id
   not asked for or missing is absent.
2. `changed_segments()` over two reads around a membership change names only the Segment that changed, and
   `clear_brand_fit_cache()` with that list deletes only its cache rows (the unchanged Segment stays a cache hit).

Invented tours, places and ids; everything is rolled back.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services.export import handler  # noqa: E402


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _atom(c, tour, atom_id, place, *, deleted=False):
    await c.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence, "
                    "deleted) VALUES ($1, $2::uuid, 'platform', 'Walk the shore', $3, 'x', $4)",
                    atom_id, tour, place, deleted)


async def _member(c, seg, atom):
    await c.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)", seg, atom)


async def _seed(c):
    a, b = await l2._tour(c, "zz1081s-a"), await l2._tour(c, "zz1081s-b")
    await c.executemany("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                        "VALUES ($1, $2, $3)", [("zz1081s_lake", "Lake Aurel", "walk"),
                                                ("zz1081s_pass", "Corvin Pass", "hike"),
                                                ("zz1081s_empty", "Tolm Ridge", "climb")])
    await _atom(c, a.tour, "zz1081s_a2", "Lake Aurel")
    await _atom(c, a.tour, "zz1081s_a1", "Lake Aurel", deleted=True)
    await _atom(c, b.tour, "zz1081s_b1", "Lake Aurel")
    await _atom(c, a.tour, "zz1081s_a3", "Corvin Pass")
    for seg, atom in (("zz1081s_lake", "zz1081s_a2"), ("zz1081s_lake", "zz1081s_a1"),
                      ("zz1081s_lake", "zz1081s_b1"), ("zz1081s_pass", "zz1081s_a3")):
        await _member(c, seg, atom)
    return a, b


@pytest.mark.asyncio
async def test_segment_states_read_label_and_every_member_sorted(conn):
    await _seed(conn)
    states = await handler.segment_states(conn, ["zz1081s_lake", "zz1081s_pass", "zz1081s_empty", "zz1081s_none"])
    assert states == {
        "zz1081s_lake": ("Lake Aurel", "walk", ("zz1081s_a1", "zz1081s_a2", "zz1081s_b1")),
        "zz1081s_pass": ("Corvin Pass", "hike", ("zz1081s_a3",)),
        "zz1081s_empty": ("Tolm Ridge", "climb", ()),
    }
    assert await handler.segment_states(conn, []) == {}


@pytest.mark.asyncio
async def test_only_the_changed_segment_loses_its_cache_rows(conn):
    await _seed(conn)
    tenant = await l2._tenant(conn, "zz1081s")
    await conn.executemany(
        "INSERT INTO acp_shared.debate_brand_fit_cache (tenant_id, segment_id, brand_version, judge_score, "
        "brand_fit_score, cross_brand_distinct, mission_present) VALUES ($1::uuid, $2, 1, 8, 8, 8, true)",
        [(tenant, "zz1081s_lake"), (tenant, "zz1081s_pass")])
    ids = ["zz1081s_lake", "zz1081s_pass"]
    before = await handler.segment_states(conn, ids)

    # the segment stage drops the dead atom's member row: the lake's evidence changed, the pass did not
    await conn.execute("DELETE FROM acp_contract.atom_segment_member WHERE atom_id = 'zz1081s_a1'")
    after = await handler.segment_states(conn, ids)
    changed = handler.changed_segments(before, after, ids)
    assert changed == ["zz1081s_lake"]

    assert await handler.clear_brand_fit_cache(conn, changed) == 1
    left = await conn.fetch("SELECT segment_id FROM acp_shared.debate_brand_fit_cache WHERE tenant_id = $1::uuid",
                            tenant)
    assert [r["segment_id"] for r in left] == ["zz1081s_pass"]
