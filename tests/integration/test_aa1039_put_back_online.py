"""AA-1039 follow-up (option A, S275) on real Postgres: after a restore nothing is republished on its own; the tenant
puts each drafted WordPress page back online with one click.

- retire -> move to draft -> restore: the page stays in draft; the "back on sale" notice lists it
  (``summary.pages_drafted``, mailed at once) and the tenant's bell gets one ``tour.pages_to_put_back`` row (a replay
  of the fan-out adds none);
- GET /v1/publish-log/put-back lists it for its tenant only;
- POST /v1/publish-log/{id}/put-back on the tenant pool (aa_app_user under RLS): another tenant gets 404 and nothing
  changes; the owner's click publishes the draft (one WordPress call), the row is live again (a later retirement
  flags it like any other post), the flag carries the note, the audit row is written; a second click replays with
  no WordPress call;
- a page the tenant set to private on WordPress is left as is (row back to the retirement's draft, gone from the
  list, a repeat click replays);
- a request already published again as another post: 409, the page stays in draft.

One rolled-back transaction per test; the "tenant pool" is the test connection with SET LOCAL ROLE aa_app_user (the
test_aa817_tenant_pool_routes harness). WordPress, Secrets Manager and SES are fakes. Invented names only.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)
from test_aa817_tenant_pool_routes import _request  # noqa: E402  (tenant-pool harness)

from api.routers import v1_publish as pub_mod  # noqa: E402
from services import tour_lifecycle as tl  # noqa: E402
from services.acp_publish import put_back as pb  # noqa: E402
from services.acp_s4_blog.cms.wordpress import PostState  # noqa: E402
from services.tour_lifecycle import fanout  # noqa: E402

SITE = "https://amber-cove.example"
CREDS = {"wp_url": SITE, "username": "u", "app_password": "p"}


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        await c.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel text, "
                        "ADD COLUMN IF NOT EXISTS angle_gate_option_id uuid")
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", AsyncMock())
    monkeypatch.setattr("services.tour_lifecycle.effects.recheck_trip_pages", AsyncMock())
    monkeypatch.setattr("services.tour_lifecycle.effects.notify_tripplanner", AsyncMock(return_value=0))
    monkeypatch.delenv("TRIP_PAGE_BASE_URL", raising=False)


class _WP:
    """The tenant's WordPress site: one post, its status, and the calls made to it."""

    def __init__(self, status="publish"):
        self.status = status
        self.calls = []

    async def set_draft(self, post_id):
        self.calls.append(("draft", post_id))
        self.status = "draft"
        return "drafted"

    async def post_status(self, post_id):
        self.calls.append(("read", post_id))
        return PostState(self.status, None)

    async def set_published(self, post_id):
        self.calls.append(("publish", post_id))
        self.status = "publish"
        return "published"


async def _seed(conn, label):
    t = await l2._tour(conn, label)
    w = SimpleNamespace(t=t, site=_WP())
    w.tenant = await l2._tenant(conn, f"{label}-owner")
    w.other = await l2._tenant(conn, f"{label}-other")
    w.req = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status) "
        "VALUES ($1, 'none', $2, 'blog', 'approved') RETURNING request_id", w.tenant, uuid.UUID(t.tour)))
    w.piece = str(await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, channel) VALUES ($1, $2, 1, 'A slow morning on the amber ridge.', 'approved', 'blog') "
        "RETURNING piece_id", w.tenant, uuid.UUID(w.req)))
    w.url = f"{SITE}/amber-ridge-morning/"
    w.page = str(await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at, idempotency_key) VALUES ($1, $2, 'blog', 'published', '17', $3, now(), $4) "
        "RETURNING publish_id", uuid.UUID(w.piece), w.tenant, w.url, f"{w.piece}:blog:aa1039putback"))
    for tid in (w.tenant, w.other):
        await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                           "connected_at) VALUES ($1, 'wordpress', '{}'::jsonb, $2, now())", tid, f"tenant/{tid}/wp")
    return w


async def _event_key(conn, t, event):
    return await conn.fetchval("SELECT idempotency_key FROM gold_aa_internal.tour_lifecycle_event "
                               "WHERE source_id = $1 AND event = $2 ORDER BY id DESC LIMIT 1", t.group, event)


async def _retire_draft_restore(conn, w):
    """The S274 rehearsal path: retire, the notice period ends, the page goes to draft, the tour comes back."""
    pool = l2._OneConnPool(conn)
    await tl.trash_master(conn, w.t.tour)
    await fanout.fan_out(pool, await _event_key(conn, w.t, "retired"))
    flag_id = await conn.fetchval("SELECT flag_id::text FROM acp_shared.content_compliance_flag "
                                  "WHERE kind = 'tour_retired' AND publish_id = $1::uuid", w.page)
    await conn.execute("UPDATE acp_shared.content_compliance_flag SET draft_due_at = now() - interval '1 minute' "
                       "WHERE flag_id = $1::uuid", flag_id)

    async def read_secret(name):
        return dict(CREDS)

    out = await fanout.move_to_draft(pool, flag_id, read_secret=read_secret, adapter_for=lambda creds: w.site)
    assert out["status"] == "drafted" and w.site.status == "draft"
    await tl.restore_master(conn, w.t.tour)
    await tl.activate_master(conn, w.t.tour)
    key = await _event_key(conn, w.t, "restored")
    result = await fanout.fan_out(pool, key)
    return SimpleNamespace(flag_id=flag_id, key=key, result=result)


@asynccontextmanager
async def _wordpress(site):
    with patch.object(pub_mod, "_get_secret", return_value=dict(CREDS)), \
            patch("services.acp_publish.dispatch._wordpress", return_value=site):
        yield


