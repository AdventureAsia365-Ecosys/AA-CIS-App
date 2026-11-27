"""AA-1187 (S296) — which existing tour an uploaded Excel row is, on real Postgres: the one rule the Upload preview
and Commit share (services/ingestion/tour_match.py). Same Name + same Provider first; else same Name + same country
(a Provider change, or a Provider filled in where the tour had none, never makes a new tour); the same Name in
another country is another tour. Each test runs in one transaction that is rolled back. Invented names only."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ingestion.tour_match import match_existing  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(conn, name, provider, country, *, age_days=0, deleted=False):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, provider, country, "
        "duration, src_itineraries, ingest_at, deleted_at) VALUES ($1, $2, $3, $4, $5, $6, '3 days', "
        "'Day 1: Arrive. Day 2: Walk the ridge. Day 3: Home.', $7, $8)",
        tour_id, MASTER, BATCH_ID, name, provider, country, T0 - timedelta(days=age_days),
        T0 if deleted else None)
    return tour_id


def _row(name, provider, country):
    return {"src_name": name, "provider": provider, "country": country}


@pytest.mark.asyncio
async def test_same_name_and_provider_wins(conn):
    same = await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", "Laos", age_days=5)
    await _tour(conn, "Zorvane Ridge Walk", "Other Operator", "Laos")
    found = await match_existing(conn, MASTER, _row(" zorvane ridge walk ", "QUILLON TREKS", "Laos"))
    assert found["tour_id"] == same and found["same_provider"] is True and found["live"] is True


@pytest.mark.asyncio
async def test_a_provider_change_matches_by_country_instead_of_making_a_new_tour(conn):
    tour = await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", "Laos")
    found = await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Quillon Trek Co", "laos"))
    assert found["tour_id"] == tour and found["same_provider"] is False and found["provider"] == "Quillon Treks"


@pytest.mark.asyncio
async def test_a_provider_filled_in_where_the_tour_had_none_matches(conn):
    tour = await _tour(conn, "Zorvane Ridge Walk", None, "Laos")
    found = await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Quillon Treks", "Laos"))
    assert found["tour_id"] == tour


@pytest.mark.asyncio
async def test_the_same_name_in_another_country_is_another_tour(conn):
    await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", "Laos")
    assert await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Brisk Paths", "Cambodia")) is None
    assert await match_existing(conn, MASTER, _row("Velmora Lake Loop", "Quillon Treks", "Laos")) is None


@pytest.mark.asyncio
async def test_an_unknown_country_on_either_side_still_matches_the_name(conn):
    tour = await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", None)
    assert (await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Brisk Paths", "Laos")))["tour_id"] == tour
    other = await _tour(conn, "Velmora Lake Loop", "Quillon Treks", "Laos")
    assert (await match_existing(conn, MASTER, _row("Velmora Lake Loop", "Brisk Paths", None)))["tour_id"] == other


@pytest.mark.asyncio
async def test_same_country_is_preferred_over_an_unknown_country(conn):
    await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", None)
    laos = await _tour(conn, "Zorvane Ridge Walk", "Brisk Paths", "Laos", age_days=9)
    found = await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Third Operator", "Laos"))
    assert found["tour_id"] == laos


@pytest.mark.asyncio
async def test_a_deleted_tour_is_matched_and_says_so(conn):
    tour = await _tour(conn, "Zorvane Ridge Walk", "Quillon Treks", "Laos", deleted=True)
    found = await match_existing(conn, MASTER, _row("Zorvane Ridge Walk", "Brisk Paths", "Laos"))
    assert found["tour_id"] == tour and found["live"] is False and found["key_deleted_at"] == T0
