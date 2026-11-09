"""AA-1039 (S274 rehearsal + review): a restore puts back the WordPress pages its retirement moved to draft, and only
those that were public before. No database.

- move_to_draft drafts only a post WordPress says is `publish` and records that (+ modified_gmt); its final write
  holds the key lock, and a restore that won the race gets the page published back (B1, M4).
- The restore fan-out marks the drafted pages (queued only when public before; older flags kept in draft and told),
  inserts their jobs in its own transaction, makes the tenant's notice wait for them; open flags keep today's
  behaviour; a key without a served state is not served (m4).
- republish_page: same draft unchanged since (M1), the latest version of the request with no other live post (M2),
  claim first under the lock, revert on a WordPress failure, never an untracked live post (M3); a deleted piece is
  skipped (m3).
- The jobs: every failure on the last attempt gives the page up and mails (m1); the digest backstop keeps waiting
  pages; a "back on sale" notice of a key off sale again is closed unsent (m2).
- The WordPress calls and the mail wording (content words only).
Invented names and hosts only.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from services.acp_s4_blog.cms.wordpress import PostChange, PostState, WordPressAdapter
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
MODIFIED = "2026-11-03T09:00:00"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Answers by SQL constant (a value, or a callable of the args); records every call in `log` (shared with the
    fake WordPress site, so the order of DB writes and remote calls can be asserted). Unknown SQL: None / []."""

    def __init__(self, answers=None, log=None):
        self.answers = dict(answers or {})
        self.calls = []
        self.log = log if log is not None else []

    def _answer(self, sql, args, default):
        self.calls.append((sql, args))
        self.log.append(("sql", sql))
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

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _detail(**over):
    d = {"tour_name": "Lake Walk", "wordpress": "drafted", "wordpress_before": "publish",
         "wordpress_modified_gmt": MODIFIED, fanout.REPUBLISH_MARK: {"event_key": RESTORE_KEY, "state": "queued"}}
    d.update(over)
    return json.dumps(d)


def _flag(**over):
    row = {"flag_id": FLAG, "tenant_id": TENANT, "state": "resolved", "resolution": "drafted", "detail": _detail(),
           "tour_id": TOUR, "publish_id": PUBLISH, "piece_id": PIECE, "flag_url": PAGE,
           "publish_status": "unpublished", "unpublished_by": fanout.ACTOR, "external_id": "41",
           "external_url": PAGE, "channel": "blog", "piece_status": "approved", "request_id": REQUEST,
           "superseded": False}
    row.update(over)
    return row


def _conn(flag=None, *, tour_state="served", waiting=0, gate=False, other_live=False, integ=True, log=None):
    return FakeConn({
        fanout.REPUBLISH_FLAG_SQL: flag if flag is not None else _flag(),
        fanout.TOUR_STATE_SQL: tour_state,
        fanout.TOUR_KEY_SQL: "src-1",
        fanout.OPEN_GATE_SQL: gate,
        fanout.OTHER_LIVE_POST_SQL: other_live,
        fanout.INTEGRATION_SQL: {"secret_key": "tenant/wp-test"} if integ else None,
        fanout.REPUBLISH_DONE_SQL: RESTORE_KEY,
        fanout.NOTICE_PAGE_SQL: {"notice_id": "n-1", "delivery": "immediate", "mailed_at": None,
                                 "waiting": waiting},
    }, log)


class FakeWP:
    def __init__(self, status="draft", modified=MODIFIED, fail=None, log=None, after_publish_status=None):
        self.status = status
        self.modified = modified
        self.fail = fail
        self.log = log if log is not None else []
        self.after_publish_status = after_publish_status
        self.published = []
        self.drafted = []

    async def post_status(self, post_id):
        self.log.append(("wp", "status"))
        if self.fail == "read":
            raise RuntimeError("WP API read 502 returned an unexpected response")
        return PostState(self.status, self.modified)

    async def draft_post(self, post_id):
        self.log.append(("wp", "draft"))
        self.drafted.append(post_id)
        self.status = "draft"
        return PostChange("drafted", "2026-11-10T08:00:00")

    async def set_published(self, post_id):
        self.log.append(("wp", "publish"))
        if self.fail == "write":
            if self.after_publish_status:
                self.status = self.after_publish_status
            raise RuntimeError("WP API update 502 returned an unexpected response")
        self.published.append(post_id)
        self.status = "publish"
        return "published"


