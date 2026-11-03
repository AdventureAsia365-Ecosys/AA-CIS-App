"""AA-1039 R6 slice 2 on real Postgres: CIS 362, the AA-subject gate (root ADR 0019 d21) and the tour retirement
fan-out (d20).

- 362: the three tables, FORCE RLS with the X1 parity policies, cis_app DML and no tenant-pool grant.
- The gate: a booking link outside the catalogue holds the piece (one open flag, the content-style sentence); a link to
  a trip page of a served tour passes and resolves the flag; a sales goal with no live AA subject is held; brand
  content with no sales link passes without a subject; a staff release lets exactly that text through.
- The fan-out: retiring a tour with a live WordPress page and a live Facebook post flags both (draft after the
  notice period, notice only), mails the post's tenant at once and folds the other derivative tenant into the digest;
  a per-tenant policy row changes the notice period; run_after_commit enqueues one fan-out job per event; the move to
  draft resolves the flag and takes the post off publish_log; a restore resolves the open flags and writes the
  "back on sale" notice; the mail job sends one message per tenant to the verified owner / editor addresses.

Every test runs in one rolled-back transaction (the seed helpers of test_aa986_l2_tour_lifecycle). No network: the
WordPress adapter, SES and the partner pool are fakes. Invented names only.
"""
import json
import os
import sys
import uuid
from datetime import timedelta
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services import tour_lifecycle as tl  # noqa: E402
from services.acp_shared import subject_gate as sg  # noqa: E402
from services.jobs import tour_retirement_jobs as trj  # noqa: E402
from services.tour_lifecycle import fanout  # noqa: E402
from shared.jobs import queue  # noqa: E402

ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TestPool1"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        # CIS 124 (pgvector) does not replay on the CI postgres:15 image: add the two plain columns of it this module
        # reads (inside the rolled-back transaction)
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
    monkeypatch.setenv("COGNITO_PARTNER_ISSUER", ISS)
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.test")
    monkeypatch.delenv("TRIP_PAGE_BASE_URL", raising=False)


# ── 362 ────────────────────────────────────────────────────────────────────────────────────────────────────────────

TABLES = ("acp_shared.content_compliance_flag", "acp_shared.tenant_tour_notice", "acp_shared.tour_retirement_policy")


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TABLES)
async def test_362_tables_are_force_rls_cis_app_only(conn, table):
    row = await conn.fetchrow("SELECT relrowsecurity AS rls, relforcerowsecurity AS force FROM pg_class "
                              "WHERE oid = $1::regclass", table)
    assert row["rls"] and row["force"]
    policies = {r["polname"] for r in await conn.fetch("SELECT polname FROM pg_policy WHERE polrelid = $1::regclass",
                                                         table)}
    name = table.split(".")[1]
    assert policies == {f"{name}_cis_app_all", f"{name}_cis_owner_all", "tenant_isolation"}
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, $2) AS ok", table, priv)
    assert not await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT') AS ok", table)
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '362'") == 1


# ── seeds ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _request(conn, tenant, *, atom="none", trip=None, goal=None, cta=None, channel="blog"):
    return str(await conn.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, trip_id, channel, status, goal, cta) "
        "VALUES ($1, $2, $3, $4, 'approved', $5, $6) RETURNING request_id",
        tenant, atom, uuid.UUID(trip) if trip else None, channel, goal, cta))


async def _piece(conn, tenant, request, text, *, channel="blog"):
    return str(await conn.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, status, "
        "channel) VALUES ($1, $2, 1, $3, 'approved', $4) RETURNING piece_id", tenant, uuid.UUID(request), text,
        channel))


async def _post(conn, tenant, piece, channel, url, external_id="41"):
    return str(await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, $3, 'published', $4, $5, now()) RETURNING publish_id",
        uuid.UUID(piece), tenant, channel, external_id, url))


def _gate_piece(tenant, request, piece, text):
    return {"piece_id": piece, "tenant_id": tenant, "request_id": request, "content_text": text, "channel": "blog"}


async def _open_gate_flags(conn, piece):
    return await conn.fetch("SELECT codes, tenant_message, stage FROM acp_shared.content_compliance_flag "
                            "WHERE piece_id = $1::uuid AND kind = 'subject_gate' AND state = 'open'", piece)


