"""AA-1127 (C1, S283 UI audit) — Photos › summary on real Postgres: "Tours with photos" counts the served tours.

A raw tour that is active but has no live Master Content row is not in the base, and a photo matched to it is not
covered; the served tour and its photo are. Runs in one transaction that is rolled back; invented names only."""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _constants import BATCH_ID, TENANT_ID as MASTER  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_photos  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
COUNTRY = "Bhutan"   # a valid country (raw_tours CHECK); the test reads deltas, so other rows do not matter


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


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


async def _raw(c, name):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                    "duration, source_status) VALUES ($1, $2, $3, $4, $5, '4 days', 'active')",
                    tid, MASTER, BATCH_ID, name, COUNTRY)
    return tid


async def _publish(c, tid, name, status="active"):
    await c.execute("INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
                    "aa_summary, aa_highlights, master_status, published_at) VALUES ($1, $2, $3, $4, 'S', "
                    "'[]'::jsonb, $5, now())", tid, uuid.uuid4(), MASTER, name, status)


async def _photo(c, tid):
    await c.execute("INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, file_name, "
                    "status, tour_id) VALUES ($1, 'zz-root-aa1127', 'x', $2, 'a.jpg', 'matched', $3)",
                    f"zz-{uuid.uuid4().hex[:8]}", COUNTRY, tid)


@pytest.mark.asyncio
async def test_base_and_covered_count_served_tours_only(conn):
    req = MagicMock()
    req.app.state.pool = _OneConnPool(conn)
    before = _row(await admin_photos.summary(req))
    served = await _raw(conn, "Zz Lantern Ridge Trek")
    await _publish(conn, served, "Zz Lantern Ridge Trek")
    unpublished = await _raw(conn, "Zz Quiet Harbour Walk")              # active raw, never published
    paused = await _raw(conn, "Zz Copper Valley Ride")
    await _publish(conn, paused, "Zz Copper Valley Ride", status="inactive")  # not live Master Content
    for tid in (served, unpublished, paused):
        await _photo(conn, tid)
    after = _row(await admin_photos.summary(req))
    delta = {k: after[k] - before[k] for k in ("active_tours", "tours_covered", "photos", "with_tour")}
    assert delta == {"active_tours": 1, "tours_covered": 1, "photos": 3, "with_tour": 3}


def _row(out):
    rows = [r for r in out["countries"] if r["country"] == COUNTRY]
    return rows[0] if rows else {"active_tours": 0, "tours_covered": 0, "photos": 0, "with_tour": 0}