async def _secret(name):
    return {"wp_url": "https://agency.example", "username": "u", "app_password": "p"}


async def _republish(conn, wp):
    return await fanout.republish_page(FakePool(conn), FLAG, read_secret=_secret, adapter_for=lambda creds: wp)


def _outcome(conn) -> dict:
    (args,) = conn.ran(fanout.REPUBLISH_DONE_SQL)
    return json.loads(args[1])


# ── strict served (m4) ─────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_served_state_is_served():
    assert fanout.served("served")
    for state in (None, "retired", "paused", ""):
        assert not fanout.served(state)


def _event(kind="restored"):
    return {"event_id": 9, "source_id": "src-1", "tour_id": TOUR, "event": kind, "reason": "restore",
            "occurred_at": datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc), "replaced_by_tour_id": None,
            "idempotency_key": RESTORE_KEY}


@pytest.mark.asyncio
async def test_a_restore_of_a_key_without_a_served_state_does_nothing():
    conn = FakeConn({fanout.EVENT_SQL: _event(), fanout.KEY_TOURS_SQL: [{"tour_id": TOUR}], fanout.STATE_SQL: None})
    out = await fanout.fan_out(FakePool(conn), RESTORE_KEY)
    assert out == {"status": "skipped", "reason": "key_is_unknown_now"}
    assert conn.ran(fanout.MARK_REPUBLISH_SQL) == [] and conn.ran(fanout.RESOLVE_RESTORED_SQL) == []
    # the state is read under the key lock
    order = [s for s, _ in conn.calls]
    assert order.index(fanout.KEY_LOCK_SQL) < order.index(fanout.STATE_SQL)


# ── the restore fan-out ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_after_a_drafted_page_marks_it_and_inserts_one_job_in_the_fanout_transaction():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.RESOLVE_RESTORED_SQL: [],
        fanout.MARK_REPUBLISH_SQL: [{"flag_id": FLAG, "tenant_id": TENANT, "external_url": PAGE, "state": "queued"}],
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
    assert out["immediate_notices"] == []          # waits for the page: the job mails it


@pytest.mark.asyncio
async def test_a_page_drafted_before_the_public_check_stays_in_draft_and_the_tenant_is_told_at_once():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.MARK_REPUBLISH_SQL: [{"flag_id": FLAG, "tenant_id": TENANT, "external_url": PAGE, "state": "kept"},
                                    {"flag_id": "f-2", "tenant_id": TENANT, "external_url": "https://x.example/p",
                                     "state": "skipped"}],
        fanout.INSERT_NOTICE_SQL: "n-1",
        fanout.ENQUEUE_REPUBLISH_SQL: 0,
    })
    out = await fanout._restored(conn, _event(), [TOUR])
    assert out["pages_to_republish"] == 0 and out["pages_kept_in_draft"] == 1 and out["immediate_notices"] == ["n-1"]
    summary = json.loads(conn.ran(fanout.INSERT_NOTICE_SQL)[0][7])
    assert summary["pages_kept"] == [{"url": PAGE}] and summary["pages_returning"] == []


@pytest.mark.asyncio
async def test_a_restore_replay_marks_nothing_new_and_creates_no_second_job():
    conn = FakeConn({
        fanout.TOUR_NAME_SQL: "Lake Walk",
        fanout.MARK_REPUBLISH_SQL: [],
        fanout.OUTSTANDING_RETIRED_SQL: [{"tenant_id": TENANT}],
        fanout.INSERT_NOTICE_SQL: None,
        fanout.ENQUEUE_REPUBLISH_SQL: 0,
    })
    out = await fanout._restored(conn, _event(), [TOUR])
    assert out["pages_to_republish"] == 0 and out["republish_jobs"] == 0 and out["immediate_notices"] == []