# ── the gate ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_holds_an_off_catalogue_booking_link_and_releases_on_a_fixed_link(conn):
    t = await l2._tour(conn, "gate-mekong")
    tenant = await l2._tenant(conn, "gate")
    req = await _request(conn, tenant, trip=t.tour, goal="conversion")
    bad = "Five quiet days on the river. [Book the river tour](https://othertravel.example/river-tour) today."
    piece = await _piece(conn, tenant, req, bad)
    pool = l2._OneConnPool(conn)

    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, bad), stage="write", actor="test")
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]
    flags = await _open_gate_flags(conn, piece)
    assert len(flags) == 1 and flags[0]["stage"] == "write"
    assert "othertravel.example" in flags[0]["tenant_message"]
    assert "flag" not in flags[0]["tenant_message"].lower() and "qa" not in flags[0]["tenant_message"].lower()

    again = await sg.run(conn, piece=_gate_piece(tenant, req, piece, bad), stage="publish", actor="test")
    assert not again.ok and len(await _open_gate_flags(conn, piece)) == 1     # updated in place, one open row

    good = f"Five quiet days on the river. [Book it here](https://aa-cis.lumiguides.it.com/trip/{t.tour}) today."
    await conn.execute("UPDATE acp_shared.content_piece SET content_text = $2 WHERE piece_id = $1::uuid", piece, good)
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, good), stage="publish", actor="test")
    assert res.ok and await _open_gate_flags(conn, piece) == []
    assert await conn.fetchval("SELECT resolution FROM acp_shared.content_compliance_flag WHERE piece_id = $1::uuid",
                               piece) == "passed"
    assert (await sg.run_for_piece(pool, piece)).ok


@pytest.mark.asyncio
async def test_gate_needs_an_aa_subject_for_a_sales_goal_but_not_for_brand_content(conn):
    tenant = await l2._tenant(conn, "gate-brand")
    sales = await _request(conn, tenant, atom="no-such-atom", goal="promotion")
    text = "Our team has guided travellers for twenty years."
    p1 = await _piece(conn, tenant, sales, text)
    res = await sg.run(conn, piece=_gate_piece(tenant, sales, p1, text), stage="write", actor="test")
    assert not res.ok and res.codes == [sg.NO_AA_SUBJECT]

    brand = await _request(conn, tenant, atom="no-such-atom", goal="trust_building")
    p2 = await _piece(conn, tenant, brand, text + " Read more on https://museum.example/history.")
    res = await sg.run(conn, piece=_gate_piece(tenant, brand, p2, text), stage="write", actor="test")
    assert res.ok and res.detail["brand_content"] is True


@pytest.mark.asyncio
async def test_gate_counts_a_live_atom_and_a_tenant_page_of_a_served_tour(conn):
    t = await l2._tour(conn, "gate-atom")
    tenant = await l2._tenant(conn, "gate-atom")
    await conn.execute("INSERT INTO acp_deliver.tenant_tour_pages (tenant_id, tour_id, url) VALUES ($1, $2, $3)",
                       tenant, uuid.UUID(t.tour), "https://agency.example/tours/lake-walk/")
    req = await _request(conn, tenant, atom=t.atom, goal="conversion")
    text = "A dawn walk by the lake. Book now: https://agency.example/tours/lake-walk"
    piece = await _piece(conn, tenant, req, text)
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, text), stage="write", actor="test")
    assert res.ok, res.detail
    other = "A dawn walk by the lake. Book now: https://agency.example/tours/our-own-walk"
    res = await sg.run(conn, piece=_gate_piece(tenant, req, piece, other), stage="write", actor="test")
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]


@pytest.mark.asyncio
async def test_a_released_text_passes_and_an_edit_is_checked_again(conn):
    from api.routers import admin_tenant_compliance as atc
    tenant = await l2._tenant(conn, "gate-release")
    req = await _request(conn, tenant, atom="no-such-atom", goal="promotion")
    text = "Join our harvest festival trip, book at https://othertravel.example/festival-trip"
    piece = await _piece(conn, tenant, req, text)
    await sg.run(conn, piece=_gate_piece(tenant, req, piece, text), stage="write", actor="test")
    flag_id = await conn.fetchval("SELECT flag_id::text FROM acp_shared.content_compliance_flag "
                                  "WHERE piece_id = $1::uuid AND state = 'open'", piece)
    out = await atc.release_flag(uuid.UUID(flag_id), atc.ReleaseBody(reason="Contract allows partner festival"),
                                 l2._req(conn), x_admin_user_id="7")
    assert out["resolution"] == "released"
    assert (await sg.run(conn, piece=_gate_piece(tenant, req, piece, text), stage="publish", actor="t")).ok
    assert not (await sg.run(conn, piece=_gate_piece(tenant, req, piece, text + "!"), stage="publish", actor="t")).ok

    listing = await atc.list_flags(l2._req(conn), state="all", kind=None, tenant_id=uuid.UUID(tenant), limit=50,
                                   offset=0)
    assert {r["resolution"] for r in listing["data"]} == {"released", None}
    assert listing["open_counts"]["subject_gate"] >= 1


# ── the fan-out ────────────────────────────────────────────────────────────────────────────────────────────────────

