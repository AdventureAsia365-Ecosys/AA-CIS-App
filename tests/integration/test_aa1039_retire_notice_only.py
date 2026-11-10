"""AA-1039 follow-up (tech lead decision S276, root ADR 0019 d20 amended) on real Postgres: a tour retirement notifies
by default and never changes the agency's site; the agency moves a page to draft with one click, or opts in to
automatic drafts in Content studio Settings (CIS 397 ``tour_retirement_policy.auto_draft``).

- Default: the WordPress page gets a notice-only flag (no draft date), the notice lists it (immediate mail), the bell
  gets one ``tour.pages_to_review`` row with the page (none again on a replay), the sweep schedules nothing and the
  page stays published.
- Opt-in: the page gets ``draft_after_notice`` at event + N days and the job drafts it once due; an agency that turned
  the opt-in off since keeps its page online (the flag becomes notice only).
- The click (POST /v1/publish-log/{publish_id}/move-to-draft): only the owner tenant's page with an open notice (404
  for another tenant and for a page with no notice), one WordPress call, the row is the retirement's draft, the flag
  resolved ``drafted`` by the member, one audit row, a repeat click is a replay; GET /retired-pages lists it until then.
- After a restore the page the agency drafted is listed in the "back on sale" notice and "Put back online" publishes it.
- Settings (GET / PATCH /v1/account/tour-retirement): the upsert on the expression key, the audit row, and the next
  retirement follows the setting.

One rolled-back transaction per test (the seed helpers of test_aa986_l2_tour_lifecycle and the tenant-pool harness of
test_aa817_tenant_pool_routes). WordPress and Secrets Manager are fakes. Invented names only.
"""
import json
import os
import sys
import uuid
from datetime import timedelta
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

from api.routers import v1_account_tour_retirement as settings_mod  # noqa: E402
from api.routers import v1_publish as pub_mod  # noqa: E402
from services import tour_lifecycle as tl  # noqa: E402
from services.acp_s4_blog.cms.wordpress import PostState  # noqa: E402
from services.jobs import tour_retirement_jobs as trj  # noqa: E402
from services.tour_lifecycle import fanout  # noqa: E402

SITE = "https://pebble-shore.example"
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
    """The agency's WordPress site: one post, its status, and the calls made to it."""

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
    w = SimpleNamespace(t=t, site=_WP(), member=str(uuid.uuid4()))
    w.tenant = await l2._tenant(conn, f"{label}-owner")
    w.other = await l2._tenant(conn, f"{label}-other")
    w.req = str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status) "
        "VALUES ($1, 'none', $2, 'blog', 'approved') RETURNING request_id", w.tenant, uuid.UUID(t.tour)))
    w.piece = str(await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, channel) VALUES ($1, $2, 1, 'A quiet evening on the pebble shore.', 'approved', 'blog') "
        "RETURNING piece_id", w.tenant, uuid.UUID(w.req)))
    w.url = f"{SITE}/pebble-shore-evening/"
    w.page = str(await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at, idempotency_key) VALUES ($1, $2, 'blog', 'published', '23', $3, now(), $4) "
        "RETURNING publish_id", uuid.UUID(w.piece), w.tenant, w.url, f"{w.piece}:blog:aa1039notice"))
    for tid in (w.tenant, w.other):
        await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key, "
                           "connected_at) VALUES ($1, 'wordpress', '{}'::jsonb, $2, now())", tid, f"tenant/{tid}/wp")
    return w


async def _event_key(conn, t, event):
    return await conn.fetchval("SELECT idempotency_key FROM gold_aa_internal.tour_lifecycle_event "
                               "WHERE source_id = $1 AND event = $2 ORDER BY id DESC LIMIT 1", t.group, event)


async def _retire(conn, w):
    await tl.trash_master(conn, w.t.tour)
    key = await _event_key(conn, w.t, "retired")
    return key, await fanout.fan_out(l2._OneConnPool(conn), key)


