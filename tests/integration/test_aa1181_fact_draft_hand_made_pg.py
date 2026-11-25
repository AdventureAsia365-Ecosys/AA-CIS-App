"""AA-1181 slice (a) on real Postgres: a supplier sheet never changes a hand-made tour (raw_tours.entry_origin
'manual', CIS 478). The merge of a staged re-import into one is refused (409 ``fact_draft_hand_made``, the staging row
stays pending, no draft); a sheet draft opened before the tour was hand-made is shown with ``hand_made`` and its publish
is refused with the tour untouched (it can still be discarded); a normal sheet tour still merges and publishes.

Needs CIS 457 and 478 (skipped without them). Each test runs in one transaction that is rolled back. Invented names and
places only."""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_pipeline as ap  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
EDITOR = "00000000-0000-4000-8000-0000000011e1"
LEAD = "00000000-0000-4000-8000-0000000011e2"


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _request(conn, *, role="content_lead", user=EDITOR):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))), scope={},
                           headers={"x-admin-role": role, "x-admin-user-id": user}, json=AsyncMock(return_value={}))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    ready = await c.fetchval(
        "SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = 'silver_aa_internal' AND "
        "((table_name = 'generated_content' AND column_name = 'origin') OR "
        "(table_name = 'raw_tours' AND column_name = 'entry_origin'))")
    if ready < 2:
        await c.close()
        pytest.skip("CIS 457 and 478 are not applied on this database")
    tr = c.transaction()
    await tr.start()
    try:
        with patch.object(ap, "verify_admin_secret", MagicMock()):
            yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(conn, *, origin="sheet"):
    tour_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "inclusions, src_itineraries, entry_origin) VALUES ($1, $2, $3, 'Tarnwick Valley Ride 3D', 'Thailand', "
        "'3 days', 'Water', 'Day 1: Arrive in Tarnwick. Day 2: Ride the valley. Day 3: Home.', $4)",
        uuid.UUID(tour_id), MASTER, BATCH_ID, origin)
    return tour_id


async def _staged(conn, tour_id, payload):
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.upload_staging (batch_id, tenant_id, parsed_payload, matched_tour_id) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING id", uuid.UUID(BATCH_ID), MASTER, json.dumps(payload),
        uuid.UUID(tour_id)))


async def _duration(conn, tour_id):
    return await conn.fetchval("SELECT duration FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid", tour_id)


@pytest.mark.asyncio
async def test_a_sheet_never_merges_into_a_hand_made_tour(conn):
    tour_id = await _tour(conn, origin="manual")
    staging = await _staged(conn, tour_id, {"duration": "4 days"})
    with pytest.raises(HTTPException) as e:
        await ap.merge_preview(staging, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    with pytest.raises(HTTPException) as e:
        await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    assert await conn.fetchval("SELECT decision FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                               staging) == "pending"     # the row waits for another decision (Bypass)
    assert not await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tour_fact_draft "
                                   "WHERE tour_id = $1::uuid", tour_id)
    assert await _duration(conn, tour_id) == "3 days"


@pytest.mark.asyncio
async def test_a_sheet_draft_never_publishes_over_a_hand_made_tour(conn):
    tour_id = await _tour(conn)
    staging = await _staged(conn, tour_id, {"duration": "4 days"})
    out = await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                  _request(conn), x_admin_secret="s")
    # the draft was opened while the tour came from a sheet; the tour is hand-made now
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET entry_origin = 'manual' WHERE tour_id = $1::uuid",
                       tour_id)
    state = await ap.get_fact_draft(tour_id, _request(conn), x_admin_secret="s")
    assert state["hand_made"] is True and state["draft"]["id"] == out["fact_draft_id"]
    fan = AsyncMock()
    with patch.object(ap.tour_detail, "_source_fanout_raw", fan):
        with pytest.raises(HTTPException) as e:          # refused for any publisher, before the gate
            await ap.publish_fact_draft(tour_id, _request(conn, user=LEAD), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    assert e.value.headers == {"X-Error-Code": "fact_draft_hand_made"}
    fan.assert_not_awaited()
    assert await _duration(conn, tour_id) == "3 days"
    assert await conn.fetchval("SELECT status FROM silver_aa_internal.raw_tour_fact_draft WHERE id = $1",
                               out["fact_draft_id"]) == "draft"
    assert not await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tour_revision "
                                   "WHERE tour_id = $1::uuid AND source = 'excel_merge'", tour_id)
    gone = await ap.discard_fact_draft(tour_id, _request(conn), x_admin_secret="s")   # Discard still works
    assert gone["discarded"] == out["fact_draft_id"]


@pytest.mark.asyncio
async def test_a_sheet_tour_still_merges_and_publishes(conn):
    """Regression: a normal Excel tour's sheet draft is merged and published as before."""
    tour_id = await _tour(conn)
    staging = await _staged(conn, tour_id, {"duration": "4 days"})
    out = await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                  _request(conn), x_admin_secret="s")
    state = await ap.get_fact_draft(tour_id, _request(conn), x_admin_secret="s")
    assert state["hand_made"] is False and state["draft"]["id"] == out["fact_draft_id"]
    with patch.object(ap.tour_detail, "_source_fanout_raw", AsyncMock()):
        pub = await ap.publish_fact_draft(tour_id, _request(conn, user=LEAD), x_admin_secret="s")
    assert pub["published"] is True and pub["updated"] == ["duration"]
    assert await _duration(conn, tour_id) == "4 days"
