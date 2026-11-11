"""AA-1081 — the Blog topic coverage SQL on the real schema (services/acp_shared/slate.py, blog_seed.py).

1. `_SEGMENT_CANDIDATES_SQL.tour_ids`: the tenant's tours a Segment is on (current, non-excluded rows in the
   tenant's markets); another tour, an excluded row, a superseded row and another market are not counted.
2. `_TENANT_TOURS_SQL`: one row per adopted tour (several versions = one tour) with its catalog name.
3. `_HELD_TOPIC_TOURS_SQL`: the tours of the tenant's picked / used Subjects on the Channel (a Segment on each tour it
   is ranked on, a merged id through its alias, a Route Subject on its Route's tour); proposed / cut rows, another
   Channel and another tenant are no topic.
4. `blog_seed._WALK_ATOMS_SQL`: one live member atom per walk Segment on the tour, with A3's exclusion flag; a
   deleted atom and another tour's atom are not read.

Invented tours, places and ids; everything is rolled back.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services.acp_shared import blog_seed, slate  # noqa: E402


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _segments(c, *rows):
    await c.executemany("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                        "VALUES ($1, $2, 'visit')", rows)


async def _rank(c, rows):
    # (market, tour, segment, excluded_reason, superseded)
    await c.executemany("""
        INSERT INTO acp_contract.atom_ranking (market, tour_id, segment_id, total_rank, demand_volume, questions,
                                               said, recurrence, computed_at, version, excluded_reason, superseded_at)
        VALUES ($1, $2::uuid, $3, 1, 2400, 5, 10, 1, now(), $6, $4, CASE WHEN $5 THEN now() END)""",
                        [(m, t, s, ex, sup, 2 if sup else 1) for m, t, s, ex, sup in rows])


async def _adopt(c, tenant, pt, version=1):
    await c.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, version_number, "
                    "rewritten_content, status) VALUES ($1::uuid, $2::uuid, $3, '{}'::jsonb, 'pending')",
                    tenant, pt, version)


@pytest.mark.asyncio
async def test_segment_candidates_carry_the_tenant_tours_they_are_on(conn):
    a, b, other = (await l2._tour(conn, "zz1081-a"), await l2._tour(conn, "zz1081-b"),
                   await l2._tour(conn, "zz1081-o"))
    await _segments(conn, ("zz1081_lake", "Lake Aurel"), ("zz1081_pass", "Corvin Pass"))
    await _rank(conn, [
        ("US", a.tour, "zz1081_lake", None, False), ("UK", b.tour, "zz1081_lake", None, False),
        ("US", other.tour, "zz1081_lake", None, False),              # not a tenant tour
        ("US", a.tour, "zz1081_pass", None, False),
        ("US", b.tour, "zz1081_pass", "transit", False),             # excluded on b
        ("DE", b.tour, "zz1081_pass", None, False),                  # another market
        ("US", b.tour, "zz1081_pass", None, True),                   # a superseded version
    ])
    rows = {r["segment_id"]: r for r in await conn.fetch(slate._SEGMENT_CANDIDATES_SQL, [a.tour, b.tour],
                                                         ["US", "UK"])}
    assert rows["zz1081_lake"]["tour_ids"] == sorted([a.tour, b.tour])
    assert rows["zz1081_pass"]["tour_ids"] == [a.tour]
    assert slate._candidate_of(rows["zz1081_pass"]).tour_ids == (a.tour,)


@pytest.mark.asyncio
async def test_tenant_tours_one_row_per_tour_with_its_name(conn):
    tenant, other = await l2._tenant(conn, "zz1081t"), await l2._tenant(conn, "zz1081u")
    a, b = await l2._tour(conn, "zz1081-ta"), await l2._tour(conn, "zz1081-tb")
    await _adopt(conn, tenant, a.pt, 1)
    await _adopt(conn, tenant, a.pt, 2)
    await _adopt(conn, other, b.pt, 1)
    rows = await conn.fetch(slate._TENANT_TOURS_SQL, uuid.UUID(tenant))
    assert [(r["tour_id"], r["aa_name"]) for r in rows] == [(a.tour, "aa986 zz1081-ta")]


@pytest.mark.asyncio
async def test_held_topic_tours_count_picked_and_used_subjects_per_tour(conn):
    tenant, other = await l2._tenant(conn, "zz1081h"), await l2._tenant(conn, "zz1081i")
    a, b, c = (await l2._tour(conn, "zz1081-ha"), await l2._tour(conn, "zz1081-hb"),
               await l2._tour(conn, "zz1081-hc"))
    await _segments(conn, ("zz1081_h_lake", "Lake Aurel"), ("zz1081_h_old", "Mirsa Gorge"),
                    ("zz1081_h_new", "Mirsa Gorge"), ("zz1081_h_bay", "Selva Bay"), ("zz1081_h_moor", "Brenn Moor"))
    await conn.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                       "VALUES ('zz1081_h_old', 'zz1081_h_new', now())")
    await _rank(conn, [
        ("US", a.tour, "zz1081_h_lake", None, False), ("UK", a.tour, "zz1081_h_lake", None, False),
        ("US", b.tour, "zz1081_h_lake", None, False),
        ("US", c.tour, "zz1081_h_new", None, False),                 # the merged id's live Segment
        ("US", c.tour, "zz1081_h_bay", None, False),
        ("US", c.tour, "zz1081_h_moor", None, False),
    ])
    await conn.execute("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day,
                              last_day, version, created_at)
                          VALUES ('zz1081_h_r', $1::uuid, 'Loop', '["zz1081_h_bay"]', 1, 3, 1, now())""",
                       uuid.UUID(b.tour))
    await conn.executemany("""INSERT INTO acp_shared.subject (tenant_id, segment_id, route_id, channel, state,
                                  cleared_bar_reason) VALUES ($1::uuid, $2, $3, $4, $5, '{}'::jsonb)""", [
        (tenant, "zz1081_h_lake", None, "blog", "used"),             # on a and b
        (tenant, "zz1081_h_old", None, "blog", "picked"),            # merged into zz1081_h_new: c
        (tenant, None, "zz1081_h_r", "blog", "used"),                # a pre-S271 Route Subject: b
        (tenant, "zz1081_h_bay", None, "blog", "proposed"),          # still on offer: no topic yet
        (tenant, "zz1081_h_moor", None, "blog", "cut"),              # dismissed: no topic
        (tenant, "zz1081_h_moor", None, "linkedin", "used"),         # another Channel
        (other, "zz1081_h_bay", None, "blog", "used"),               # another tenant
    ])
    rows = await conn.fetch(slate._HELD_TOPIC_TOURS_SQL, uuid.UUID(tenant), "blog", [a.tour, b.tour, c.tour])
    assert {r["tour_id"]: r["n"] for r in rows} == {a.tour: 1, b.tour: 2, c.tour: 1}


