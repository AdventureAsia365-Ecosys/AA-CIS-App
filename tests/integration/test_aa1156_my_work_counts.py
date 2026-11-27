"""AA-1156 — My work on real Postgres: each tile's count equals the total its target page lists for the same filter.
AA-1190: the approval tiles (Settings proposals, compliance exceptions, data erasure), the platform tile, the Review
Queue split into ready / held by policy.

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
from api.services import compliance_exceptions, settings_registry  # noqa: E402
from services.quality import policy_review  # noqa: E402
from services.tenant_erasure import store as erasure_store  # noqa: E402
from shared import settings as shared_settings  # noqa: E402
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
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))), headers={})


async def _review_row(conn, status: str, created_at=None, *, policy: bool = False) -> uuid.UUID:
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
    if policy:   # AA-1049: an elephant-riding policy review (the marker the page's filter finds)
        await conn.execute("UPDATE silver_aa_internal.review_queue SET escalate_detail = $2::jsonb WHERE tour_id = $1",
                           tour, json.dumps(policy_review.detail([{"where": "highlights", "field": "highlights"}])))
    return tour


_VERSION_SQL = (
    "INSERT INTO shared.supply_content_version (entity_id, content_version, supply_version, origin, description, "
    "highlights, good_to_know, photos, status, created_by, created_at, reviewed_by, reviewed_at) VALUES ($1, $2, 1, "
    "'llm', 'A quiet lodge on an invented river.', ARRAY[]::text[], ARRAY[]::text[], '[]'::jsonb, $3, 'test:AA-1156', "
    "COALESCE($4, now()), $5, $6) RETURNING id")


async def _supply(conn, status: str, updated_at=None, *, draft: bool = False, draft_at=None):
    """A supply entity in ``status`` (published and stale get an approved version first, as after a publish);
    ``draft`` adds one open draft version created at ``draft_at``."""
    entity_id = await conn.fetchval(
        "INSERT INTO shared.supply_entity (source_system, source_id, entity_type, title, facts, "
        "applied_supply_version, active, content_status, updated_at) VALUES ('booking', $1, 'hotel', "
        "'Lantern River Lodge', '{}'::jsonb, 1, true, $2, COALESCE($3, now())) RETURNING id",
        str(uuid.uuid4()), "stale" if status == "published" else status, updated_at)
    number = 1
    if status in ("published", "stale"):
        approved = await conn.fetchval(_VERSION_SQL, entity_id, number, "approved", LONG_AGO, "test:AA-1156",
                                       LONG_AGO)
        await conn.execute("UPDATE shared.supply_entity SET content_status = $2, published_version_id = $3, "
                           "updated_at = COALESCE($4, now()) WHERE id = $1", entity_id, status, approved, updated_at)
        number += 1
    if draft:
        await conn.fetchval(_VERSION_SQL, entity_id, number, "draft", draft_at, None, None)


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


CIS_KEY = next(k for k in shared_settings.READER.keys() if shared_settings.definition(k).app == "cis")


async def _proposal(conn, *, by: str = "admin:00000000-0000-0000-0000-0000000000aa", key: str = CIS_KEY,
                    tenant: str | None = None):
    version = await conn.fetchval("SELECT coalesce(max(version), 0) + 1 FROM shared.setting_value WHERE key = $1 "
                                  "AND tenant_id IS NOT DISTINCT FROM $2::uuid", key, tenant)
    await conn.execute(
        "INSERT INTO shared.setting_value (key, tenant_id, value, version, proposed_by, reason) "
        "VALUES ($1, $2::uuid, $5::jsonb, $3, $4, 'An invented reason long enough for the check.')",
        key, tenant, version, by, json.dumps(shared_settings.definition(key).default_value))


async def _exception(conn, status: str, *, by: str = "admin:00000000-0000-0000-0000-0000000000aa"):
    decided = status != "proposed"
    await conn.execute(
        "INSERT INTO compliance.exception (subject_kind, subject_ref, title, reason, risk_level, status, proposed_by, "
        "expires_on, decided_by, decided_at) VALUES ('check', $1, 'An invented risk', 'An invented reason long "
        "enough for the check.', 'low', $2, $3, current_date + 30, $4, $5)",
        f"zz-{uuid.uuid4().hex[:8]}", status, by, "admin:someone-else" if decided else None,
        datetime.now(timezone.utc) if decided else None)


async def _erasure(conn, phase: str):
    tid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
        "is_canary) VALUES ($1::uuid, 'Lanternfall Voyages synthetic', $2, 'starter'::plan_tier_enum, 3, 60, false, "
        "true)", tid, f"zz1190-{tid[:12]}")
    req = await erasure_store.create_request(conn, tid, opened_at=datetime.now(timezone.utc), mode="drill",
                                             actor="test:AA-1190")
    if phase != "dry_run":
        assert await erasure_store.move(conn, req["request_id"], ["dry_run"], phase, combined_fingerprint="a" * 64,
                                        fingerprint_at=datetime.now(timezone.utc))


async def _ops_event(conn, kind: str, *, ended: bool = False, severity: str = "sev2"):
    await conn.execute(
        "INSERT INTO acp_shared.ops_event (occurred_at, ended_at, kind, severity, app, subject, title, details, "
        "dedupe_key) VALUES (now(), $1, $2, $3, 'cis', 'invented-service', 'An invented problem', '{}'::jsonb, $4)",
        datetime.now(timezone.utc) if ended else None, kind, severity, f"zz1190:{uuid.uuid4().hex}")


async def _page_totals(conn, monkeypatch) -> dict:
    """The total each target page shows for the tile's filter, read through the page's own list route."""
    monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda _s: None)
    monkeypatch.setattr(admin_content_quality, "verify_admin_secret", lambda _s: None)
    req = _req(conn)
    review = await admin_pipeline.admin_review_queue(req, x_admin_secret="x", page_size=1, policy="none")
    held = await admin_pipeline.admin_review_queue(req, x_admin_secret="x", page_size=1, policy="elephant_riding")
    supply_drafts = await supply.list_entities(conn, has_draft=True, limit=1, offset=0)
    supply_none = await supply.list_entities(conn, status="draft", limit=1, offset=0)
    aa = await store.list_items(conn, kind=None, locale=None, status="draft", country=None, limit=1, offset=0)
    cards = await admin_content_quality.list_place_cards(req, limit=1, offset=0, status="auto_checked", q=None,
                                                         x_admin_secret="x")
    # Compliance: list_flags' own total query with the page's default (state open, every kind and tenant). Its row
    # query joins acp_shared.content_piece columns this test DB's replay does not create, so the rows are not read.
    flags = {"total": int(await conn.fetchval(atc.COUNT_SQL, "open", None, None))}
    # Settings: the open proposals the page lists on the CIS tab (two-person approval off: nothing left out)
    registry = await settings_registry.list_settings(req.app.state.pool, app="cis")
    exceptions = await compliance_exceptions.list_exceptions(req.app.state.pool)
    erasures = await erasure_store.list_requests(conn, limit=200)
    return {"settings_to_approve": sum(len(i["proposals"]) for i in registry["settings"]),
            "compliance_exceptions_to_approve": exceptions["counts"]["proposed"],
            "erasure_to_approve": sum(1 for r in erasures if r["phase"] == "awaiting_approval"),
            "review_held_by_policy": held["pagination"]["total"],
            "review_waiting": review["pagination"]["total"], "supply_drafts": supply_drafts["total"],
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
    # waiting for review: a first draft (in_review) and redrafts that keep the entity's status (stale after new facts
    # from Booking, published after a human edit); not waiting: no text yet (the other tile) and stale with no redraft
    for status in ("in_review", "stale", "published"):
        await _supply(conn, status, draft=True)
    for status in ("draft", "draft", "stale", "published"):
        await _supply(conn, status)
    await _aa_item(conn, publish=False)
    await _aa_item(conn, publish=True)
    for status in ("auto_checked", "auto_checked", "verified", "rejected"):
        await _card(conn, status)
    for state in ("open", "resolved"):
        await _flag(conn, state)
    await _review_row(conn, "pending", policy=True)
    await _proposal(conn)
    for status in ("proposed", "proposed", "approved"):
        await _exception(conn, status)
    for phase in ("awaiting_approval", "dry_run"):
        await _erasure(conn, phase)
    await _ops_event(conn, "alarm")
    await _ops_event(conn, "service_down", ended=True)      # closed: not open
    await _ops_event(conn, "deploy", severity="info")        # a deploy is not a problem

    tiles = await _summary(conn, "admin")
    assert list(tiles) == [t.key for t in admin_my_work.TILES]
    assert all(t.available for t in tiles.values())
    counts = {k: t.count for k, t in tiles.items() if k != "ops_open_problems"}
    assert counts == await _page_totals(conn, monkeypatch)
    added = {"review_waiting": 2, "supply_drafts": 3, "supply_no_description": 2, "aa_content_drafts": 1,
             "place_cards_to_review": 2, "compliance_holds": 1, "review_held_by_policy": 1,
             "settings_to_approve": 1, "compliance_exceptions_to_approve": 2, "erasure_to_approve": 1,
             "ops_open_problems": 1}
    assert {k: tiles[k].count - before[k] for k in added} == added
    assert tiles["settings_to_approve"].filter == {"pending": "1", "app": "cis"}
    assert not tiles["place_cards_to_review"].counts_toward_total
    assert not tiles["review_held_by_policy"].counts_toward_total


@pytest.mark.asyncio
async def test_own_proposals_leave_the_approval_tiles_while_two_person_approval_is_on(conn, monkeypatch):
    """Maker-checker: with two-person approval on, the approve routes refuse the proposer, so the tiles leave their
    own proposals out; with it off (a recorded self-approval) they count."""
    me = "admin:00000000-0000-0000-0000-0000000000bb"
    await _proposal(conn, by=me)
    await _exception(conn, "proposed", by=me)
    viewer = admin_my_work.Viewer(role="admin", perms=frozenset(), actor=me)

    async def on(_conn):
        return True

    async def off(_conn):
        return False

    monkeypatch.setattr(settings_registry, "two_person_on", off)
    assert (await admin_my_work._settings_to_approve(conn, viewer))["count"] >= 1
    assert (await admin_my_work._compliance_exceptions(conn, viewer))["count"] >= 1
    monkeypatch.setattr(settings_registry, "two_person_on", on)
    open_others = await conn.fetchval("SELECT count(*) FROM compliance.exception WHERE status = 'proposed' "
                                      "AND proposed_by <> $1", me)
    assert (await admin_my_work._compliance_exceptions(conn, viewer))["count"] == open_others
    settings_tile = await admin_my_work._settings_to_approve(conn, viewer)
    mine = await conn.fetchval("SELECT count(*) FROM shared.setting_value WHERE status = 'proposed' AND "
                               "proposed_by = $1", me)
    assert mine == 1 and settings_tile["by_app"].get("cis", 0) == await conn.fetchval(
        "SELECT count(*) FROM shared.setting_value WHERE status = 'proposed' AND proposed_by <> $1 AND key = ANY($2)",
        me, [k for k in shared_settings.READER.keys() if shared_settings.definition(k).app == "cis"])


@pytest.mark.asyncio
async def test_the_oldest_time_is_the_oldest_waiting_item(conn):
    await _review_row(conn, "pending", created_at=LONG_AGO)
    await _review_row(conn, "approved", created_at=LONG_AGO - timedelta(days=30))   # closed: not waiting
    # the drafts tile dates an item by its oldest open draft: a redraft does not move the entity row
    await _supply(conn, "stale", updated_at=LONG_AGO - timedelta(days=30), draft=True, draft_at=LONG_AGO)
    await _supply(conn, "draft", updated_at=LONG_AGO - timedelta(days=60))   # no draft: not in the drafts tile
    await _card(conn, "auto_checked", created_at=LONG_AGO)
    await _card(conn, "verified", created_at=LONG_AGO - timedelta(days=30))
    tiles = await _summary(conn, "admin")
    for key in ("review_waiting", "supply_drafts", "place_cards_to_review"):
        assert tiles[key].oldest_at == LONG_AGO, key


@pytest.mark.asyncio
async def test_a_trashed_source_leaves_the_review_tile_like_the_page(conn, monkeypatch):
    """AA-986 L1: a pending row whose source is no longer live is hidden from the page, so not counted either."""
    tour = await _review_row(conn, "pending")
    before = (await _summary(conn, "content"))["review_waiting"].count
    await conn.execute("UPDATE silver_aa_internal.raw_tours SET deleted_at = now() WHERE tour_id = $1", tour)
    tiles = await _summary(conn, "content")
    assert tiles["review_waiting"].count == before - 1
    assert tiles["review_waiting"].count == (await _page_totals(conn, monkeypatch))["review_waiting"]


@pytest.mark.asyncio
async def test_a_content_role_gets_only_its_tiles(conn):
    assert list(await _summary(conn, "content")) == [
        "review_waiting", "supply_drafts", "supply_no_description", "aa_content_drafts", "review_held_by_policy"]
    assert list(await _summary(conn, "tenant_qa")) == ["compliance_holds", "place_cards_to_review"]