async def _world(conn, label):
    t = await l2._tour(conn, label)
    w = SimpleNamespace(t=t)
    w.posting = await l2._tenant(conn, f"{label}-posts")
    req = await _request(conn, w.posting, trip=t.tour)
    w.wp_piece = await _piece(conn, w.posting, req, "A page about the lake.")
    w.wp = await _post(conn, w.posting, w.wp_piece, "blog", "https://agency.example/lake-walk", external_id="41")
    req_fb = await _request(conn, w.posting, trip=t.tour, channel="facebook")
    fb_piece = await _piece(conn, w.posting, req_fb, "Lake walk post", channel="facebook")
    w.fb = await _post(conn, w.posting, fb_piece, "facebook", "https://social.example/posts/9", external_id="9_9")
    w.quiet = await l2._tenant(conn, f"{label}-quiet")
    await _request(conn, w.quiet, atom=t.atom)
    return w


async def _event_key(conn, t, event):
    return await conn.fetchval("SELECT idempotency_key FROM gold_aa_internal.tour_lifecycle_event "
                               "WHERE source_id = $1 AND event = $2 ORDER BY id DESC LIMIT 1", t.group, event)


@pytest.mark.asyncio
async def test_retire_flags_live_posts_and_writes_notices(conn):
    w = await _world(conn, "fan-retire")
    await conn.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by) "
                       "VALUES ($1, 3, 'test')", w.posting)
    out = await tl.trash_master(conn, w.t.tour)
    await tl.run_after_commit(l2._OneConnPool(conn), out)
    key = await _event_key(conn, w.t, "retired")
    assert await conn.fetchval("SELECT count(*) FROM shared.job WHERE idempotency_key = $1",
                               f"{trj.FANOUT_KIND}:{key}") == 1

    result = await fanout.fan_out(l2._OneConnPool(conn), key)
    assert result["status"] == "done" and result["flags_created"] == 2 and result["live_posts"] == 2
    flags = {r["channel"]: r for r in await conn.fetch(
        "SELECT channel, action, draft_due_at, tenant_message, state FROM acp_shared.content_compliance_flag "
        "WHERE kind = 'tour_retired' AND event_key = $1", key)}
    occurred = await conn.fetchval("SELECT occurred_at FROM gold_aa_internal.tour_lifecycle_event "
                                   "WHERE idempotency_key = $1", key)
    assert flags["blog"]["action"] == "draft_after_notice" and flags["blog"]["draft_due_at"] == occurred + timedelta(3)
    assert flags["facebook"]["action"] == "notice_only" and flags["facebook"]["draft_due_at"] is None
    notices = {r["tenant_id"]: r for r in await conn.fetch(
        "SELECT tenant_id::text, delivery, event, summary FROM acp_shared.tenant_tour_notice WHERE event_key = $1",
        key)}
    assert notices[w.posting]["delivery"] == "immediate" and notices[w.quiet]["delivery"] == "digest"
    assert len(json.loads(notices[w.posting]["summary"])["pages"]) == 1
    assert len(result["immediate_notices"]) == 1

    again = await fanout.fan_out(l2._OneConnPool(conn), key)                  # idempotent replay
    assert again["flags_created"] == 0 and again["immediate_notices"] == []
    assert await trj.sweep(l2._OneConnPool(conn)) == 0


@pytest.mark.asyncio
async def test_move_to_draft_after_the_notice_period(conn):
    w = await _world(conn, "fan-draft")
    await tl.trash_master(conn, w.t.tour)
    key = await _event_key(conn, w.t, "retired")
    await fanout.fan_out(l2._OneConnPool(conn), key)
    flag_id = await conn.fetchval("SELECT flag_id::text FROM acp_shared.content_compliance_flag "
                                  "WHERE event_key = $1 AND channel = 'blog'", key)
    await conn.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key) "
                       "VALUES ($1, 'wordpress', '{}'::jsonb, 'tenant/wp-test')", w.posting)
    assert await fanout.enqueue_due_drafts(l2._OneConnPool(conn), 4) == 0      # not due yet
    await conn.execute("UPDATE acp_shared.content_compliance_flag SET draft_due_at = now() - interval '1 minute' "
                       "WHERE flag_id = $1::uuid", flag_id)
    assert await fanout.enqueue_due_drafts(l2._OneConnPool(conn), 4) == 1
    assert await fanout.enqueue_due_drafts(l2._OneConnPool(conn), 4) == 0      # one job per flag

    calls = []

    class _WP:
        async def set_draft(self, post_id):
            calls.append(post_id)
            return "drafted"

    async def read_secret(name):
        return {"wp_url": "https://agency.example", "username": "u", "app_password": "p"}

    out = await fanout.move_to_draft(l2._OneConnPool(conn), flag_id, read_secret=read_secret,
                                     adapter_for=lambda creds: _WP())
    assert out["status"] == "drafted" and calls == ["41"]
    row = await conn.fetchrow("SELECT state, resolution, drafted_at FROM acp_shared.content_compliance_flag "
                              "WHERE flag_id = $1::uuid", flag_id)
    assert row["state"] == "resolved" and row["resolution"] == "drafted" and row["drafted_at"] is not None
    assert await conn.fetchval("SELECT status FROM acp_shared.publish_log WHERE publish_id = $1::uuid",
                               w.wp) == "unpublished"
    again = await fanout.move_to_draft(l2._OneConnPool(conn), flag_id, read_secret=read_secret,
                                       adapter_for=lambda creds: _WP())
    assert again["status"] == "skipped" and calls == ["41"]


