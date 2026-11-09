"""AA-1039 (S274 rehearsal): a restore puts back the WordPress pages its retirement moved to draft. No database.

- The restore fan-out marks the drafted pages, inserts their jobs in its own transaction, makes the tenant's notice
  wait for them (immediate, not mailed by the fan-out); open (not yet drafted) flags keep today's behaviour.
- republish_page: publishes only a remote draft of a still-servable piece of a served tour; leaves a page the tenant
  removed / published / scheduled alone; records every ending once; a WordPress error is retryable.
- The job: audit row on success, the notice's mail once no page waits, the last attempt gives up and still mails.
- The WordPress calls and the mail wording (content words only).
Invented names and hosts only.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_s4_blog.cms.wordpress import WordPressAdapter
from services.jobs import tour_retirement_jobs as trj
from services.tour_lifecycle import fanout, notice_mail
from shared.llm_client import capacity
from tests.unit.safe_http_fakes import TARGET, response

TENANT = "7d1f3c2a-1111-4c2b-9a77-000000000001"
FLAG = "7d1f3c2a-2222-4c2b-9a77-000000000002"
PIECE = "7d1f3c2a-3333-4c2b-9a77-000000000003"
PUBLISH = "7d1f3c2a-4444-4c2b-9a77-000000000004"
TOUR = "7d1f3c2a-5555-4c2b-9a77-000000000005"
REQUEST = "7d1f3c2a-6666-4c2b-9a77-000000000006"
RESTORE_KEY = "restored:key-1"
PAGE = "https://agency.example/lake-walk"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Answers by SQL constant (a value, or a callable of the args); records every call. Unknown SQL: None / []."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def _answer(self, sql, args, default):
        self.calls.append((sql, args))
        if sql in self.answers:
            a = self.answers[sql]
            return a(*args) if callable(a) else a
        return default

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args, None)

    async def fetchval(self, sql, *args):
        return self._answer(sql, args, None)

    async def fetch(self, sql, *args):
        return self._answer(sql, args, [])

    async def execute(self, sql, *args):
        return self._answer(sql, args, "UPDATE 1")

    def transaction(self):
        return _Tx()

    def ran(self, sql):
        return [args for s, args in self.calls if s == sql]


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _flag(**over):
    row = {"flag_id": FLAG, "tenant_id": TENANT, "state": "resolved", "resolution": "drafted",
           "detail": json.dumps({"tour_name": "Lake Walk", fanout.REPUBLISH_MARK: {
               "event_key": RESTORE_KEY, "state": "queued"}}),
           "tour_id": TOUR, "publish_id": PUBLISH, "piece_id": PIECE, "flag_url": PAGE,
           "publish_status": "unpublished", "unpublished_by": fanout.ACTOR, "external_id": "41",
           "external_url": PAGE, "channel": "blog", "piece_status": "approved", "request_id": REQUEST}
    row.update(over)
    return row


def _conn(flag=None, *, tour_state="served", waiting=0, gate=False, other_live=False, integ=True):
    return FakeConn({
        fanout.REPUBLISH_FLAG_SQL: flag if flag is not None else _flag(),
        fanout.TOUR_STATE_SQL: tour_state,
        fanout.OPEN_GATE_SQL: gate,
        fanout.OTHER_LIVE_POST_SQL: other_live,
        fanout.INTEGRATION_SQL: {"secret_key": "tenant/wp-test"} if integ else None,
        fanout.REPUBLISH_DONE_SQL: RESTORE_KEY,
        fanout.NOTICE_PAGE_SQL: {"notice_id": "n-1", "delivery": "immediate", "mailed_at": None,
                                 "waiting": waiting},
    })


class FakeWP:
    def __init__(self, status="draft", fail=None):
        self.status = status
        self.fail = fail
        self.published = []

    async def post_status(self, post_id):
        if self.fail == "read":
            raise RuntimeError("WP API read 502 returned an unexpected response")
        return self.status

    async def set_published(self, post_id):
        if self.fail == "write":
            raise RuntimeError("WP API update 502 returned an unexpected response")
        self.published.append(post_id)
        return "published"


async def _secret(name):
    return {"wp_url": "https://agency.example", "username": "u", "app_password": "p"}


async def _republish(conn, wp):
    return await fanout.republish_page(FakePool(conn), FLAG, read_secret=_secret, adapter_for=lambda creds: wp)


def _outcome(conn) -> dict:
    (args,) = conn.ran(fanout.REPUBLISH_DONE_SQL)
    return json.loads(args[1])


# ── the restore fan-out ────────────────────────────────────────────────────────────────────────────────────────────

def _event():
    return {"event_id": 9, "source_id": "src-1", "tour_id": TOUR, "event": "restored", "reason": "restore",
            "occurred_at": datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc), "replaced_by_tour_id": None,
            "idempotency_key": RESTORE_KEY}


@pytest.mark.asyncio
async def test_restore_after_a_drafted_page_marks_it_and_inserts_one_job_in_the_fanout_transaction():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.RESOLVE_RESTORED_SQL: [],
        fanout.MARK_REPUBLISH_SQL: [{"flag_id": FLAG, "tenant_id": TENANT, "external_url": PAGE}],
        fanout.OUTSTANDING_RETIRED_SQL: [{"tenant_id": TENANT}],
        fanout.INSERT_NOTICE_SQL: "n-1",
        fanout.ENQUEUE_REPUBLISH_SQL: 1,
    })
    out = await fanout._restored(conn, _event(), [TOUR])
    assert out["pages_to_republish"] == 1 and out["republish_jobs"] == 1
    assert conn.ran(fanout.MARK_REPUBLISH_SQL) == [([TOUR], RESTORE_KEY)]
    assert conn.ran(fanout.ENQUEUE_REPUBLISH_SQL) == [(RESTORE_KEY, fanout.REPUBLISH_ATTEMPTS, [TOUR])]
    (notice,) = conn.ran(fanout.INSERT_NOTICE_SQL)
    assert notice[6] == "immediate"
    assert json.loads(notice[7])["pages_returning"] == [{"url": PAGE, "flag_id": FLAG}]
    # the notice waits for the page: its mail is enqueued by the job, not by the fan-out
    assert out["immediate_notices"] == []


@pytest.mark.asyncio
async def test_a_restore_replay_marks_nothing_new_and_creates_no_second_job():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.MARK_REPUBLISH_SQL: [],              # already marked by the first run
        fanout.OUTSTANDING_RETIRED_SQL: [{"tenant_id": TENANT}],
        fanout.INSERT_NOTICE_SQL: None,             # (tenant, event) notice exists
        fanout.ENQUEUE_REPUBLISH_SQL: 0,            # job key exists
    })
    out = await fanout._restored(conn, _event(), [TOUR])
    assert out["pages_to_republish"] == 0 and out["republish_jobs"] == 0 and out["immediate_notices"] == []


def test_the_mark_and_job_sql_hold_the_idempotency_rules():
    mark = fanout.MARK_REPUBLISH_SQL
    assert "resolution = 'drafted'" in mark and "NOT (f.detail ? 'restore_republish')" in mark
    # only the retirement this restore undoes: retired events after the key's previous restore, before this one
    assert "p.event = 'restored' AND p.id < ev.id" in mark and "r.id > prev.id AND r.id < ev.id" in mark
    job = fanout.ENQUEUE_REPUBLISH_SQL
    assert "'restored_page_republish:' || f.flag_id::text" in job and "ON CONFLICT (idempotency_key)" in job
    assert "'restore_republish' ->> 'state' = 'queued'" in job
    assert "pages_returning" in fanout.UNMAILED_IMMEDIATE_SQL


@pytest.mark.asyncio
async def test_open_flags_keep_todays_behaviour_resolved_and_mailed_at_once():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.RESOLVE_RESTORED_SQL: [{"tenant_id": TENANT}],
        fanout.MARK_REPUBLISH_SQL: [],
        fanout.INSERT_NOTICE_SQL: "n-1",
        fanout.ENQUEUE_REPUBLISH_SQL: 0,
    })
    out = await fanout._restored(conn, _event(), [TOUR])
    assert out["flags_resolved"] == 1 and out["immediate_notices"] == ["n-1"]
    (notice,) = conn.ran(fanout.INSERT_NOTICE_SQL)
    assert notice[6] == "immediate" and json.loads(notice[7])["pages_returning"] == []


# ── republish_page ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_remote_draft_is_published_again_and_the_record_follows():
    conn, wp = _conn(), FakeWP("draft")
    out = await _republish(conn, wp)
    assert out["status"] == "republished" and wp.published == ["41"]
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]
    outcome = _outcome(conn)
    assert outcome["state"] == "republished" and outcome["wordpress"] == "published" and outcome["republished_at"]
    (notice,) = conn.ran(fanout.NOTICE_PAGE_SQL)
    assert notice == (TENANT, RESTORE_KEY, FLAG, PAGE, "pages_back")
    assert out["mail_notice_id"] == "n-1"


@pytest.mark.asyncio
async def test_the_mail_waits_while_another_page_of_the_notice_is_still_coming_back():
    out = await _republish(_conn(waiting=1), FakeWP("draft"))
    assert out["status"] == "republished" and "mail_notice_id" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("remote,reason", [
    ("missing", "removed_on_site"), ("trash", "removed_on_site"), ("publish", "already_published"),
    ("future", "scheduled_on_site"), ("private", "changed_on_site"),
])
async def test_a_page_the_tenant_removed_published_or_scheduled_is_left_alone(remote, reason):
    conn, wp = _conn(), FakeWP(remote)
    out = await _republish(conn, wp)
    assert out["status"] == "skipped" and out["reason"] == reason and wp.published == []
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == []
    assert _outcome(conn)["wordpress_status"] == remote
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] is None        # dropped from the notice, not "kept"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn_kw,flag_kw,state,reason", [
    ({"tour_state": "retired"}, {}, "skipped", "tour_not_served"),
    ({"tour_state": "paused"}, {}, "skipped", "tour_not_served"),
    ({"tour_state": None}, {}, "skipped", "tour_not_served"),
    ({}, {"piece_status": "failed"}, "kept", "piece_not_publishable"),
    ({}, {"piece_status": None}, "kept", "piece_not_publishable"),
    ({"gate": True}, {}, "kept", "content_needs_change"),
    ({"other_live": True}, {}, "skipped", "published_again_from_studio"),
    ({}, {"publish_status": "published", "unpublished_by": None}, "skipped", "publish_record_changed"),
    ({}, {"resolution": "restored"}, "skipped", "flag_changed"),
    ({"integ": False}, {}, "kept", "wordpress_not_connected"),
])
async def test_nothing_is_published_when_the_tour_or_the_piece_no_longer_allows_it(conn_kw, flag_kw, state, reason):
    conn, wp = _conn(_flag(**flag_kw), **conn_kw), FakeWP("draft")
    out = await _republish(conn, wp)
    assert out["status"] == state and out["reason"] == reason and wp.published == []
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == []
    assert _outcome(conn)["reason"] == reason
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] == ("pages_kept" if state == "kept" else None)


@pytest.mark.asyncio
async def test_the_piece_s_own_tour_must_be_served_the_aa986_rule():
    from shared.tour_liveness import ServedState, TourNotServedError

    async def refuse(conn, tour_id, work):
        raise TourNotServedError(ServedState("retired", "master_trashed"), work)

    conn, wp = _conn(), FakeWP("draft")
    with patch.object(fanout._tl, "request_tours", AsyncMock(return_value={REQUEST: TOUR})), \
            patch.object(fanout._tl, "ensure_served", refuse):
        out = await _republish(conn, wp)
    assert out["reason"] == "tour_not_served" and wp.published == []


@pytest.mark.asyncio
async def test_a_page_on_another_site_is_kept_in_draft():
    flag = _flag(external_url="https://old-site.example/lake-walk")
    out = await _republish(_conn(flag), FakeWP("draft"))
    assert out["status"] == "kept" and out["reason"] == "other_site"


@pytest.mark.asyncio
@pytest.mark.parametrize("detail,reason", [
    ({}, "not_marked"),
    ({fanout.REPUBLISH_MARK: {"event_key": RESTORE_KEY, "state": "republished"}}, "already_republished"),
])
async def test_an_unmarked_or_finished_flag_is_not_touched_again(detail, reason):
    conn, wp = _conn(_flag(detail=json.dumps(detail))), FakeWP("draft")
    out = await _republish(conn, wp)
    assert out == {"status": "skipped", "reason": reason} and wp.published == []
    assert conn.ran(fanout.REPUBLISH_DONE_SQL) == [] and conn.ran(fanout.NOTICE_PAGE_SQL) == []


@pytest.mark.asyncio
async def test_a_second_run_that_loses_the_race_changes_nothing():
    conn = _conn()
    conn.answers[fanout.REPUBLISH_DONE_SQL] = None
    out = await _republish(conn, FakeWP("publish"))
    assert out["status"] == "skipped" and out["reason"] == "already_done"
    assert conn.ran(fanout.NOTICE_PAGE_SQL) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["read", "write"])
async def test_a_wordpress_error_is_noted_and_retryable(fail):
    conn = _conn()
    with pytest.raises(fanout.RepublishRefused) as exc:
        await _republish(conn, FakeWP("draft", fail=fail))
    assert not exc.value.permanent
    (note,) = conn.ran(fanout.REPUBLISH_NOTE_SQL)
    assert "502" in json.loads(note[1])["error"]
    assert conn.ran(fanout.REPUBLISH_DONE_SQL) == [] and conn.ran(fanout.MARK_REPUBLISHED_SQL) == []


# ── the job ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _ctx(attempt=1, max_attempts=4):
    from shared.jobs import queue
    from shared.jobs.registry import JobContext
    job = queue.Job(id=str(uuid.uuid4()), kind=trj.REPUBLISH_KIND, payload={"flag_id": FLAG}, status="running",
                    attempt=attempt, max_attempts=max_attempts, cost_usd=0.0, progress={})
    return JobContext(MagicMock(), job, {})


def test_the_kind_is_registered_with_no_llm_line():
    from shared.jobs.registry import kinds
    assert trj.REPUBLISH_KIND == "restored_page_republish"
    assert kinds()[trj.REPUBLISH_KIND].max_attempts == fanout.REPUBLISH_ATTEMPTS
    assert trj.REPUBLISH_KIND in capacity.KIND_LINES and capacity.KIND_LINES[trj.REPUBLISH_KIND] is None


@pytest.mark.asyncio
async def test_job_success_writes_the_audit_row_and_mails_the_notice_once_ready():
    done = {"status": "republished", "flag_id": FLAG, "tenant_id": TENANT, "piece_id": PIECE,
            "publish_id": PUBLISH, "mail_notice_id": "n-1"}
    with patch.object(trj.fanout, "republish_page", AsyncMock(return_value=done)), \
            patch.object(trj, "enqueue", AsyncMock(return_value=("mail-1", True))) as enq, \
            patch("services.acp_shared.audit_log.write_audit_log", AsyncMock()) as audit:
        out = await trj.run_republish(_ctx())
    assert out["mail_job"] == "mail-1"
    assert enq.await_args.kwargs["idempotency_key"] == "tour_notice_email:n-1"
    assert audit.await_args.kwargs["details"]["reason"] == "tour_restored"


@pytest.mark.asyncio
async def test_job_retries_a_wordpress_error_until_the_last_attempt_then_keeps_the_page_and_mails():
    refused = fanout.RepublishRefused("WP API read 502", permanent=False)
    with patch.object(trj.fanout, "republish_page", AsyncMock(side_effect=refused)), \
            patch.object(trj.fanout, "give_up_republish", AsyncMock(return_value={"mail_notice_id": "n-1"})) as gu, \
            patch.object(trj, "enqueue", AsyncMock(return_value=("mail-1", True))) as enq:
        with pytest.raises(RuntimeError) as early:
            await trj.run_republish(_ctx(attempt=1))
        assert gu.await_count == 0 and enq.await_count == 0
        from shared.jobs.registry import NonRetryable
        assert not isinstance(early.value, NonRetryable)
        with pytest.raises(NonRetryable):
            await trj.run_republish(_ctx(attempt=4))
    assert gu.await_count == 1 and enq.await_args.kwargs["idempotency_key"] == "tour_notice_email:n-1"


@pytest.mark.asyncio
async def test_give_up_keeps_the_page_and_tells_the_tenant():
    conn = _conn()
    out = await fanout.give_up_republish(FakePool(conn), FLAG, "WP API read 502")
    assert out["status"] == "kept" and out["reason"] == "wordpress_error" and out["mail_notice_id"] == "n-1"
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] == "pages_kept"


# ── WordPress ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (200, {"id": 41, "status": "draft"}, "draft"),
    (200, {"id": 41, "status": "publish"}, "publish"),
    (200, {"id": 41, "status": "trash"}, "trash"),
    (404, {"code": "rest_post_invalid_id", "message": "Invalid post ID."}, "missing"),
])
async def test_wordpress_post_status(status, body, expected):
    call = AsyncMock(return_value=response(status, "application/json", json.dumps(body)))
    with patch(TARGET, call):
        assert await WordPressAdapter("https://agency.example", "u", "p").post_status(41) == expected
    assert call.await_args.args[0] == "GET" and call.await_args.kwargs["params"] == {"context": "edit"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ctype,text", [
    (404, "text/html", "<html>not found</html>"),
    (200, "text/html", "<html>challenge</html>"),
    (401, "application/json", json.dumps({"code": "rest_forbidden_context", "message": "no"})),
])
async def test_wordpress_post_status_refuses_a_host_page(status, ctype, text):
    with patch(TARGET, AsyncMock(return_value=response(status, ctype, text))):
        with pytest.raises(RuntimeError):
            await WordPressAdapter("https://agency.example", "u", "p").post_status(41)


@pytest.mark.asyncio
async def test_wordpress_set_published():
    call = AsyncMock(return_value=response(200, "application/json", json.dumps({"id": 41, "status": "publish"})))
    with patch(TARGET, call):
        assert await WordPressAdapter("https://agency.example", "u", "p").set_published(41) == "published"
    assert call.await_args.args[0] == "POST" and call.await_args.kwargs["json"] == {"status": "publish"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (200, {"id": 41, "status": "pending"}),
    (403, {"code": "rest_cannot_publish", "message": "no"}),
    (200, "<html>challenge</html>"),
])
async def test_wordpress_set_published_refuses_anything_else(status, body):
    text = body if isinstance(body, str) else json.dumps(body)
    ctype = "text/html" if isinstance(body, str) else "application/json"
    with patch(TARGET, AsyncMock(return_value=response(status, ctype, text))):
        with pytest.raises(RuntimeError):
            await WordPressAdapter("https://agency.example", "u", "p").set_published(41)


# ── the mail ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_back_on_sale_mail_says_which_pages_are_online_again():
    notice = {"event": "restored", "tour_name": "Lake Walk Weekend", "summary": {
        "back_on": "10 November 2026", "pages_back": [{"url": PAGE}],
        "pages_kept": [{"url": "https://agency.example/hill-trail"}],
        "pages_returning": [{"url": "https://agency.example/river", "flag_id": FLAG}]}}
    msg = notice_mail.render([notice], to=("owner@agency.example",), agency="Invented Agency")
    assert msg.subject == "Lake Walk Weekend is back on sale"
    assert f"Your page {PAGE}, which we moved to draft while the tour was off sale, is online again." in msg.text
    assert "https://agency.example/hill-trail is still in draft" in msg.text
    assert "https://agency.example/river, which we moved to draft while the tour was off sale, is being put back" \
        in msg.text
    for word in ("flag", " qa", "compliance", "tour_retired", "aa-1039", FLAG):
        assert word not in msg.text.lower() and word not in msg.html.lower()
