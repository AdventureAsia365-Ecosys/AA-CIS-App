"""AA-995 (migration 290) — the photo read views give every copy of the same file (sha256) one canonical path.

Real Postgres; everything is seeded in one transaction that is rolled back.
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
SHA_A = "a" * 64
SHA_B = "b" * 64


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


async def _tour(c):
    tour_id = str(uuid.uuid4())
    await c.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER)
    await c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                           source_status)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, 'zz995 tour', 'Vietnam', 'active')""",
                    tour_id, MASTER, BATCH_ID)
    return tour_id


async def _photo(c, *, sha, tour_id=None, destination_id=None, status="matched", served=True, age="0 s"):
    pid = str(uuid.uuid4())
    await c.execute(
        """INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name, tour_id,
               destination_id, match_source, status, s3_key_large, s3_key_small, sha256, created_at)
           VALUES ($1::uuid, $2, 'root', 'Vietnam/zz995', 'Vietnam', $3, $4::uuid, $5::uuid, 'auto', $6, $7, $7, $8,
                   now() - $9::interval)""",
        pid, f"zz995-{pid}", f"{pid}.jpg", tour_id, destination_id, status,
        "photos/k.jpg" if served else None, sha, age)
    return pid


@pytest.mark.asyncio
async def test_copies_share_the_earliest_served_row_path(conn):
    c = conn
    t1, t2 = await _tour(c), await _tour(c)
    dest = str(await c.fetchval("INSERT INTO shared.destinations (name, country, lat, lng) "
                                "VALUES ('zz995 place', 'Vietnam', 10, 100) RETURNING id"))
    # Earliest copy is rejected or not served: it never becomes the canonical row.
    await _photo(c, sha=SHA_A, status="rejected", age="3 days")
    await _photo(c, sha=SHA_A, served=False, age="2 days")
    first = await _photo(c, sha=SHA_A, tour_id=t1, age="1 day")
    second = await _photo(c, sha=SHA_A, tour_id=t2)
    on_place = await _photo(c, sha=SHA_A, destination_id=dest)
    other = await _photo(c, sha=SHA_B, tour_id=t2, age="1 hour")
    no_sha = await _photo(c, sha=None, tour_id=t1)

    rows = {r["photo_id"]: r["path"] for r in await c.fetch(
        "SELECT photo_id::text, path FROM shared.v_tour_photos WHERE tour_id = ANY($1::uuid[])", [t1, t2])}
    assert rows[first] == f"/content/photos/{first}"
    assert rows[second] == f"/content/photos/{first}"        # same file, another tour: the canonical path
    assert rows[other] == f"/content/photos/{other}"         # different file keeps its own path
    assert rows[no_sha] == f"/content/photos/{no_sha}"       # no hash: own path
    assert set(rows) == {first, second, other, no_sha}       # photo_id is still the row's own id

    path = await c.fetchval("SELECT path FROM shared.v_destination_photos WHERE destination_id = $1::uuid", dest)
    assert path == f"/content/photos/{first}"


@pytest.mark.asyncio
async def test_columns_unchanged(conn):
    cols = [r["attname"] for r in await conn.fetch(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'shared.v_tour_photos'::regclass AND attnum > 0 "
        "ORDER BY attnum")]
    assert cols == ["tour_id", "photo_id", "path", "place_label", "destination_id", "width", "height",
                    "match_source", "updated_at", "position"]