async def _flag(conn, page):
    return await conn.fetchrow("SELECT flag_id::text AS flag_id, action, draft_due_at, state, resolution, resolved_by, "
                               "tenant_message, detail, created_at FROM acp_shared.content_compliance_flag "
                               "WHERE kind = 'tour_retired' AND publish_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
                               page)


async def _row(conn, page):
    return await conn.fetchrow("SELECT status, unpublished_by FROM acp_shared.publish_log WHERE publish_id = $1::uuid",
                               page)


def _tenant(w, tenant_id=None):
    return {"sub": tenant_id or w.tenant, "role": "tenant", "mid": w.member}


async def _click(conn, w, tenant_id=None, page=None):
    with patch.object(pub_mod, "_get_secret", return_value=dict(CREDS)), \
            patch.object(pub_mod, "_wp_adapter", return_value=w.site):
        return await pub_mod.move_page_to_draft(uuid.UUID(page or w.page), l2._req(conn), tenant=_tenant(w, tenant_id))


async def _opt_in(conn, tenant, days=7):
    await conn.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, auto_draft, updated_by) "
                       "VALUES ($1, $2, true, 'test')", tenant, days)


# ── the default: notice only ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_by_default_the_page_stays_online_and_the_agency_is_told(conn):
    w = await _seed(conn, "rn-default")
    key, out = await _retire(conn, w)
    assert out["flags_created"] == 1 and out["auto_draft_pages"] == 0 and out["tenants_told_pages_to_review"] == 1
    flag = await _flag(conn, w.page)
    assert flag["action"] == "notice_only" and flag["draft_due_at"] is None and flag["state"] == "open"
    assert "stays online" in flag["tenant_message"] and json.loads(flag["detail"])["auto_draft"] is False
    notice = await conn.fetchrow("SELECT delivery, summary FROM acp_shared.tenant_tour_notice "
                                 "WHERE tenant_id = $1::uuid AND event_key = $2", w.tenant, key)
    assert notice["delivery"] == "immediate"
    assert json.loads(notice["summary"])["pages"] == [{"url": w.url, "publish_id": w.page, "draft_on": None,
                                                       "auto_draft": False}]
    bell = await conn.fetch("SELECT payload FROM shared.notifications WHERE tenant_id = $1::uuid "
                            "AND event_type = 'tour.pages_to_review'", w.tenant)
    assert len(bell) == 1
    payload = json.loads(bell[0]["payload"])
    assert payload["pages"] == [{"publish_id": w.page, "url": w.url, "draft_on": None}] and payload["count"] == 1

    again = await fanout.fan_out(l2._OneConnPool(conn), key)                  # replay: no second bell row
    assert again["flags_created"] == 0
    assert await conn.fetchval("SELECT count(*) FROM shared.notifications WHERE tenant_id = $1::uuid "
                               "AND event_type = 'tour.pages_to_review'", w.tenant) == 1
    assert await trj.enqueue_due_drafts(l2._OneConnPool(conn)) == 0
    assert (await _row(conn, w.page))["status"] == "published" and w.site.calls == []

    listed = await pub_mod.list_retired_pages(l2._req(conn), tenant=_tenant(w))
    assert [(d["publish_id"], d["can_move_to_draft"], d["draft_due_at"]) for d in listed["data"]] == \
        [(w.page, True, None)]
    assert (await pub_mod.list_retired_pages(l2._req(conn), tenant=_tenant(w, w.other)))["data"] == []


