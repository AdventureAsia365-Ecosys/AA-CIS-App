"""AA-671 S257 — photo -> destination of its own tour, on real Postgres (lesson S231: new SQL needs an integration
test). Everything is seeded inside one transaction that is rolled back.

- services.photos.destinations.tour_candidates: the places of the photo's OWN tour by its source key (a tour variant
  finds its group's places), geo.tour_place + the CURRENT graph build's stops, never a merged duplicate or an
  unresolved place; read as a plain NOSUPERUSER, NOBYPASSRLS role without any tripplanner privilege (replaces the
  AA-860 itinerary_places test: no CIS read of tripplanner.*; raw_tours is FORCE-RLS, the read pins the AA master).
- photo_destination_match job SQL: the open set (matched, a tour, no destination), the guarded UPDATE (never a set
  destination, a rejected photo or a row whose tour changed), and what the photo view by place then serves.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MASTER = "00000000-0000-0000-0000-000000000001"
COUNTRY = "Zz671land"
READER = "zz671_photo_reader"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _destination(c, name, duplicate_of=None):
    return str(await c.fetchval("INSERT INTO shared.destinations (name, country, lat, lng, duplicate_of) "
                                "VALUES ($1, $2, 10, 100, $3::uuid) RETURNING id", f"zz671 {name}", COUNTRY,
                                duplicate_of))


async def _tour(c, source_group_id=None, source_status="active"):
    tour_id = str(uuid.uuid4())
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER)
    await c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                           source_group_id, source_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, 'zz671 tour', 'Vietnam', $4::uuid, $5)""",
                    tour_id, MASTER, BATCH_ID, source_group_id, source_status)      # raw_tours.country is CHECKed
    return tour_id


async def _tour_place(c, tour_id, source_id, destination_id, name, day=1):
    await c.execute("""INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm,
                           country, country_norm, destination_id, activity, intensity_level, duration_hint,
                           text_extract, text_hash)
                       VALUES ($1::uuid, $2, $3::uuid, $4, $5, lower($5), $6, lower($6), $7::uuid,
                               'cultural_heritage', 'leisurely', 'half_day', 't', 'h')""",
                    str(uuid.uuid4()), source_id, tour_id, day, name, COUNTRY, destination_id)


async def _build(c, *, current):
    if current:
        await c.execute("UPDATE geo.graph_build SET is_current = false WHERE is_current")
    return str(await c.fetchval("INSERT INTO geo.graph_build (is_current) VALUES ($1) RETURNING build_id",
                                current))


async def _stop(c, build_id, source_id, destination_id, day):
    await c.execute("INSERT INTO geo.tour_stop (build_id, source_id, day_index, destination_id, country, source) "
                    "VALUES ($1::uuid, $2, $3, $4::uuid, $5, 'overnight')",
                    build_id, source_id, day, destination_id, COUNTRY)


async def _as_reader(c):
    """A NOSUPERUSER, NOBYPASSRLS role with read access to the CIS schemas only (no tripplanner)."""
    await c.execute(f"CREATE ROLE {READER} NOLOGIN NOSUPERUSER NOBYPASSRLS")
    await c.execute(f"GRANT USAGE ON SCHEMA geo, shared, silver_aa_internal TO {READER}")
    await c.execute(f"GRANT SELECT ON geo.tour_place, geo.tour_stop, geo.graph_build, shared.destinations, "
                    f"silver_aa_internal.raw_tours TO {READER}")
    await c.execute(f"SET LOCAL ROLE {READER}")