async def _click(conn, tenant, page, flag_on=True):
    return await pub_mod.put_back(uuid.UUID(page), _request(conn, flag_on), tenant={"sub": tenant})


async def _row(conn, page):
    return await conn.fetchrow("SELECT status, unpublished_at, unpublished_by FROM acp_shared.publish_log "
                               "WHERE publish_id = $1::uuid", page)


@pytest.mark.asyncio
async def test_restore_lists_the_drafted_page_and_never_publishes_it(conn):
    w = await _seed(conn, "pb-notice")
    r = await _retire_draft_restore(conn, w)
    assert w.site.status == "draft" and [c[0] for c in w.site.calls] == ["draft"]   # nothing republished
    assert r.result["pages_drafted"] == 1 and r.result["tenants_told_put_back"] == 1
    notice = await conn.fetchrow("SELECT delivery, summary FROM acp_shared.tenant_tour_notice "
                                 "WHERE event_key = $1 AND tenant_id = $2::uuid", r.key, w.tenant)
    assert notice["delivery"] == "immediate"
    assert json.loads(notice["summary"])["pages_drafted"] == [{"publish_id": w.page, "url": w.url}]
    bell = await conn.fetch("SELECT payload, target_roles FROM shared.notifications "
                            "WHERE tenant_id = $1::uuid AND event_type = 'tour.pages_to_put_back'", w.tenant)
    assert len(bell) == 1 and bell[0]["target_roles"] == ["tenant"]
    assert json.loads(bell[0]["payload"])["pages"] == [{"publish_id": w.page, "url": w.url}]
    await fanout.fan_out(l2._OneConnPool(conn), r.key)                       # a replay adds no bell row
    assert await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE tenant_id = $1::uuid "
                               "AND event_type = 'tour.pages_to_put_back'", w.tenant) == 1

    listed = await pub_mod.list_put_back(_request(conn, True), tenant={"sub": w.tenant})
    assert [d["publish_id"] for d in listed["data"]] == [w.page] and listed["data"][0]["external_url"] == w.url
    assert (await pub_mod.list_put_back(_request(conn, True), tenant={"sub": w.other}))["data"] == []


@pytest.mark.asyncio
async def test_the_owner_puts_it_back_once_and_another_tenant_cannot(conn):
    w = await _seed(conn, "pb-click")
    r = await _retire_draft_restore(conn, w)
    async with _wordpress(w.site):
        with pytest.raises(HTTPException) as exc:
            await _click(conn, w.other, w.page)
        assert exc.value.status_code == 404
        assert (await _row(conn, w.page))["status"] == "unpublished" and w.site.status == "draft"

        out = await _click(conn, w.tenant, w.page)
        assert out["outcome"] == "published" and out["status"] == "published" and not out["replayed"]
        assert [c[0] for c in w.site.calls][-2:] == ["read", "publish"] and w.site.status == "publish"
        row = await _row(conn, w.page)
        assert row["status"] == "published" and row["unpublished_at"] is None and row["unpublished_by"] is None
        detail = json.loads(await conn.fetchval("SELECT detail FROM acp_shared.content_compliance_flag "
                                                "WHERE flag_id = $1::uuid", r.flag_id))
        assert detail["put_back"]["outcome"] == "published" and detail["put_back"]["by"] == f"tenant:{w.tenant}"
        assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1::uuid "
                                   "AND action = 'content_piece.put_back_online' AND resource_id = $2",
                                   w.tenant, w.piece) == 1

        calls = len(w.site.calls)
        again = await _click(conn, w.tenant, w.page)                              # double click
        assert again["replayed"] and again["outcome"] == "published" and len(w.site.calls) == calls
    assert (await pub_mod.list_put_back(_request(conn, True), tenant={"sub": w.tenant}))["data"] == []

    # live again like any other post: a new retirement flags it
    await tl.trash_master(conn, w.t.tour)
    out = await fanout.fan_out(l2._OneConnPool(conn), await _event_key(conn, w.t, "retired"))
    assert out["flags_created"] == 1


@pytest.mark.asyncio
async def test_a_page_made_private_on_wordpress_is_left_as_is(conn):
    w = await _seed(conn, "pb-private")
    r = await _retire_draft_restore(conn, w)
    w.site.status = "private"
    async with _wordpress(w.site):
        out = await _click(conn, w.tenant, w.page, flag_on=False)
        assert out["outcome"] == "changed_on_wordpress" and out["wordpress_status"] == "private"
        assert w.site.status == "private" and "publish" not in [c[0] for c in w.site.calls]
        row = await _row(conn, w.page)
        assert row["status"] == "unpublished" and row["unpublished_by"] == pb.RETIREMENT_ACTOR
        assert (await pub_mod.list_put_back(_request(conn, True), tenant={"sub": w.tenant}))["data"] == []
        calls = len(w.site.calls)
        again = await _click(conn, w.tenant, w.page)
        assert again["replayed"] and again["outcome"] == "changed_on_wordpress" and len(w.site.calls) == calls
    assert r.flag_id


@pytest.mark.asyncio
async def test_content_published_again_as_another_post_keeps_the_page_in_draft(conn):
    w = await _seed(conn, "pb-other")
    await _retire_draft_restore(conn, w)
    await conn.execute(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at, idempotency_key) VALUES ($1, $2, 'blog', 'published', '18', $3, now(), $4)",
        uuid.UUID(w.piece), w.tenant, f"{SITE}/amber-ridge-morning-2/", f"{w.piece}:blog:aa1039putback")
    async with _wordpress(w.site):
        with pytest.raises(HTTPException) as exc:
            await _click(conn, w.tenant, w.page)
    assert exc.value.status_code == 409 and exc.value.detail == pub_mod.PUT_BACK_OTHER_LIVE
    assert (await _row(conn, w.page))["status"] == "unpublished" and w.site.status == "draft"