# ── the opt-in ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_opted_in_agency_gets_its_page_drafted_when_due(conn):
    w = await _seed(conn, "rn-optin")
    await _opt_in(conn, w.tenant, days=2)
    key, out = await _retire(conn, w)
    assert out["auto_draft_pages"] == 1
    flag = await _flag(conn, w.page)
    occurred = await conn.fetchval("SELECT occurred_at FROM gold_aa_internal.tour_lifecycle_event "
                                   "WHERE idempotency_key = $1", key)
    assert flag["action"] == "draft_after_notice" and flag["draft_due_at"] == occurred + timedelta(days=2)
    assert "As set in your Content studio" in flag["tenant_message"]
    pages = json.loads(await conn.fetchval("SELECT summary FROM acp_shared.tenant_tour_notice "
                                           "WHERE tenant_id = $1::uuid AND event_key = $2", w.tenant, key))["pages"]
    assert pages[0]["auto_draft"] is True and pages[0]["draft_on"]

    assert await trj.enqueue_due_drafts(l2._OneConnPool(conn)) == 0                         # not due yet
    await conn.execute("UPDATE acp_shared.content_compliance_flag SET draft_due_at = now() - interval '1 minute' "
                       "WHERE flag_id = $1::uuid", flag["flag_id"])
    assert await trj.enqueue_due_drafts(l2._OneConnPool(conn)) == 1

    async def read_secret(name):
        return dict(CREDS)

    out = await fanout.move_to_draft(l2._OneConnPool(conn), flag["flag_id"], read_secret=read_secret,
                                     adapter_for=lambda creds: w.site)
    assert out["status"] == "drafted" and w.site.status == "draft"
    flag = await _flag(conn, w.page)
    assert flag["resolution"] == "drafted" and flag["resolved_by"] == fanout.ACTOR
    assert json.loads(flag["detail"])["drafted_by"] == "auto_draft"


@pytest.mark.asyncio
async def test_turning_the_opt_in_off_keeps_a_scheduled_page_online(conn):
    w = await _seed(conn, "rn-optout")
    await _opt_in(conn, w.tenant)
    await _retire(conn, w)
    flag = await _flag(conn, w.page)
    await conn.execute("UPDATE acp_shared.content_compliance_flag SET draft_due_at = now() - interval '1 minute', "
                       "created_at = now() - interval '8 days' WHERE flag_id = $1::uuid", flag["flag_id"])
    body = settings_mod.TourRetirementSettingsIn(auto_draft=False)
    assert (await settings_mod.set_tour_retirement_settings(body, l2._req(conn), tenant=_tenant(w)))["auto_draft"] \
        is False

    async def never(*a, **k):
        raise AssertionError("the page must not be touched")

    out = await fanout.move_to_draft(l2._OneConnPool(conn), flag["flag_id"], read_secret=never, adapter_for=never)
    assert out == {"status": "skipped", "reason": "auto_draft_off"}
    flag = await _flag(conn, w.page)
    assert flag["action"] == "notice_only" and flag["draft_due_at"] is None and flag["state"] == "open"
    assert "stays online" in flag["tenant_message"]
    assert await trj.enqueue_due_drafts(l2._OneConnPool(conn)) == 0
    assert (await _row(conn, w.page))["status"] == "published"


# ── the click ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_agency_moves_its_page_to_draft_with_one_click(conn):
    w = await _seed(conn, "rn-click")
    await _retire(conn, w)
    flag = await _flag(conn, w.page)

    with pytest.raises(HTTPException) as exc:                         # another tenant: 404, nothing touched
        await _click(conn, w, tenant_id=w.other)
    assert exc.value.status_code == 404 and w.site.calls == []

    out = await _click(conn, w)
    assert out["outcome"] == "drafted" and out["status"] == "unpublished" and not out["replayed"]
    assert out["external_url"] == w.url and w.site.calls == [("draft", "23")]
    row = await _row(conn, w.page)
    assert row["status"] == "unpublished" and row["unpublished_by"] == fanout.ACTOR
    flag = await _flag(conn, w.page)
    assert flag["state"] == "resolved" and flag["resolution"] == "drafted"
    assert flag["resolved_by"] == f"membership:{w.member}" and json.loads(flag["detail"])["drafted_by"] == "tenant"
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1::uuid "
                               "AND action = 'content_piece.moved_to_draft' AND resource_id = $2 AND actor = $3",
                               w.tenant, w.piece, f"membership:{w.member}") == 1

    again = await _click(conn, w)                                     # double click: replay, no WordPress call
    assert again["replayed"] and again["outcome"] == "already_drafted" and len(w.site.calls) == 1
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1::uuid "
                               "AND action = 'content_piece.moved_to_draft'", w.tenant) == 1
    assert (await pub_mod.list_retired_pages(l2._req(conn), tenant=_tenant(w)))["data"] == []