def test_the_mark_and_job_sql_hold_the_rules():
    mark = fanout.MARK_REPUBLISH_SQL
    assert "resolution = 'drafted'" in mark and "NOT (f.detail ? 'restore_republish')" in mark
    assert "p.event = 'restored' AND p.id < ev.id" in mark and "r.id > prev.id AND r.id < ev.id" in mark
    # queued only for a page moved from publish to draft by this system, with its modified time recorded
    assert "f.detail ->> 'wordpress_before' = 'publish'" in mark and "wordpress_modified_gmt" in mark
    assert "'drafted_before_check'" in mark
    job = fanout.ENQUEUE_REPUBLISH_SQL
    assert "'restored_page_republish:' || f.flag_id::text" in job and "ON CONFLICT (idempotency_key)" in job
    assert "'restore_republish' ->> 'state' = 'queued'" in job
    assert "pages_returning" in fanout.UNMAILED_IMMEDIATE_SQL
    # M2: any piece of the request
    assert "op.angle_gate_request_id = $1::uuid" in fanout.OTHER_LIVE_POST_SQL


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


# ── move_to_draft (B1, M4) ─────────────────────────────────────────────────────────────────────────────────────────

def _open_flag(**over):
    row = {"flag_id": FLAG, "tenant_id": TENANT, "state": "open", "action": "draft_after_notice",
           "draft_due_at": datetime.now(timezone.utc) - timedelta(minutes=1), "resolution": None,
           "detail": json.dumps({"tour_name": "Lake Walk"}), "tour_id": TOUR, "publish_id": PUBLISH,
           "piece_id": PIECE, "publish_status": "published", "unpublished_at": None, "external_id": "41",
           "external_url": PAGE, "channel": "blog"}
    row.update(over)
    return row


def _draft_conn(flag=None, *, tour_state="retired", resolved=FLAG, log=None):
    return FakeConn({
        fanout.FLAG_SQL: flag if flag is not None else _open_flag(),
        fanout.TOUR_STATE_SQL: tour_state,
        fanout.TOUR_KEY_SQL: "src-1",
        fanout.PIECE_SUBJECT_ALIVE_SQL: False,
        fanout.INTEGRATION_SQL: {"secret_key": "tenant/wp-test"},
        fanout.RESOLVE_ONE_SQL: resolved,
    }, log)


async def _draft(conn, wp):
    return await fanout.move_to_draft(FakePool(conn), FLAG, read_secret=_secret, adapter_for=lambda creds: wp)


@pytest.mark.asyncio
async def test_a_public_post_is_drafted_and_the_public_status_and_modified_time_are_recorded():
    log = []
    conn, wp = _draft_conn(log=log), FakeWP("publish", log=log)
    out = await _draft(conn, wp)
    assert out["status"] == "drafted" and wp.drafted == ["41"]
    (resolve,) = conn.ran(fanout.RESOLVE_ONE_SQL)
    assert resolve[1] == "drafted"
    assert json.loads(resolve[3]) == {"wordpress": "drafted", "wordpress_before": "publish",
                                      "wordpress_modified_gmt": "2026-11-10T08:00:00"}
    assert conn.ran(fanout.MARK_UNPUBLISHED_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]
    # M4: the final write holds the key's fan-out lock
    order = [e[1] for e in log]
    assert order.index(fanout.KEY_LOCK_SQL) > order.index("draft")
    assert order.index(fanout.KEY_LOCK_SQL) < order.index(fanout.RESOLVE_ONE_SQL)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", ["private", "pending", "draft", "future", "trash", "missing"])
async def test_a_post_that_is_not_public_is_never_touched(remote):
    conn, wp = _draft_conn(), FakeWP(remote)
    out = await _draft(conn, wp)
    assert wp.drafted == [] and out["resolution"] == "unpublished" and out["wordpress_status"] == remote
    (resolve,) = conn.ran(fanout.RESOLVE_ONE_SQL)
    assert resolve[1] == "unpublished"
    assert json.loads(resolve[3]) == {"wordpress": "not_public", "wordpress_before": remote}
    assert conn.ran(fanout.MARK_UNPUBLISHED_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]


