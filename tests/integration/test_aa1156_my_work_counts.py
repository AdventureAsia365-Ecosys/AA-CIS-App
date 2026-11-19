"""AA-1156 — My work on real Postgres: each tile's count equals the total its target page lists for the same filter.

The fixtures add waiting and not-waiting rows to every queue (Review Queue, supply entities, AA content, place cards,
compliance flags); the test then asks GET /admin/my-work/summary (as admin, so every tile) and each target page's own
list route with the tile's filter, and compares the numbers (and that the fixture rows moved them). It also checks the
oldest time and that a content role gets only its tiles.

conftest replays every migration. Each test runs in one transaction that is rolled back. Invented names and places only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.core.staff_authz import principal_from_role  # noqa: E402
from api.routers import admin_content_quality, admin_my_work, admin_pipeline  # noqa: E402
from api.routers import admin_tenant_compliance as atc  # noqa: E402
from services.aa_content import store  # noqa: E402
from services.supply_content import review as supply  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
LONG_AGO = datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc)   # older than any row the replay may hold


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


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


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


async def _review_row(conn, status: str, created_at=None):
    tour = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries) VALUES ($1, $2, $3, 'Quillmere Ridge 3D', 'Thailand', '3 days', 'Day 1: Arrive in "
        "Quillmere. Day 2: Walk the Brannoch forest trail.')", tour, MASTER, BATCH_ID)
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_highlights, status) VALUES ($1, $2, 1, 'Quillmere Ridge', 'Three days in the hills', '[]'::jsonb, "
        "'hitl'::content_status_enum) RETURNING id", tour, MASTER)
    await conn.execute(
        "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, failure_summary, "
        "score_overall, review_status, created_at) VALUES ($1, $2, $3, 'LOW_SCORE', 4.0, $4, COALESCE($5, now()))",
        tour, gc, MASTER, status, created_at)


async def _supply(conn, status: str, updated_at=None):
    await conn.execute(
        "INSERT INTO shared.supply_entity (source_system, source_id, entity_type, title, facts, "
        "applied_supply_version, active, content_status, updated_at) VALUES ('booking', $1, 'hotel', "
        "'Lantern River Lodge', '{}'::jsonb, 1, true, $2, COALESCE($3, now()))",
        str(uuid.uuid4()), status, updated_at)


async def _aa_item(conn, *, publish: bool):
    item = await store.create_item(conn, kind="starter_template", locale="en",
                                   group_key=f"aa1156-{uuid.uuid4().hex[:10]}", title="Walk the Brannoch trail",
                                   body_markdown="A short walk along an invented river.", applies_to={},
                                   actor="test:AA-1156")
    if publish:
        cid = uuid.UUID(str(item["content_id"]))
        row = await conn.fetchrow(store.ITEM_SQL, cid)
        await store.publish(conn, cid, expected_updated_at=row["updated_at"], actor="test:AA-1156")


async def _card(conn, status: str, created_at=None):
    key = f"brannoch-falls-{uuid.uuid4().hex[:8]}|laos"
    await conn.execute(
        "INSERT INTO acp_shared.place_card (place_key, display_name, country, facts, status, created_at) "
        "VALUES ($1, 'Brannoch Falls', 'Laos', $2::jsonb, $3, COALESCE($4, now()))",
        key, json.dumps([]), status, created_at)


async def _flag(conn, state: str):
    resolved = state == "resolved"
    await conn.execute(
        "INSERT INTO acp_shared.content_compliance_flag (tenant_id, kind, stage, piece_id, codes, detail, "
        "tenant_message, action, state, resolution, resolved_at) VALUES ($1, 'subject_gate', 'write', $2, "
        "ARRAY['no_aa_subject'], '{}'::jsonb, 'This post names no Adventure Asia tour.', 'none', $3, $4, $5)",
        uuid.UUID(MASTER), uuid.uuid4(), state, "passed" if resolved else None,
        datetime.now(timezone.utc) if resolved else None)


async def _page_totals(conn, monkeypatch) -> dict:
    """The total each target page shows for the tile's filter, read through the page's own list route."""
    monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda _s: None)
    monkeypatch.setattr(admin_content_quality, "verify_admin_secret", lambda _s: None)
    req = _req(conn)
    review = await admin_pipeline.admin_review_queue(req, x_admin_secret="x", page_size=1)   # page default: pending
    supply_drafts = await supply.list_entities(conn, status="in_review", limit=1, offset=0)
    supply_none = await supply.list_entities(conn, status="draft", limit=1, offset=0)
    aa = await store.list_items(conn, kind=None, locale=None, status="draft", country=None, limit=1, offset=0)
    cards = await admin_content_quality.list_place_cards(req, limit=1, offset=0, status="auto_checked", q=None,
                                                         x_admin_secret="x")
    # Compliance: list_flags' own total query with the page's default (state open, every kind and tenant). Its row
    # query joins acp_shared.content_piece columns this test DB's replay does not create, so the rows are not read.
    flags = {"total": int(await conn.fetchval(atc.COUNT_SQL, "open", None, None))}
    return {"review_waiting": review["pagination"]["total"], "supply_drafts": supply_drafts["total"],
            "supply_no_description": supply_none["total"], "aa_content_drafts": aa["total"],
            "place_cards_to_review": cards["total"], "compliance_holds": flags["total"]}


async def _summary(conn, role: str) -> dict:
    out = await admin_my_work.my_work_summary(_req(conn), principal_from_role(role))
    return {t.key: t for t in out.tiles}


@pytest.mark.asyncio
async def test_every_tile_counts_what_its_target_page_lists(conn, monkeypatch):
    before = {k: t.count for k, t in (await _summary(conn, "admin")).items()}
    for status in ("pending", "pending", "approved", "rejected"):
        await _review_row(conn, status)
    for status in ("in_review", "draft", "draft", "stale"):
        await _supply(conn, status)
    await _aa_item(conn, publish=False)
    await _aa_item(conn, publish=True)
    for status in ("auto_checked", "auto_checked", "verified", "rejected"):
        await _card(conn, status)
    for state in ("open", "resolved"):
        await _flag(conn, state)

    tiles = await _summary(conn, "admin")
    assert list(tiles) == [t.key for t in admin_my_work.TILES]
    assert all(t.available for t in tiles.values())
    assert {k: t.count for k, t in tiles.items()} == await _page_totals(conn, monkeypatch)
    added = {"review_waiting": 2, "supply_drafts": 1, "supply_no_description": 2, "aa_content_drafts": 1,
             "place_cards_to_review": 2, "compliance_holds": 1}
    assert {k: tiles[k].count - before[k] for k in added} == added


@pytest.mark.asyncio
async def test_the_oldest_time_is_the_oldest_waiting_item(conn):
    await _review_row(conn, "pending", created_at=LONG_AGO)
    await _review_row(conn, "approved", created_at=LONG_AGO - timedelta(days=30))   # closed: not waiting
    await _supply(conn, "in_review", updated_at=LONG_AGO)
    await _card(conn, "auto_checked", created_at=LONG_AGO)
    await _card(conn, "verified", created_at=LONG_AGO - timedelta(days=30))
    tiles = await _summary(conn, "admin")
    for key in ("review_waiting", "supply_drafts", "place_cards_to_review"):
        assert tiles[key].oldest_at == LONG_AGO, key


@pytest.mark.asyncio
async def test_a_trashed_source_leaves_the_review_tile_like_the_page(conn, monkeypatch):
    """AA-986 L1: a pending row whose source is no longer live is hidden from the page, so not counted either."""
    await _review_row(conn, "pending")
    before = (await _summary(conn, "content"))["review_waiting"].count
    tour = await conn.fetchval("SELECT tour_id FROM silver_aa_internal.review_queue ORDER BY created_at DESC LIMIT 1")
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET deleted_at = now() WHERE tour_id = $1", tour)
    tiles = await _summary(conn, "content")
    assert tiles["review_waiting"].count == before - 1
    assert tiles["review_waiting"].count == (await _page_totals(conn, monkeypatch))["review_waiting"]


@pytest.mark.asyncio
async def test_a_content_role_gets_only_its_tiles(conn):
    assert list(await _summary(conn, "content")) == [
        "review_waiting", "supply_drafts", "supply_no_description", "aa_content_drafts"]
    assert list(await _summary(conn, "tenant_qa")) == ["place_cards_to_review", "compliance_holds"]