@pytest.mark.asyncio
async def test_a_page_with_no_retirement_notice_cannot_be_moved(conn):
    w = await _seed(conn, "rn-nonotice")
    with pytest.raises(HTTPException) as exc:
        await _click(conn, w)
    assert exc.value.status_code == 404 and w.site.calls == []
    assert (await _row(conn, w.page))["status"] == "published"


@pytest.mark.asyncio
async def test_a_page_drafted_by_the_agency_is_put_back_after_the_restore(conn):
    w = await _seed(conn, "rn-putback")
    await _retire(conn, w)
    await _click(conn, w)
    await tl.restore_master(conn, w.t.tour)
    await tl.activate_master(conn, w.t.tour)
    key = await _event_key(conn, w.t, "restored")
    result = await fanout.fan_out(l2._OneConnPool(conn), key)
    assert result["pages_drafted"] == 1 and result["tenants_told_put_back"] == 1
    summary = json.loads(await conn.fetchval("SELECT summary FROM acp_shared.tenant_tour_notice "
                                             "WHERE tenant_id = $1::uuid AND event_key = $2", w.tenant, key))
    assert summary["pages_drafted"] == [{"publish_id": w.page, "url": w.url}]

    with patch.object(pub_mod, "_get_secret", return_value=dict(CREDS)), \
            patch("services.acp_publish.dispatch._wordpress", return_value=w.site):
        out = await pub_mod.put_back(uuid.UUID(w.page), _request(conn, True), tenant={"sub": w.tenant})
    assert out["outcome"] == "published" and w.site.status == "publish"
    assert (await _row(conn, w.page))["status"] == "published"


# ── settings ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_upsert_audit_and_the_next_retirement_follows_them(conn):
    w = await _seed(conn, "rn-settings")
    get = await settings_mod.get_tour_retirement_settings(l2._req(conn), tenant=_tenant(w))
    assert get["auto_draft"] is False and get["notice_days"] >= 1

    body = settings_mod.TourRetirementSettingsIn(auto_draft=True, notice_days=4)
    out = await settings_mod.set_tour_retirement_settings(body, l2._req(conn), tenant=_tenant(w))
    assert out["auto_draft"] is True and out["notice_days"] == 4
    out = await settings_mod.set_tour_retirement_settings(       # second write updates the same row
        settings_mod.TourRetirementSettingsIn(auto_draft=True, notice_days=5), l2._req(conn), tenant=_tenant(w))
    assert out["notice_days"] == 5
    rows = await conn.fetch("SELECT notice_days, auto_draft, updated_by FROM acp_shared.tour_retirement_policy "
                            "WHERE tenant_id = $1::uuid", w.tenant)
    assert [(r["notice_days"], r["auto_draft"], r["updated_by"]) for r in rows] == \
        [(5, True, f"membership:{w.member}")]
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE tenant_id = $1::uuid "
                               "AND action = 'tenant.tour_retirement_settings_changed'", w.tenant) == 2
    assert (await settings_mod.get_tour_retirement_settings(l2._req(conn), tenant=_tenant(w, w.other)))[
        "auto_draft"] is False                                   # another agency is not opted in

    key, _ = await _retire(conn, w)
    flag = await _flag(conn, w.page)
    occurred = await conn.fetchval("SELECT occurred_at FROM gold_aa_internal.tour_lifecycle_event "
                                   "WHERE idempotency_key = $1", key)
    assert flag["action"] == "draft_after_notice" and flag["draft_due_at"] == occurred + timedelta(days=5)