@pytest.mark.asyncio
async def test_a_key_without_a_state_is_not_served_so_the_due_page_is_drafted():
    conn, wp = _draft_conn(tour_state=None), FakeWP("publish")
    out = await _draft(conn, wp)
    assert out["status"] == "drafted" and wp.drafted == ["41"]


@pytest.mark.asyncio
async def test_a_restore_that_won_the_race_gets_the_page_published_back():
    conn, wp = _draft_conn(resolved=None), FakeWP("publish")
    out = await _draft(conn, wp)
    assert wp.drafted == ["41"] and wp.published == ["41"] and out["status"] == "reverted"
    assert conn.ran(fanout.MARK_UNPUBLISHED_SQL) == []
    notes = [json.loads(a[1]) for a in conn.ran(fanout.NOTE_SQL)]
    assert notes[0][fanout.DRAFT_REVERT] == {"state": "pending"}
    assert notes[-1][fanout.DRAFT_REVERT]["state"] == "done"


@pytest.mark.asyncio
async def test_a_failed_revert_stays_pending_and_the_next_run_retries_it():
    conn, wp = _draft_conn(resolved=None), FakeWP("publish", fail="write")
    with pytest.raises(fanout.DraftRefused) as exc:
        await _draft(conn, wp)
    assert not exc.value.permanent
    assert json.loads(conn.ran(fanout.NOTE_SQL)[-1][1])[fanout.DRAFT_REVERT]["state"] == "pending"
    pending = _open_flag(state="resolved", resolution="restored",
                         detail=json.dumps({fanout.DRAFT_REVERT: {"state": "pending"}}))
    conn2, wp2 = _draft_conn(pending), FakeWP("draft")
    out = await _draft(conn2, wp2)
    assert out["status"] == "reverted" and wp2.published == ["41"]


# ── republish_page ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_same_draft_is_published_again_after_a_claim_under_the_lock():
    log = []
    conn, wp = _conn(log=log), FakeWP("draft", log=log)
    out = await _republish(conn, wp)
    assert out["status"] == "republished" and wp.published == ["41"]
    order = [e[1] for e in log]
    # M3: lock, re-check, claim (publish_log live again), then WordPress
    assert order.index(fanout.KEY_LOCK_SQL) < order.index(fanout.MARK_REPUBLISHED_SQL) < order.index("publish")
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]
    assert conn.ran(fanout.UNCLAIM_SQL) == []
    outcome = _outcome(conn)
    assert outcome["state"] == "republished" and outcome["wordpress"] == "published"
    assert conn.ran(fanout.NOTICE_PAGE_SQL) == [(TENANT, RESTORE_KEY, FLAG, PAGE, "pages_back")]
    assert out["mail_notice_id"] == "n-1"


@pytest.mark.asyncio
async def test_the_mail_waits_while_another_page_of_the_notice_is_still_coming_back():
    out = await _republish(_conn(waiting=1), FakeWP("draft"))
    assert out["status"] == "republished" and "mail_notice_id" not in out