@pytest.mark.asyncio
async def test_tour_candidates_by_source_key_current_build_and_keepers_only(conn):
    from services.photos.destinations import tour_candidates

    gate = await _destination(conn, "Brass Gate")
    stop = await _destination(conn, "Stop Town")
    dup = await _destination(conn, "Brass Gate Copy", duplicate_of=gate)
    old_stop = await _destination(conn, "Old Build Town")
    other = await _destination(conn, "Other Tour Place")
    group = str(uuid.uuid4())
    keeper_tour = await _tour(conn, group, "superseded")   # an older row of the group wrote the places
    variant = await _tour(conn, group)            # the photo's tour: the group's active row, same source key
    lone = await _tour(conn)                      # source_id = its own tour_id

    await _tour_place(conn, keeper_tour, group, gate, "Brass Gate of Zz")   # itinerary spelling differs
    await _tour_place(conn, keeper_tour, group, dup, "Brass Gate Copy", day=2)   # merged duplicate: never offered
    await _tour_place(conn, keeper_tour, group, None, "Unresolved", day=3)       # no destination: ignored
    await _tour_place(conn, lone, lone, other, "Other Tour Place")
    old = await _build(conn, current=False)
    cur = await _build(conn, current=True)
    await _stop(conn, cur, group, stop, 1)
    await _stop(conn, cur, group, gate, 2)
    await _stop(conn, old, group, old_stop, 3)                                   # previous build: ignored

    await _as_reader(conn)
    got = await tour_candidates(conn, [variant, lone, str(uuid.uuid4())])

    assert set(got) == {variant, lone}
    assert sorted(got[variant]) == sorted([(gate, "zz671 Brass Gate"), (gate, "Brass Gate of Zz"),
                                           (stop, "zz671 Stop Town")])
    assert got[lone] == [(other, "Other Tour Place"), (other, "zz671 Other Tour Place")]
    assert await tour_candidates(conn, []) == {}


async def _photo(c, tour_id, label, *, status="matched", destination_id=None, large=True):
    return str(await c.fetchval(
        """INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, file_name, place_label,
               tour_id, destination_id, status, s3_key_large)
           VALUES ($1, 'root', 'f', $2, $3 || '.jpg', $3, $4::uuid, $5::uuid, $6, $7) RETURNING id""",
        f"zz671-{uuid.uuid4()}", COUNTRY, label, tour_id, destination_id, status,
        "photos/zz/f-1600.webp" if large else None))


@pytest.mark.asyncio
async def test_job_sql_open_set_guarded_update_and_served_view(conn):
    from services.jobs import photo_destination_match_job as J

    gate = await _destination(conn, "Brass Gate")
    isle = await _destination(conn, "Zorvan Isle")
    tour = await _tour(conn)
    other_tour = await _tour(conn)
    open_a = await _photo(conn, tour, "Brass Gate")
    open_b = await _photo(conn, tour, "Zorvan Isle")
    moved = await _photo(conn, tour, "Brass Gate")                 # its tour changes after the plan
    await _photo(conn, tour, "Set", destination_id=isle)           # has a destination: not open
    await _photo(conn, tour, "Rejected", status="rejected")        # rejected: not open
    await _photo(conn, None, "No tour")                            # no tour: not open

    rows = await conn.fetch(J.OPEN_ROWS_SQL, COUNTRY, 100)
    assert sorted(r["id"] for r in rows) == sorted([open_a, open_b, moved])
    assert await conn.fetchval(J.OPEN_COUNT_SQL, COUNTRY) == 3
    assert len(await conn.fetch(J.OPEN_ROWS_SQL, COUNTRY, 2)) == 2

    await conn.execute("UPDATE shared.place_photo SET tour_id = $2::uuid WHERE id = $1::uuid", moved, other_tour)
    await conn.execute("UPDATE shared.place_photo SET destination_id = $2::uuid WHERE id = $1::uuid", open_b, isle)
    done = await conn.fetch(J.APPLY_SQL, [open_a, open_b, moved], [gate, gate, gate], [tour, tour, tour])
    assert [r["id"] for r in done] == [open_a]                     # open_b set meanwhile, moved changed tour
    got = {r["id"]: r["destination_id"] for r in await conn.fetch(
        "SELECT id::text AS id, destination_id::text AS destination_id FROM shared.place_photo "
        "WHERE id = ANY($1::uuid[])", [open_a, open_b, moved])}
    assert got == {open_a: gate, open_b: isle, moved: None}
    again = await conn.fetch(J.APPLY_SQL, [open_a], [isle], [tour])
    assert again == []                                             # idempotent: a set destination is never changed

    served = {r["destination_id"] for r in await conn.fetch(
        "SELECT destination_id::text AS destination_id FROM shared.v_destination_photos "
        "WHERE destination_id = ANY($1::uuid[])", [gate, isle])}
    assert served == {gate, isle}
    totals = await conn.fetchrow(J.SERVED_SQL)
    assert totals["photos"] >= 3 and totals["destinations"] >= 2
