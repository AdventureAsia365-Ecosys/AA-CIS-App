"""AA-914 — the Route Segment guard's SQL on the real schema (services/acp_shared/route_guard.py).

A Mongolia tour with two live atoms in two Segments, a third Segment whose only member is another tour's atom
(the "Dal Lake" case), a merged id aliased to one of the trip's Segments and an id with no atom_segment row.
The guard keeps the trip's two Segments (the aliased one resolved), drops the other two; `current_route` returns
the current Route version and never the superseded one. Everything is rolled back.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_shared import route_guard as rg  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    yield c
    await t.rollback()
    await c.close()


async def _live_tour(c, name, country):
    tour_id = str(uuid.uuid4())
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER)  # raw_tours / published_tours RLS
    await c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                           source_group_id, source_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, 'active')""",
                    tour_id, MASTER, BATCH_ID, name, country, str(uuid.uuid4()))
    await c.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id,
                           aa_name, aa_summary, aa_highlights, aa_itineraries, master_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'S', '[]'::jsonb, 'Day 1', 'active')""",
                    tour_id, str(uuid.uuid4()), MASTER, name)
    return tour_id


@pytest.mark.asyncio
async def test_guard_keeps_only_the_trip_segments_and_current_route_skips_superseded(tx):
    trip = await _live_tour(tx, "zz914 Khovsgol tour", "Mongolia")
    other = await _live_tour(tx, "zz914 Kashmir tour", "India")
    await tx.executemany("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place,
                            activity_type) VALUES ($1, $2::uuid, 'atom text', $3, $4, 'sightseeing')""",
                         [("zz914_a1", trip, 2, "Mörön"), ("zz914_a2", trip, 3, "Lake Khövsgöl"),
                          ("zz914_a3", other, 4, "Dal Lake")])
    await tx.execute("""INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action)
                        VALUES ('zz914_moron', 'Mörön', 'visit'), ('zz914_lake', 'Lake Khövsgöl', 'visit'),
                               ('zz914_old', 'Lake Khövsgöl', 'visit'), ('zz914_dal', 'Dal Lake', 'ride')""")
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical) "
                     "VALUES ('zz914_old', 'zz914_lake')")
    await tx.execute("""INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id, is_alias)
                        VALUES ('zz914_moron', 'zz914_a1', false), ('zz914_lake', 'zz914_a2', false),
                               ('zz914_dal', 'zz914_a3', false)""")

    snapshot = json.dumps(["zz914_moron", "zz914_dal", "zz914_old", "zz914_gone"])
    kept = await rg.trip_route_segment_ids(tx, trip, snapshot, tenant_id=str(uuid.uuid4()), request_id="r",
                                           consumer="test")
    assert kept == ["zz914_moron", "zz914_lake"]
    # the other tour keeps only its own Segment
    assert await rg.trip_route_segment_ids(tx, other, snapshot) == ["zz914_dal"]

    await tx.execute("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day,
                            last_day, version, superseded_at, created_at)
                        VALUES ('zz914_r', $1::uuid, 'Old', $2::jsonb, 2, 4, 1, now(), now()),
                               ('zz914_r:v2', $1::uuid, 'New', $3::jsonb, 2, 4, 2, NULL, now())""",
                    trip, json.dumps(["zz914_dal", "zz914_moron"]), json.dumps(["zz914_moron", "zz914_lake"]))
    assert await rg.current_route(tx, "zz914_r") is None
    assert await rg.current_route(tx, "zz914_r:v2") == {"tour_id": trip, "segment_ids": ["zz914_moron", "zz914_lake"]}
