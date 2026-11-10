"""AA-1039 follow-up (tech lead decision S276, root ADR 0019 d20 amended): a tour retirement only notifies by default;
the agency moves a page to draft with one click, or opts in to automatic drafts.

- fanout.policy: the agency's own opt-in only (the platform row never opts in), notice only before CIS 395 / 360.
- The job path of move_to_draft: a flag of an agency that turned automatic drafts off since becomes notice only.
- POST /v1/publish-log/{publish_id}/move-to-draft: 404 for a page with no open notice (another tenant's too), 409 for a
  Facebook post, a replay answers "already a draft" with no WordPress call, the refusals of the shared draft path map
  to 409 / 422 / 502, a draft writes one audit row with the clicking member as actor.
- GET /v1/publish-log/retired-pages: the row shape (draft date only for an automatic draft).
- GET / PATCH /v1/account/tour-retirement: defaults, validation (1-30 days, no extra field), the upsert, the audit row,
  503 before CIS 395, owner-only writes (tenant role middleware).
- Wording: the e-mail and the bell say what the agency can do and never that Adventure Asia changes its site unless
  automatic drafts are on; the activity labels.

No network, no DB: fakes only. Invented names.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.middleware import tenant_role
from api.routers import v1_account_tour_retirement as settings_mod
from api.routers import v1_notifications as notif_mod
from api.routers import v1_publish as pub_mod
from api.services import tenant_account
from services.acp_publish import retired_pages as rp
from services.tour_lifecycle import fanout, notice_mail

TENANT_ID = str(uuid.uuid4())
MEMBER = str(uuid.uuid4())
TENANT = {"sub": TENANT_ID, "role": "tenant", "mid": MEMBER}
# Fixed, not uuid4(): it is part of the test_studio_roles parameter ids, and pytest-xdist workers must collect the
# same ids (a random one per import made every worker collect a different test list).
PUBLISH_ID = uuid.UUID("5b0c1a2e-7d43-4f6a-9e21-3c8b5d6f7a10")
PIECE_ID = str(uuid.uuid4())
FLAG_ID = str(uuid.uuid4())
URL = "https://lantern-bay.example/misty-ridge-walk/"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    """Answers by the first matching SQL fragment; records every call."""

    def __init__(self, answers=None, errors=None):
        self.answers = answers or {}
        self.errors = errors or {}
        self.calls = []

    def transaction(self):
        return _Tx()

    def _answer(self, sql, args):
        self.calls.append((sql, args))
        for key, err in self.errors.items():
            if key in sql:
                raise err
        for key, value in self.answers.items():
            if key in sql:
                return value(*args) if callable(value) else value
        return None

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    async def fetch(self, sql, *args):
        return self._answer(sql, args) or []

    async def fetchval(self, sql, *args):
        return self._answer(sql, args)

    async def execute(self, sql, *args):
        self._answer(sql, args)
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))


# ── fanout.policy ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_defaults_to_notice_only_without_a_row():
    assert await fanout.policy(_Conn(), TENANT_ID) == (fanout.DEFAULT_NOTICE_DAYS, False)


@pytest.mark.asyncio
async def test_policy_reads_the_agency_opt_in():
    conn = _Conn({"auto_draft": {"notice_days": 3, "auto_draft": True}})
    assert await fanout.policy(conn, TENANT_ID) == (3, True)
    sql = conn.calls[0][0]
    assert "tenant_id IS NOT NULL AND auto_draft" in sql, "the platform row can never opt an agency in"


@pytest.mark.asyncio
async def test_policy_before_395_reads_the_notice_days_and_stays_notice_only():
    conn = _Conn({"SELECT notice_days FROM": 5}, errors={"auto_draft": asyncpg.UndefinedColumnError("auto_draft")})
    assert await fanout.policy(conn, TENANT_ID) == (5, False)


@pytest.mark.asyncio
async def test_policy_before_360_is_the_default():
    conn = _Conn(errors={"tour_retirement_policy": asyncpg.UndefinedTableError("x")})
    assert await fanout.policy(conn, TENANT_ID) == (fanout.DEFAULT_NOTICE_DAYS, False)
    assert await fanout.notice_days(conn, TENANT_ID) == fanout.DEFAULT_NOTICE_DAYS


def test_flag_sentences_say_what_the_agency_can_do():
    kept = fanout.page_notice_message("Misty Ridge Walk")
    assert "stays online" in kept and "move it to draft with one click" in kept and "keep it" in kept
    assert "will be moved" not in kept
    dated = fanout.page_message("Misty Ridge Walk", datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert "As set in your Content studio" in dated and "17 November 2026" in dated


# ── move_to_draft: the job re-reads the opt-in ─────────────────────────────────────────────────────────────────────

def _flag(**over):
    row = {"flag_id": FLAG_ID, "tenant_id": TENANT_ID, "state": "open", "action": "draft_after_notice",
           "draft_due_at": datetime.now(timezone.utc) - timedelta(minutes=1),
           "created_at": datetime.now(timezone.utc) - timedelta(days=8), "tour_name": "Misty Ridge Walk",
           "tour_id": str(uuid.uuid4()), "publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID,
           "publish_status": "published", "unpublished_at": None, "external_id": "41", "external_url": URL,
           "channel": "blog"}
    row.update(over)
    return row


async def _never(*a, **k):
    raise AssertionError("WordPress must not be called")


@pytest.mark.asyncio
async def test_the_job_leaves_the_page_online_when_the_agency_turned_automatic_drafts_off():
    conn = _Conn({"FROM acp_shared.content_compliance_flag f": _flag(), "opted_out": True})
    out = await fanout.move_to_draft(_Pool(conn), FLAG_ID, read_secret=_never, adapter_for=_never)
    assert out == {"status": "skipped", "reason": "auto_draft_off"}
    update = [c for c in conn.calls if "action = 'notice_only'" in c[0]]
    assert len(update) == 1 and "stays online" in update[0][1][1]


@pytest.mark.asyncio
async def test_the_job_skips_a_notice_only_flag():
    conn = _Conn({"FROM acp_shared.content_compliance_flag f": _flag(action="notice_only", draft_due_at=None)})
    out = await fanout.move_to_draft(_Pool(conn), FLAG_ID, read_secret=_never, adapter_for=_never)
    assert out == {"status": "skipped", "reason": "flag_notice_only"}


@pytest.mark.asyncio
async def test_the_tenant_path_needs_its_own_flag():
    conn = _Conn({"FROM acp_shared.content_compliance_flag f": _flag(tenant_id=str(uuid.uuid4()))})
    out = await fanout.move_to_draft(_Pool(conn), FLAG_ID, read_secret=_never, adapter_for=_never,
                                     actor="tenant:x", tenant_id=TENANT_ID)
    assert out == {"status": "skipped", "reason": "flag_not_found"}


@pytest.mark.asyncio
async def test_the_tenant_path_drafts_a_notice_only_page_at_once_and_records_who():
    flag = _flag(action="notice_only", draft_due_at=None)
    resolved = []
    conn = _Conn({"FROM acp_shared.content_compliance_flag f": flag, "AS alive": False, "liveness": "retired",
                  "tenant_integrations": {"secret_key": "tenant/wp"},
                  "SET state = 'resolved'": lambda *a: resolved.append(a) or FLAG_ID})

    class _Adapter:
        async def set_draft(self, post_id):
            assert post_id == "41"
            return "drafted"

    async def secret(name):
        return {"wp_url": "https://lantern-bay.example", "username": "u", "app_password": "p"}

    out = await fanout.move_to_draft(_Pool(conn), FLAG_ID, read_secret=secret, adapter_for=lambda c: _Adapter(),
                                     actor=f"membership:{MEMBER}", tenant_id=TENANT_ID)
    assert out["status"] == "drafted"
    flag_id, resolution, by, note = resolved[0]
    assert resolution == "drafted" and by == f"membership:{MEMBER}" and json.loads(note)["drafted_by"] == "tenant"
    marks = [c for c in conn.calls if "SET status = 'unpublished'" in c[0]]
    assert marks and marks[0][1][2] == fanout.ACTOR, "the row stays the retirement's draft so Put back online works"


@pytest.mark.asyncio
async def test_a_second_run_that_finds_the_flag_resolved_records_nothing():
    conn = _Conn({"FROM acp_shared.content_compliance_flag f": _flag(), "AS alive": False, "liveness": "retired",
                  "opted_out": False, "tenant_integrations": {"secret_key": "tenant/wp"},
                  "SET state = 'resolved'": None})

    class _Adapter:
        async def set_draft(self, post_id):
            return "drafted"

    async def secret(name):
        return {"wp_url": "https://lantern-bay.example", "username": "u", "app_password": "p"}

    out = await fanout.move_to_draft(_Pool(conn), FLAG_ID, read_secret=secret, adapter_for=lambda c: _Adapter())
    assert out["status"] == "already_drafted"
    assert not [c for c in conn.calls if "SET status = 'unpublished'" in c[0]]


# ── POST /v1/publish-log/{publish_id}/move-to-draft ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_move_to_draft_404_without_an_open_notice_for_this_tenant():
    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert exc.value.status_code == 404 and exc.value.detail == pub_mod.MOVE_NOT_OFFERED
    for sql, args in conn.calls:
        assert "f.tenant_id = $1::uuid" in sql and args[0] == TENANT_ID


@pytest.mark.asyncio
async def test_move_to_draft_replay_answers_from_the_record(monkeypatch):
    conn = _Conn({"f.resolution = 'drafted'": {"flag_id": FLAG_ID, "external_url": URL}})
    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", _never)
    out = await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert out["outcome"] == "already_drafted" and out["replayed"] is True and out["external_url"] == URL


@pytest.mark.asyncio
async def test_move_to_draft_refuses_a_facebook_post(monkeypatch):
    conn = _Conn({"f.state = 'open'": {"flag_id": FLAG_ID, "channel": "facebook"}})
    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", _never)
    with pytest.raises(HTTPException) as exc:
        await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert exc.value.status_code == 409 and exc.value.detail == pub_mod.MOVE_NOT_WORDPRESS


@pytest.mark.parametrize("raised,status", [
    (fanout.DraftRefused("wordpress_not_connected"), 422),
    (fanout.DraftRefused("other_site"), 422),
    (fanout.DraftRefused("timeout", permanent=False), 502),
])
@pytest.mark.asyncio
async def test_move_to_draft_refusals(monkeypatch, raised, status):
    conn = _Conn({"f.state = 'open'": {"flag_id": FLAG_ID, "channel": "blog"}})

    async def refuse(*a, **k):
        raise raised

    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", refuse)
    with pytest.raises(HTTPException) as exc:
        await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert exc.value.status_code == status


@pytest.mark.parametrize("out,detail", [
    ({"status": "resolved", "resolution": "restored"}, pub_mod.MOVE_TOUR_BACK),
    ({"status": "resolved", "resolution": "unpublished"}, pub_mod.MOVE_NOT_LIVE),
    ({"status": "skipped", "reason": "flag_resolved"}, pub_mod.MOVE_NOT_OFFERED),
])
@pytest.mark.asyncio
async def test_move_to_draft_rechecks_map_to_409(monkeypatch, out, detail):
    conn = _Conn({"f.state = 'open'": {"flag_id": FLAG_ID, "channel": "blog"}})

    async def run(*a, **k):
        return out

    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", run)
    with pytest.raises(HTTPException) as exc:
        await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert exc.value.status_code == 409 and exc.value.detail == detail


@pytest.mark.asyncio
async def test_move_to_draft_drafts_and_audits_with_the_member_as_actor(monkeypatch):
    conn = _Conn({"f.state = 'open'": {"flag_id": FLAG_ID, "channel": "blog"}, "SELECT external_url": URL})
    seen, audits = {}, []

    async def run(pool, flag_id, **kw):
        seen.update(kw, flag_id=flag_id)
        return {"status": "drafted", "outcome": "drafted", "publish_id": str(PUBLISH_ID), "tenant_id": TENANT_ID,
                "piece_id": PIECE_ID}

    async def audit(pool, **kw):
        audits.append(kw)

    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", run)
    monkeypatch.setattr(pub_mod, "write_audit_log", audit)
    out = await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert out["outcome"] == "drafted" and out["status"] == "unpublished" and out["replayed"] is False
    assert seen["flag_id"] == FLAG_ID and seen["tenant_id"] == TENANT_ID and seen["actor"] == f"membership:{MEMBER}"
    assert len(audits) == 1 and audits[0]["action"] == "content_piece.moved_to_draft"
    assert audits[0]["actor"] == f"membership:{MEMBER}" and audits[0]["resource_id"] == PIECE_ID


@pytest.mark.asyncio
async def test_move_to_draft_secret_reader_refuses_a_tombstoned_connection(monkeypatch):
    conn = _Conn({"f.state = 'open'": {"flag_id": FLAG_ID, "channel": "blog"}})
    monkeypatch.setattr(pub_mod, "_get_secret", lambda name: {"tombstoned": True})
    monkeypatch.setattr(pub_mod, "is_tombstoned_secret", lambda creds: True)

    async def run(pool, flag_id, *, read_secret, **kw):
        return await read_secret("tenant/wp")

    monkeypatch.setattr(pub_mod._fanout, "move_to_draft", run)
    with pytest.raises(HTTPException) as exc:
        await pub_mod.move_page_to_draft(PUBLISH_ID, _request(conn), tenant=TENANT)
    assert exc.value.status_code == 422


# ── GET /v1/publish-log/retired-pages ──────────────────────────────────────────────────────────────────────────────

def _page(channel="blog", action="notice_only", due=None):
    return {"publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": channel, "external_url": URL,
            "flag_id": FLAG_ID, "action": action, "draft_due_at": due,
            "flagged_at": datetime(2026, 11, 10, tzinfo=timezone.utc), "tour_name": "Misty Ridge Walk",
            "angle_name": "Sunrise on the ridge"}


@pytest.mark.asyncio
async def test_retired_pages_list_shape():
    due = datetime(2026, 11, 17, tzinfo=timezone.utc)
    conn = _Conn({"f.state = 'open'": [
        _page(), _page(channel="facebook"), _page(action="draft_after_notice", due=due)]})
    out = await pub_mod.list_retired_pages(_request(conn), tenant=TENANT)
    assert out["total"] == 3
    wp, fb, auto = out["data"]
    assert wp["can_move_to_draft"] is True and wp["draft_due_at"] is None and wp["title"] == "Sunrise on the ridge"
    assert fb["can_move_to_draft"] is False
    assert auto["draft_due_at"] == due.isoformat()
    assert conn.calls[0][1] == (TENANT_ID,)


@pytest.mark.asyncio
async def test_retired_pages_before_360_is_empty():
    conn = _Conn(errors={"content_compliance_flag": asyncpg.UndefinedTableError("x")})
    assert await pub_mod.list_retired_pages(_request(conn), tenant=TENANT) == {"data": [], "total": 0}


def test_actor_is_the_membership_else_the_tenant():
    assert rp.actor_of(TENANT, TENANT_ID) == f"membership:{MEMBER}"
    assert rp.actor_of({"sub": TENANT_ID}, TENANT_ID) == f"tenant:{TENANT_ID}"
    assert rp.actor_of({"mid": "not-a-uuid"}, TENANT_ID) == f"tenant:{TENANT_ID}"


# ── /v1/account/tour-retirement ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_default_is_off_with_the_platform_notice_period():
    conn = _Conn({"ORDER BY (tenant_id IS NULL)": [{"own": False, "notice_days": 10, "auto_draft": False}]})
    out = await settings_mod.get_tour_retirement_settings(_request(conn), tenant=TENANT)
    assert out == {"auto_draft": False, "notice_days": 10, "min_days": 1, "max_days": 30, "default_notice_days": 7}


@pytest.mark.asyncio
async def test_settings_default_without_any_row():
    out = await settings_mod.get_tour_retirement_settings(_request(_Conn()), tenant=TENANT)
    assert out["auto_draft"] is False and out["notice_days"] == 7


@pytest.mark.asyncio
async def test_settings_read_the_agency_row():
    conn = _Conn({"ORDER BY (tenant_id IS NULL)": [{"own": True, "notice_days": 3, "auto_draft": True},
                                                    {"own": False, "notice_days": 10, "auto_draft": False}]})
    out = await settings_mod.get_tour_retirement_settings(_request(conn), tenant=TENANT)
    assert out["auto_draft"] is True and out["notice_days"] == 3


@pytest.mark.parametrize("body", [{"auto_draft": True, "notice_days": 0}, {"auto_draft": True, "notice_days": 31},
                                  {"auto_draft": True, "notice_days": 7, "scope": "platform"}, {"notice_days": 7},
                                  {"auto_draft": "maybe"}])
def test_settings_body_validation(body):
    with pytest.raises(ValidationError):
        settings_mod.TourRetirementSettingsIn(**body)


@pytest.mark.asyncio
async def test_settings_patch_upserts_and_audits(monkeypatch):
    conn = _Conn({"ORDER BY (tenant_id IS NULL)": [],
                  "INSERT INTO acp_shared.tour_retirement_policy": {"notice_days": 5, "auto_draft": True}})
    audits = []

    async def audit(pool, **kw):
        audits.append(kw)

    monkeypatch.setattr(settings_mod, "write_audit_log", audit)
    body = settings_mod.TourRetirementSettingsIn(auto_draft=True, notice_days=5)
    out = await settings_mod.set_tour_retirement_settings(body, _request(conn), tenant=TENANT)
    assert out["auto_draft"] is True and out["notice_days"] == 5
    upsert = [c for c in conn.calls if "INSERT INTO" in c[0]][0]
    assert upsert[1] == (TENANT_ID, 5, True, f"membership:{MEMBER}")
    assert len(audits) == 1 and audits[0]["action"] == "tenant.tour_retirement_settings_changed"
    assert audits[0]["details"] == {"before": {"auto_draft": False, "notice_days": 7},
                                    "after": {"auto_draft": True, "notice_days": 5}}


@pytest.mark.asyncio
async def test_settings_patch_keeps_the_days_when_only_turning_off(monkeypatch):
    conn = _Conn({"ORDER BY (tenant_id IS NULL)": [{"own": True, "notice_days": 4, "auto_draft": True}],
                  "INSERT INTO acp_shared.tour_retirement_policy": lambda *a: {"notice_days": a[1],
                                                                               "auto_draft": a[2]}})
    monkeypatch.setattr(settings_mod, "write_audit_log", lambda *a, **k: _noop())
    out = await settings_mod.set_tour_retirement_settings(
        settings_mod.TourRetirementSettingsIn(auto_draft=False), _request(conn), tenant=TENANT)
    assert out["auto_draft"] is False and out["notice_days"] == 4


async def _noop():
    return None


@pytest.mark.asyncio
async def test_settings_answer_503_before_395():
    conn = _Conn(errors={"auto_draft": asyncpg.UndefinedColumnError("auto_draft")})
    with pytest.raises(HTTPException) as exc:
        await settings_mod.get_tour_retirement_settings(_request(conn), tenant=TENANT)
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        await settings_mod.set_tour_retirement_settings(
            settings_mod.TourRetirementSettingsIn(auto_draft=True), _request(conn), tenant=TENANT)
    assert exc.value.status_code == 503


@pytest.mark.parametrize("role,method,path,refused", [
    ("editor", "PATCH", "/v1/account/tour-retirement", True),
    ("viewer", "PATCH", "/v1/account/tour-retirement", True),
    ("owner", "PATCH", "/v1/account/tour-retirement", False),
    ("viewer", "GET", "/v1/account/tour-retirement", False),
    ("editor", "POST", f"/v1/publish-log/{PUBLISH_ID}/move-to-draft", False),
    ("viewer", "POST", f"/v1/publish-log/{PUBLISH_ID}/move-to-draft", True),
])
def test_studio_roles(role, method, path, refused):
    assert tenant_role.refusal(method, path, role) is refused


# ── wording ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _notice(pages, posts=()):
    return {"event": "retired", "tour_name": "Misty Ridge Walk",
            "summary": {"stop_date": "10 November 2026", "pages": pages, "posts": list(posts)}}


def test_the_mail_lists_kept_pages_and_never_says_we_change_the_site():
    msg = notice_mail.render([_notice([{"url": URL, "publish_id": "p1", "draft_on": None, "auto_draft": False}])],
                             to=("owner@lantern-bay.example",), agency="Lantern Bay Travel")
    assert "These pages of yours stay online" in msg.text and URL in msg.text
    assert "move it to draft with one click" in msg.text and "Adventure Asia does not change your website" in msg.text
    assert "will be moved to draft" not in msg.text
    assert "AA-" not in msg.text and "AA-" not in msg.html


def test_the_mail_gives_the_date_only_to_an_opted_in_agency():
    msg = notice_mail.render([_notice([{"url": URL, "publish_id": "p1", "draft_on": "17 November 2026",
                                        "auto_draft": True}])], to=("o@lantern-bay.example",), agency="Lantern Bay")
    assert "will be moved to draft on 17 November 2026, as set in your Content studio" in msg.text
    assert "These pages of yours stay online" not in msg.text


def test_the_bell_row_lists_the_pages_with_their_action():
    row = {"id": 8, "event_type": "tour.pages_to_review", "entity_type": "tour", "entity_id": "t1",
           "payload": {"tour_name": "Misty Ridge Walk", "pages": [{"publish_id": "p1", "url": URL, "draft_on": None},
                                                                  {"url": "no-id"}]},
           "is_read": False, "created_at": None}
    item = notif_mod._item(row)
    assert item["title"] == "Pages about a tour no longer offered"
    assert item["pages"] == [{"publish_id": "p1", "url": URL, "draft_on": None}]
    assert "move it to draft with one click" in item["message"] and "keep it" in item["message"]
    assert "As set" not in item["message"]
    row["payload"]["pages"][0]["draft_on"] = "17 November 2026"
    assert "as set in your Content studio Settings" in notif_mod._item(row)["message"]


def test_the_discontinued_bell_says_live_posts_stay_online():
    msg = notif_mod._message("tour.discontinued", {"tour_name": "Misty Ridge Walk", "live_posts": 2})
    assert "stay online" in msg and "move to draft or keep them" in msg


def test_activity_labels():
    assert tenant_account.TENANT_VISIBLE_ACTIONS["content_piece.moved_to_draft"] == ("content", "Moved a page to draft")
    assert tenant_account.detail_text("content_piece.moved_to_draft", {"channel": "blog"}) == \
        "WordPress · tour no longer offered"
    label = tenant_account.TENANT_VISIBLE_ACTIONS["tenant.tour_retirement_settings_changed"]
    assert label[0] == "account"
    assert tenant_account.detail_text("tenant.tour_retirement_settings_changed",
                                      {"after": {"auto_draft": True, "notice_days": 5}}) == \
        "Automatic draft on, after 5 days"
    assert tenant_account.detail_text("tenant.tour_retirement_settings_changed",
                                      {"after": {"auto_draft": False, "notice_days": 5}}) == "Automatic draft off"
