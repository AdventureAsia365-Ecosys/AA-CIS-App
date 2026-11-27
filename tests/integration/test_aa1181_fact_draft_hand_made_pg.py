"""AA-1181 slice (a) on real Postgres: a supplier sheet never changes a hand-made tour (raw_tours.entry_origin
'manual', CIS 478). The merge of a staged re-import into one is refused (409 ``fact_draft_hand_made``, the staging row
stays pending, no draft); a sheet draft opened before the tour was hand-made is shown with ``hand_made`` and its publish
is refused with the tour untouched (it can still be discarded); a normal sheet tour still merges and publishes. The
other staging decisions (replace / update / keep_both) are refused on a hand-made match too (409 ``source_hand_made``,
services/tour_lifecycle), the staged row is listed with ``matched_hand_made`` and Create a new tour still works; an
older sheet version never goes live over a hand-made one (set-active). The rule is per key: a key whose live version
came from a sheet but which holds a hand-made version (left by a Replace before AA-1181) refuses merge, merge-preview
and publish too.

Tours are built in the real shape of a source version (``source_group_id`` = own id, version 1, active; a staged row
carries ``matched_source_group_id``), so the refusals stop the real data loss, not only a short cut of the code.

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
    """A live source version 1 of its own key (the shape supersede(create_new) and the content workspace write)."""
    tour_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "inclusions, src_itineraries, entry_origin, source_group_id, source_version, source_status) VALUES ($1, $2, "
        "$3, 'Tarnwick Valley Ride 3D', 'Thailand', '3 days', 'Water', 'Day 1: Arrive in Tarnwick. Day 2: Ride the "
        "valley. Day 3: Home.', $4, $1, 1, 'active')",
        uuid.UUID(tour_id), MASTER, BATCH_ID, origin)
    return tour_id


async def _staged(conn, tour_id, payload):
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.upload_staging (batch_id, tenant_id, parsed_payload, matched_tour_id, "
        "matched_source_group_id) VALUES ($1, $2, $3::jsonb, $4, $4) RETURNING id", uuid.UUID(BATCH_ID), MASTER,
        json.dumps(payload), uuid.UUID(tour_id)))


async def _join_as_superseded(conn, tour_id, key):
    """Make ``tour_id`` the superseded version 1 of ``key``'s tour key, whose live version becomes version 2: the shape
    a Replace on a hand-made match left before AA-1181 (a sheet version live over a hand-made one)."""
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET source_version = 2 WHERE tour_id = $1::uuid", key)
    await conn.execute(
        "UPDATE silver_aa_internal.raw_tours SET source_group_id = $2::uuid, source_version = 1, source_status = "
        "'superseded' WHERE tour_id = $1::uuid", tour_id, key)


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
async def test_a_sheet_never_merges_into_a_key_with_a_hand_made_version(conn):
    """The live version came from a sheet, but the key holds a superseded hand-made version: refused like the list."""
    sheet = await _tour(conn)
    hand = await _tour(conn, origin="manual")
    await _join_as_superseded(conn, hand, sheet)
    staging = await _staged(conn, sheet, {"duration": "4 days"})
    listed = await ap.list_upload_staging(BATCH_ID, _request(conn), x_admin_secret="s")
    assert next(i for i in listed["items"] if i["staging_id"] == staging)["matched_hand_made"] is True
    with pytest.raises(HTTPException) as e:
        await ap.merge_preview(staging, _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    with pytest.raises(HTTPException) as e:
        await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                _request(conn), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    assert await conn.fetchval("SELECT decision FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                               staging) == "pending"
    assert not await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tour_fact_draft "
                                   "WHERE tour_id = $1::uuid", sheet)
    state = await ap.get_fact_draft(sheet, _request(conn), x_admin_secret="s")
    assert state["hand_made"] is True and state["draft"] is None          # the drawer offers no Publish either
    assert await _duration(conn, sheet) == "3 days"
    assert await _origin_and_status(conn, sheet) == ("sheet", "active")
    assert await _origin_and_status(conn, hand) == ("manual", "superseded")


@pytest.mark.asyncio
async def test_a_sheet_draft_never_publishes_into_a_key_with_a_hand_made_version(conn):
    """A draft opened before a hand-made version joined the key is refused at publish, with nothing written."""
    sheet = await _tour(conn)
    staging = await _staged(conn, sheet, {"duration": "4 days"})
    out = await ap.decide_staging(staging, ap.DecideRequest(decision="merge", resolutions={}, reason="zz new sheet"),
                                  _request(conn), x_admin_secret="s")
    hand = await _tour(conn, origin="manual")
    await _join_as_superseded(conn, hand, sheet)
    fan = AsyncMock()
    with patch.object(ap.tour_detail, "_source_fanout_raw", fan):
        with pytest.raises(HTTPException) as e:
            await ap.publish_fact_draft(sheet, _request(conn, user=LEAD), x_admin_secret="s")
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    fan.assert_not_awaited()
    assert await _duration(conn, sheet) == "3 days"
    assert await conn.fetchval("SELECT status FROM silver_aa_internal.raw_tour_fact_draft WHERE id = $1",
                               out["fact_draft_id"]) == "draft"
    assert not await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tour_revision "
                                   "WHERE tour_id = $1::uuid AND source = 'excel_merge'", sheet)


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


async def _origin_and_status(conn, tour_id):
    r = await conn.fetchrow("SELECT entry_origin, source_status::text AS status FROM silver_aa_internal.raw_tours "
                            "WHERE tour_id = $1::uuid", tour_id)
    return r["entry_origin"], r["status"]


@pytest.mark.asyncio
async def test_a_sheet_never_replaces_or_joins_a_hand_made_tour(conn):
    tour_id = await _tour(conn, origin="manual")
    staging = await _staged(conn, tour_id, {"src_name": "Tarnwick Valley Ride 3D", "duration": "4 days",
                                            "src_itineraries": "Day 1: Arrive in Tarnwick."})
    listed = await ap.list_upload_staging(BATCH_ID, _request(conn), x_admin_secret="s")
    item = next(i for i in listed["items"] if i["staging_id"] == staging)
    assert item["matched_hand_made"] is True and item["matched_deleted"] is False
    versions = await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tours WHERE "
                                   "COALESCE(source_group_id, tour_id) = $1::uuid", tour_id)
    for decision in ("replace", "update", "keep_both"):
        with pytest.raises(HTTPException) as e:
            await ap.decide_staging(staging, ap.DecideRequest(decision=decision), _request(conn), x_admin_secret="s",
                                    x_admin_user_id=None)
        assert e.value.status_code == 409 and e.value.headers == {"X-Error-Code": "source_hand_made"}
        assert e.value.detail.startswith("source_hand_made: this tour was made by hand in the content workspace")
    assert await _origin_and_status(conn, tour_id) == ("manual", "active")      # still the live, hand-made tour
    assert await conn.fetchval("SELECT count(*) AS n FROM silver_aa_internal.raw_tours WHERE "
                               "COALESCE(source_group_id, tour_id) = $1::uuid", tour_id) == versions
    assert await conn.fetchval("SELECT decision FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                               staging) == "pending"
    with patch("services.tour_lifecycle.run_after_commit", AsyncMock()):
        out = await ap.decide_staging(staging, ap.DecideRequest(decision="create_new"), _request(conn),
                                      x_admin_secret="s", x_admin_user_id=None)
    assert out["committed"] is True and out["new_tour_id"] != tour_id               # a separate sheet tour
    assert await _origin_and_status(conn, out["new_tour_id"]) == ("sheet", "active")
    assert await _origin_and_status(conn, tour_id) == ("manual", "active")


@pytest.mark.asyncio
async def test_an_older_sheet_version_never_goes_live_over_a_hand_made_one(conn):
    from services.tour_lifecycle import LifecycleError, set_active
    hand = await _tour(conn, origin="manual")
    sheet = await _tour(conn)
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET source_group_id = $1::uuid, source_version = "
                       "CASE WHEN tour_id = $1::uuid THEN 2 ELSE 1 END, source_status = CASE WHEN tour_id = $1::uuid "
                       "THEN 'active' ELSE 'superseded' END::silver_aa_internal.source_status_enum "
                       "WHERE tour_id IN ($1::uuid, $2::uuid)", hand, sheet)
    with pytest.raises(LifecycleError) as e:
        await set_active(conn, sheet)
    assert e.value.status == 409 and e.value.code == "source_hand_made"
    assert await _origin_and_status(conn, hand) == ("manual", "active")
    assert await _origin_and_status(conn, sheet) == ("sheet", "superseded")