@pytest.mark.asyncio
async def test_a_draft_the_tenant_edited_since_is_kept():
    conn, wp = _conn(), FakeWP("draft", modified="2026-11-08T10:00:00")
    out = await _republish(conn, wp)
    assert out["status"] == "kept" and out["reason"] == "changed_on_site" and wp.published == []
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == []
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] == "pages_kept"


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
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("conn_kw,flag_kw,state,reason", [
    ({"tour_state": "retired"}, {}, "skipped", "tour_not_served"),
    ({"tour_state": "paused"}, {}, "skipped", "tour_not_served"),
    ({"tour_state": None}, {}, "skipped", "tour_not_served"),
    ({}, {"piece_status": "failed"}, "kept", "piece_not_publishable"),
    ({}, {"piece_status": None, "request_id": None, "superseded": None}, "skipped", "piece_deleted"),
    ({}, {"superseded": True}, "kept", "newer_version"),
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
async def test_another_live_post_of_any_piece_of_the_request_is_looked_up_by_request():
    conn = _conn(other_live=True)
    await _republish(conn, FakeWP("draft"))
    (args,) = conn.ran(fanout.OTHER_LIVE_POST_SQL)
    assert args == (REQUEST, TENANT, list(fanout.WORDPRESS_CHANNELS), PUBLISH)


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
async def test_a_retire_between_the_first_check_and_the_claim_stops_it():
    states = iter(["served", "retired"])
    conn, wp = _conn(), FakeWP("draft")
    conn.answers[fanout.TOUR_STATE_SQL] = lambda *a: next(states)
    out = await _republish(conn, wp)
    assert out["status"] == "skipped" and out["reason"] == "tour_not_served" and wp.published == []
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == []


@pytest.mark.asyncio
async def test_a_studio_publish_of_the_same_revision_at_the_claim_keeps_the_page_in_draft():
    def unique(*args):
        raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")

    conn, wp = _conn(), FakeWP("draft")
    conn.answers[fanout.MARK_REPUBLISHED_SQL] = unique
    out = await _republish(conn, wp)
    assert out["status"] == "kept" and out["reason"] == "published_again_from_studio" and wp.published == []


@pytest.mark.asyncio
async def test_a_wordpress_refusal_after_the_claim_reverts_it_and_retries():
    conn = _conn()
    with pytest.raises(fanout.RepublishRefused) as exc:
        await _republish(conn, FakeWP("draft", fail="write"))
    assert not exc.value.permanent
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) and conn.ran(fanout.UNCLAIM_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]
    assert conn.ran(fanout.REPUBLISH_DONE_SQL) == []


@pytest.mark.asyncio
async def test_a_lost_answer_after_wordpress_published_counts_as_published():
    conn, wp = _conn(), FakeWP("draft", fail="write", after_publish_status="publish")
    out = await _republish(conn, wp)
    assert out["status"] == "republished" and conn.ran(fanout.UNCLAIM_SQL) == []


@pytest.mark.asyncio
async def test_a_status_read_error_is_noted_and_retryable_before_any_claim():
    conn = _conn()
    with pytest.raises(fanout.RepublishRefused):
        await _republish(conn, FakeWP("draft", fail="read"))
    assert "502" in json.loads(conn.ran(fanout.REPUBLISH_NOTE_SQL)[0][1])["error"]
    assert conn.ran(fanout.MARK_REPUBLISHED_SQL) == []


@pytest.mark.asyncio
async def test_a_page_on_another_site_is_kept_in_draft():
    out = await _republish(_conn(_flag(external_url="https://old-site.example/lake-walk")), FakeWP("draft"))
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
async def test_give_up_reverts_a_claim_keeps_the_page_and_tells_the_tenant():
    conn = _conn(_flag(detail=_detail(**{fanout.REPUBLISH_MARK: {"event_key": RESTORE_KEY, "state": "queued",
                                                                 "claimed_at": "2026-11-10T09:00:00"}})))
    out = await fanout.give_up_republish(FakePool(conn), FLAG, "WP API read 502")
    assert out["status"] == "kept" and out["reason"] == "wordpress_error" and out["mail_notice_id"] == "n-1"
    assert conn.ran(fanout.UNCLAIM_SQL) == [(PUBLISH, TENANT, fanout.ACTOR)]
    assert conn.ran(fanout.NOTICE_PAGE_SQL)[0][4] == "pages_kept"


# ── the jobs ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _ctx(attempt=1, max_attempts=4, pool=None, kind=None, payload=None):
    from shared.jobs import queue
    from shared.jobs.registry import JobContext
    job = queue.Job(id=str(uuid.uuid4()), kind=kind or trj.REPUBLISH_KIND, payload=payload or {"flag_id": FLAG},
                    status="running", attempt=attempt, max_attempts=max_attempts, cost_usd=0.0, progress={})
    return JobContext(pool or MagicMock(), job, {})


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
@pytest.mark.parametrize("error", [fanout.RepublishRefused("WP API read 502", permanent=False),
                                   ConnectionResetError("db gone"), ValueError("bad row")])
async def test_job_retries_any_failure_until_the_last_attempt_then_keeps_the_page_and_mails(error):
    from shared.jobs.registry import NonRetryable
    with patch.object(trj.fanout, "republish_page", AsyncMock(side_effect=error)), \
            patch.object(trj.fanout, "give_up_republish", AsyncMock(return_value={"mail_notice_id": "n-1"})) as gu, \
            patch.object(trj, "enqueue", AsyncMock(return_value=("mail-1", True))) as enq:
        with pytest.raises(Exception) as early:
            await trj.run_republish(_ctx(attempt=1))
        assert not isinstance(early.value, NonRetryable) and gu.await_count == 0 and enq.await_count == 0
        with pytest.raises(NonRetryable):
            await trj.run_republish(_ctx(attempt=4))
    assert gu.await_count == 1 and enq.await_args.kwargs["idempotency_key"] == "tour_notice_email:n-1"


def _notice(**over):
    n = {"notice_id": "n-1", "tenant_id": TENANT, "tour_id": TOUR, "event": "restored", "tour_name": "Lake Walk",
         "delivery": "immediate", "summary": {"back_on": "10 November 2026"}, "mailed_at": None,
         "created_at": datetime.now(timezone.utc)}
    n.update(over)
    return n


@pytest.mark.asyncio
async def test_the_digest_backstop_moves_pages_still_waiting_to_kept():
    waiting = {"back_on": "10 November 2026", "pages_returning": [{"url": PAGE, "flag_id": FLAG}]}
    kept = {"back_on": "10 November 2026", "pages_returning": [], "pages_kept": [{"url": PAGE}]}
    conn = FakeConn({fanout.TOUR_STATE_SQL: "served", trj.KEEP_WAITING_SQL: json.dumps(kept)})
    out, closed = await trj._prepare_restored(_ctx(pool=FakePool(conn)), [_notice(summary=waiting)])
    assert closed == 0 and out[0]["summary"] == kept
    assert conn.ran(trj.KEEP_WAITING_SQL) == [("n-1",)]
    text = notice_mail.render(out, to=("a@agency.example",), agency="Invented Agency").text
    assert "is still in draft" in text and "being put back" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["retired", "paused", None])