@pytest.mark.asyncio
async def test_restore_resolves_the_flags_and_writes_back_on_sale_notices(conn):
    w = await _world(conn, "fan-restore")
    await tl.trash_master(conn, w.t.tour)
    await fanout.fan_out(l2._OneConnPool(conn), await _event_key(conn, w.t, "retired"))
    await tl.restore_master(conn, w.t.tour)
    await tl.activate_master(conn, w.t.tour)
    key = await _event_key(conn, w.t, "restored")
    assert key
    result = await fanout.fan_out(l2._OneConnPool(conn), key)
    assert result["status"] == "done" and result["flags_resolved"] == 2
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.content_compliance_flag WHERE kind = 'tour_retired' "
                               "AND tour_id = $1::uuid AND state = 'open'", w.t.tour) == 0
    deliveries = {r["tenant_id"]: r["delivery"] for r in await conn.fetch(
        "SELECT tenant_id::text, delivery FROM acp_shared.tenant_tour_notice WHERE event_key = $1", key)}
    assert deliveries == {w.posting: "immediate", w.quiet: "digest"}


class _Ses:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": f"m-{len(self.sent)}"}


class _Cognito:
    def __init__(self, emails):
        self.emails = emails

    def list_users(self, **kw):
        sub = kw["Filter"].split('"')[1]
        email = self.emails.get(sub)
        if not email:
            return {"Users": []}
        return {"Users": [{"Attributes": [{"Name": "email", "Value": email},
                                          {"Name": "email_verified", "Value": "true"}]}]}


@pytest.mark.asyncio
async def test_mail_goes_to_owner_and_editor_once(conn, monkeypatch):
    monkeypatch.setattr("api.core.partner_identity.partner_issuer", lambda: ISS)
    w = await _world(conn, "fan-mail")
    for sub, role in (("sub-owner", "owner"), ("sub-editor", "editor"), ("sub-viewer", "viewer")):
        await conn.execute(
            "INSERT INTO shared.tenant_members (tenant_id, cognito_iss, cognito_sub, content_studio_role, status, "
            "created_by) VALUES ($1, $2, $3, $4, 'active', 'seed:AA-1039')", w.posting, ISS, sub, role)
    await tl.trash_master(conn, w.t.tour)
    result = await fanout.fan_out(l2._OneConnPool(conn), await _event_key(conn, w.t, "retired"))
    notice_id = result["immediate_notices"][0]
    ses = _Ses()
    cognito = _Cognito({"sub-owner": "owner@agency.example", "sub-editor": "editor@agency.example",
                        "sub-viewer": "viewer@agency.example"})
    job = queue.Job(id=str(uuid.uuid4()), kind=trj.MAIL_KIND, payload={"notice_id": notice_id}, status="running", attempt=1,
                    max_attempts=4, cost_usd=0.0, progress={})
    from shared.jobs.registry import JobContext
    out = await trj.run_mail(JobContext(l2._OneConnPool(conn), job, {"ses": ses, "cognito": cognito}))
    assert out["sent"] == 1 and len(ses.sent) == 1
    msg = ses.sent[0]
    assert msg["Destination"]["ToAddresses"] == ["owner@agency.example", "editor@agency.example"]
    body = msg["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "moved to draft" in body and "flag" not in body.lower() and " qa" not in body.lower()
    assert await conn.fetchval("SELECT mailed_at IS NOT NULL AS ok FROM acp_shared.tenant_tour_notice "
                               "WHERE notice_id = $1::uuid", notice_id)
    out = await trj.run_mail(JobContext(l2._OneConnPool(conn), job, {"ses": ses, "cognito": cognito}))
    assert out["sent"] == 0 and len(ses.sent) == 1                             # mailed once

    digest = queue.Job(id=str(uuid.uuid4()), kind=trj.MAIL_KIND, payload={"digest_day": "2026-11-03"}, status="running", attempt=1,
                       max_attempts=4, cost_usd=0.0, progress={})
    out = await trj.run_mail(JobContext(l2._OneConnPool(conn), digest, {"ses": ses, "cognito": cognito}))
    # the quiet tenant has no member with an address: its digest notice waits (deferred), nothing is sent for it
    assert {r["status"] for r in out["results"]} <= {"deferred", "sent"}
