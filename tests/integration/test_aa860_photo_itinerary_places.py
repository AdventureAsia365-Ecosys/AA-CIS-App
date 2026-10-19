"""AA-860 (X1 S15) — services.photos.sync.itinerary_places reads the CIS place pipeline (schema geo), not the
frozen tripplanner.* extraction tables.

Real Postgres (lesson S231: new SQL needs an integration test). Everything is seeded inside one transaction
that is rolled back. The read runs as a plain NOSUPERUSER role that has no privilege on schema tripplanner,
the way cis_app will run it after X1 S18 (published_tours / raw_tours are FORCE-RLS: the function pins
app.tenant_id to the AA master tenant itself).
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
COUNTRY = "Zz860land"
READER = "zz860_photo_reader"
SOURCE = str(uuid.uuid4())             # source_id = COALESCE(source_group_id, tour_id)::text
DEAD_SOURCE = str(uuid.uuid4())


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


async def _destination(c, name, country=COUNTRY):
    return str(await c.fetchval("INSERT INTO shared.destinations (name, country, lat, lng) "
                                "VALUES ($1, $2, 10, 100) RETURNING id", f"zz860 {name}", country))


async def _live_tour(c, source_group_id, *, master_status="active"):
    tour_id = str(uuid.uuid4())
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER)
    await c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                           source_group_id, source_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, 'zz860 tour', 'Vietnam', $4::uuid, 'active')""",
                    tour_id, MASTER, BATCH_ID, source_group_id)
    await c.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id,
                           aa_name, aa_summary, aa_highlights, aa_itineraries, master_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, 'zz860 tour', 'S', '[]'::jsonb, 'Day 1', $4)""",
                    tour_id, str(uuid.uuid4()), MASTER, master_status)
    return tour_id


async def _tour_place(c, tour_id, source_id, destination_id, day=1):
    await c.execute("""INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm,
                           country, country_norm, destination_id, activity, intensity_level, duration_hint,
                           text_extract, text_hash)
                       VALUES ($1::uuid, $2, $3::uuid, $4, 'p', 'p', $5, lower($5), $6::uuid,
                               'cultural_heritage', 'leisurely', 'half_day', 't', 'h')""",
                    str(uuid.uuid4()), source_id, tour_id, day, COUNTRY, destination_id)


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
    await c.execute(f"GRANT USAGE ON SCHEMA geo, shared, gold_aa_internal, silver_aa_internal TO {READER}")
    await c.execute(f"GRANT SELECT ON geo.tour_place, geo.tour_stop, geo.graph_build, shared.destinations, "
                    f"gold_aa_internal.published_tours, silver_aa_internal.raw_tours TO {READER}")
    await c.execute(f"SET LOCAL ROLE {READER}")


@pytest.mark.asyncio
async def test_itinerary_places_from_geo_current_build_only(conn):
    from services.photos.sync import itinerary_places

    d_place = await _destination(conn, "Place")
    d_stop = await _destination(conn, "Stop")
    d_old_build = await _destination(conn, "OldBuild")
    d_other_country = await _destination(conn, "Elsewhere", country="Zz860other")
    d_dead_source = await _destination(conn, "DeadSource")
    tour = await _live_tour(conn, SOURCE)
    inactive = await _live_tour(conn, DEAD_SOURCE, master_status="inactive")

    await _tour_place(conn, tour, SOURCE, d_place, day=1)
    await _tour_place(conn, tour, SOURCE, d_other_country, day=2)   # other country: filtered out
    await _tour_place(conn, tour, SOURCE, None, day=3)              # unresolved place: ignored
    old = await _build(conn, current=False)
    cur = await _build(conn, current=True)
    await _stop(conn, cur, SOURCE, d_stop, 1)
    await _stop(conn, cur, SOURCE, d_place, 2)                      # also a tour_place: listed once
    await _stop(conn, old, SOURCE, d_old_build, 3)                  # previous build: ignored
    await _stop(conn, cur, DEAD_SOURCE, d_dead_source, 1)  # source with no live tour: ignored

    await _as_reader(conn)
    got = await itinerary_places(conn, COUNTRY)

    names = {tid: sorted(name for _, name in places) for tid, places in got.items()}
    assert names == {
        tour: ["zz860 Place", "zz860 Stop"],
    }
    assert inactive not in got
    assert {dest for _, places in got.items() for dest, _ in places} <= {d_place, d_stop}
    assert all(isinstance(t, str) for t in got)


@pytest.mark.asyncio
async def test_itinerary_places_empty_country(conn):
    from services.photos.sync import itinerary_places

    await _as_reader(conn)
    assert await itinerary_places(conn, "Zz860nowhere") == {}


class _BrokenConn:
    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def is_in_transaction(self):
        return False

    async def execute(self, *args):
        raise asyncpg.exceptions.InsufficientPrivilegeError("permission denied for schema geo")

    async def fetch(self, *args):
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_itinerary_places_fails_open_but_logs_an_error():
    from structlog.testing import capture_logs

    from services.photos.sync import itinerary_places

    with capture_logs() as logs:
        assert await itinerary_places(_BrokenConn(), COUNTRY) == {}
    errors = [e for e in logs if e["event"] == "photo_sync_itinerary_places_unavailable"]
    assert errors and errors[0]["log_level"] == "error"
    assert "permission denied" in errors[0]["error"]