async def test_a_back_on_sale_notice_of_a_key_off_sale_again_is_closed_unsent(state):
    conn = FakeConn({fanout.TOUR_STATE_SQL: state})
    retired = _notice(notice_id="n-2", event="retired", summary={"stop_date": "11 November 2026"})
    ctx = _ctx(pool=FakePool(conn))
    out, closed = await trj._prepare_restored(ctx, [_notice(), retired])
    assert closed == 1 and [n["notice_id"] for n in out] == ["n-2"]
    (args,) = conn.ran(trj.CLOSE_UNSENT_SQL)
    assert args[0] == ["n-1"] and args[1] == str(ctx.job_id) and "off sale" in args[2]


# ── WordPress ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (200, {"id": 41, "status": "draft", "modified_gmt": MODIFIED}, PostState("draft", MODIFIED)),
    (200, {"id": 41, "status": "publish", "modified_gmt": MODIFIED}, PostState("publish", MODIFIED)),
    (200, {"id": 41, "status": "trash"}, PostState("trash", None)),
    (404, {"code": "rest_post_invalid_id", "message": "Invalid post ID."}, PostState("missing", None)),
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
async def test_wordpress_draft_post_returns_the_modified_time():
    body = {"id": 41, "status": "draft", "modified_gmt": "2026-11-10T08:00:00"}
    with patch(TARGET, AsyncMock(return_value=response(200, "application/json", json.dumps(body)))):
        out = await WordPressAdapter("https://agency.example", "u", "p").draft_post(41)
    assert out == PostChange("drafted", "2026-11-10T08:00:00")


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
    assert "https://agency.example/river" not in msg.text            # a waiting page is never named
    for word in ("flag", " qa", "compliance", "tour_retired", "aa-1039", FLAG):
        assert word not in msg.text.lower() and word not in msg.html.lower()