@pytest.mark.asyncio
async def test_walk_atoms_read_one_live_member_per_segment_on_the_tour(conn):
    tenant = await l2._tenant(conn, "zz1081w")
    a, b = await l2._tour(conn, "zz1081-wa"), await l2._tour(conn, "zz1081-wb")

    async def atom(atom_id, tour, day, deleted=False):
        await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence, "
                           "itinerary_day, deleted) VALUES ($1, $2::uuid, 'platform', $3, 'Lake Aurel', 'x', $4, $5)",
                           atom_id, uuid.UUID(tour), f"Moment {atom_id}", day, deleted)

    await atom("zz1081-w1", a.tour, 1)
    await atom("zz1081-w2", a.tour, 2)
    await atom("zz1081-w2b", a.tour, 2)
    await atom("zz1081-w3", a.tour, 3, deleted=True)
    await atom("zz1081-wo", b.tour, 1)
    await _segments(conn, ("zz1081_w_s1", "Lake Aurel"), ("zz1081_w_s2", "Corvin Pass"), ("zz1081_w_s3", "Old Velk"))
    await conn.executemany("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias) "
                           "VALUES ($1, $2, false)", [
                               ("zz1081_w_s1", "zz1081-w1"), ("zz1081_w_s1", "zz1081-wo"),
                               ("zz1081_w_s2", "zz1081-w2b"), ("zz1081_w_s2", "zz1081-w2"),
                               ("zz1081_w_s3", "zz1081-w3")])
    await _rank(conn, [("US", a.tour, "zz1081_w_s2", "transit", False)])
    rows = await conn.fetch(blog_seed._WALK_ATOMS_SQL, ["zz1081_w_s1", "zz1081_w_s2", "zz1081_w_s3"], a.tour, tenant)
    by_seg = {r["segment_id"]: r for r in rows}
    assert set(by_seg) == {"zz1081_w_s1", "zz1081_w_s2"}           # the deleted atom's Segment has no live member
    assert by_seg["zz1081_w_s1"]["atom_id"] == "zz1081-w1"        # not the other tour's atom
    assert by_seg["zz1081_w_s2"]["atom_id"] == "zz1081-w2"        # first by atom_id
    assert by_seg["zz1081_w_s2"]["excluded"] is True and by_seg["zz1081_w_s1"]["excluded"] is None
    assert blog_seed.walk_atoms(rows, ["zz1081_w_s1", "zz1081_w_s2"]) == [("zz1081-w1", "Moment zz1081-w1")]
