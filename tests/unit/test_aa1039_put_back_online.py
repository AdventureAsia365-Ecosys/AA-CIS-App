"""AA-1039 follow-up (option A, S275): "Put back online" instead of an automatic republish after a restore.

- WordPressAdapter.post_status / set_published: AA-460 shape checks (a host page or a 2xx that is not the post in the
  asked state raises; WordPress's own "gone" codes read as missing).
- put_back.remote_put_back: a draft is published, a live page only recorded, anything else left as is; a lost answer
  to a publish that went through counts as published.
- POST /v1/publish-log/{publish_id}/put-back: tenant-scoped (404 for a page that is not the tenant's or was never
  drafted by the retirement), the publish route's checks, the claim (409 for a concurrent click), idempotent replays,
  revert on "left as is" and on a WordPress error, the flag note and the audit row.
- GET /v1/publish-log/put-back: only the retirement's drafts no click settled, about a served tour.
- The restore notice: the mail names each drafted page; the bell row carries the pages.

No network, no DB: fakes only. Invented names.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

from api.routers import v1_notifications as notif_mod
from api.routers import v1_publish as pub_mod
from api.services import tenant_account
from services.acp_publish import put_back as pb
from services.acp_s4_blog.cms.wordpress import PostState, WordPressAdapter
from services.tour_lifecycle import notice_mail
from shared.tour_liveness import ServedState, TourNotServedError
from tests.unit.safe_http_fakes import TARGET, response

TENANT_ID = str(uuid.uuid4())
TENANT = {"sub": TENANT_ID, "role": "tenant"}
PUBLISH_ID = uuid.uuid4()
PIECE_ID = str(uuid.uuid4())
FLAG_ID = str(uuid.uuid4())
REQUEST_ID = str(uuid.uuid4())
WP_CREDS = {"wp_url": "https://lantern-bay.example", "username": "u", "app_password": "p"}
URL = "https://lantern-bay.example/misty-ridge-walk/"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def _json(status, body, content_type="application/json"):
    return response(status, content_type, body if isinstance(body, str) else json.dumps(body))


# ── WordPressAdapter ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAdapter:
    adapter = WordPressAdapter(wp_url="https://lantern-bay.example", username="u", app_password="p")

    async def test_post_status_reads_the_edit_context(self):
        call = AsyncMock(return_value=_json(200, {"id": 6, "status": "draft", "modified_gmt": "2026-11-09T10:00:00"}))
        with patch(TARGET, call):
            state = await self.adapter.post_status(6)
        assert state == PostState("draft", "2026-11-09T10:00:00")
        method, url = call.call_args[0]
        assert method == "GET" and url.endswith("/wp-json/wp/v2/posts/6")
        assert call.call_args.kwargs["params"] == {"context": "edit"}

    @pytest.mark.parametrize("status,code", [(404, "rest_post_invalid_id"), (410, "rest_already_trashed")])
    async def test_post_status_gone_is_missing(self, status, code):
        with patch(TARGET, AsyncMock(return_value=_json(status, {"code": code, "message": "x"}))):
            assert (await self.adapter.post_status(6)).status == "missing"

    async def test_post_status_host_page_raises(self):
        with patch(TARGET, AsyncMock(return_value=_json(404, "<html>Not Found</html>", "text/html"))):
            with pytest.raises(RuntimeError, match="unexpected response"):
                await self.adapter.post_status(6)

    async def test_post_status_refused_read_raises(self):
        body = {"code": "rest_forbidden_context", "message": "Sorry, you are not allowed to edit this post."}
        with patch(TARGET, AsyncMock(return_value=_json(401, body))):
            with pytest.raises(RuntimeError, match="refused the read"):
                await self.adapter.post_status(6)

    async def test_set_published_needs_the_post_back_as_publish(self):
        call = AsyncMock(return_value=_json(200, {"id": 6, "status": "publish"}))
        with patch(TARGET, call):
            assert await self.adapter.set_published(6) == "published"
        assert call.call_args[0][0] == "POST" and call.call_args.kwargs["json"] == {"status": "publish"}

    async def test_set_published_answer_still_draft_raises(self):
        with patch(TARGET, AsyncMock(return_value=_json(200, {"id": 6, "status": "draft"}))):
            with pytest.raises(RuntimeError, match="unexpected response"):
                await self.adapter.set_published(6)

    async def test_set_published_refused_raises_permission_message(self):
        body = {"code": "rest_cannot_publish", "message": "Sorry, you are not allowed to publish posts."}
        with patch(TARGET, AsyncMock(return_value=_json(403, body))):
            with pytest.raises(RuntimeError, match="cannot publish"):
                await self.adapter.set_published(6)

    async def test_set_draft_still_works_through_the_shared_call(self):
        call = AsyncMock(return_value=_json(200, {"id": 6, "status": "draft"}))
        with patch(TARGET, call):
            assert await self.adapter.set_draft(6) == "drafted"
        assert call.call_args.kwargs["json"] == {"status": "draft"}


# ── remote_put_back ────────────────────────────────────────────────────────────────────────────────────────────────

class _WP:
    def __init__(self, *states, publish_error=None):
        self.states = list(states)
        self.publish_error = publish_error
        self.published = []

    async def post_status(self, post_id):
        s = self.states.pop(0)
        if isinstance(s, Exception):
            raise s
        return PostState(s, None)

    async def set_published(self, post_id):
        self.published.append(post_id)
        if self.publish_error:
            raise self.publish_error
        return "published"


@pytest.mark.asyncio
class TestRemote:
    async def test_a_draft_is_published(self):
        wp = _WP("draft")
        assert await pb.remote_put_back(wp, "6") == (pb.PUBLISHED, "publish") and wp.published == ["6"]

    async def test_a_live_page_is_only_recorded(self):
        wp = _WP("publish")
        assert await pb.remote_put_back(wp, "6") == (pb.ALREADY_ONLINE, "publish") and wp.published == []

    @pytest.mark.parametrize("status", ["private", "pending", "future", "trash", "missing"])
    async def test_anything_else_is_left_as_is(self, status):
        wp = _WP(status)
        assert await pb.remote_put_back(wp, "6") == (pb.CHANGED_ON_WORDPRESS, status) and wp.published == []

    async def test_a_lost_answer_to_a_publish_that_went_through_is_published(self):
        wp = _WP("draft", "publish", publish_error=RuntimeError("timeout"))
        assert await pb.remote_put_back(wp, "6") == (pb.PUBLISHED, "publish")

    async def test_a_refused_publish_raises(self):
        wp = _WP("draft", "draft", publish_error=RuntimeError("refused"))
        with pytest.raises(RuntimeError, match="refused"):
            await pb.remote_put_back(wp, "6")

    async def test_an_unreadable_post_raises_and_publishes_nothing(self):
        wp = _WP(RuntimeError("host page"))
        with pytest.raises(RuntimeError):
            await pb.remote_put_back(wp, "6")
        assert wp.published == []


# ── the route ──────────────────────────────────────────────────────────────────────────────────────────────────────

ROW_SQL = pb.ROW_SQL.format(tenant_cond=pb.ROW_TENANT_COND)


def _page(**over):
    p = {"publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": "blog", "external_url": URL,
         "publish_status": "unpublished", "unpublished_by": pb.RETIREMENT_ACTOR, "flag_id": FLAG_ID,
         "tour_id": str(uuid.uuid4()), "drafted_at": NOW, "put_back": None, "tour_name": "Misty Ridge Walk",
         "request_id": REQUEST_ID, "angle_name": "Walking the misty ridge"}
    p.update(over)
    return p


def _row(**over):
    r = {"publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": "blog", "status": "unpublished",
         "external_id": "6", "external_url": URL, "published_at": NOW, "unpublished_by": pb.RETIREMENT_ACTOR,
         "idempotency_key": "k", "stale": None, "content_text": "A walk on the ridge.", "piece_status": "approved",
         "request_id": REQUEST_ID, "piece_channel": "blog", "superseded": False, "other_live": False}
    r.update(over)
    return r


class _World:
    def __init__(self, page=None, row=None, integ=None, claim="ok", reread=None):
        self.page = _page() if page is None else page
        self.row = _row() if row is None else row
        self.integ = {"secret_key": "tenant/wp"} if integ is None else integ
        self.claim = claim
        self.reread = reread
        self.executed = []
        self.finished = False
        self.row_reads = 0

    def conn(self):
        world = self

        class _Conn:
            async def fetchrow(self, sql, *args):
                if sql == pb.DRAFTED_PAGE_SQL:
                    assert args[0] == TENANT_ID and args[2] == str(PUBLISH_ID)
                    return world.page or None
                if sql == ROW_SQL:
                    assert args[1] == TENANT_ID
                    world.row_reads += 1
                    return world.row if world.row_reads == 1 else world.reread
                if sql == pub_mod._PUBLISH_INTEGRATION_SQL:
                    return world.integ or None
                if sql == pb.FINISH_SQL:
                    world.finished = True
                    return {"publish_id": str(PUBLISH_ID), "status": "published", "external_id": "6",
                            "external_url": URL, "published_at": NOW}
                raise AssertionError(sql)

            async def fetchval(self, sql, *args):
                assert sql == pb.CLAIM_SQL and args[1] == TENANT_ID and args[2] == pb.RETIREMENT_ACTOR
                if world.claim == "unique":
                    raise asyncpg.UniqueViolationError("dup")
                return str(PUBLISH_ID) if world.claim == "ok" else None

            async def fetch(self, sql, *args):
                return []          # request_tours: no tour, so ensure_served has nothing to check

            async def execute(self, sql, *args):
                world.executed.append((sql, args))
                return "UPDATE 1"

            def transaction(self):
                @asynccontextmanager
                async def _tx():
                    yield
                return _tx()

        return _Conn()


def _setup(monkeypatch, world, *, remote=(pb.PUBLISHED, "publish")):
    @asynccontextmanager
    async def scoped(request, tenant_id, flag, fail_closed=True):
        assert flag == pub_mod.PUT_BACK_FLAG and tenant_id == TENANT_ID
        yield world.conn(), True

    @asynccontextmanager
    async def acquire():
        yield world.conn()

    pool = MagicMock()
    pool.acquire = acquire
    request = MagicMock()
    request.app.state.pool = pool
    monkeypatch.setattr(pub_mod, "acquire_scoped_conn", scoped)
    monkeypatch.setattr(pub_mod, "_subject_gate", AsyncMock())
    monkeypatch.setattr(pub_mod, "_get_secret", lambda key: dict(WP_CREDS))
    remote_mock = AsyncMock(side_effect=remote) if isinstance(remote, Exception) else AsyncMock(return_value=remote)
    monkeypatch.setattr(pub_mod, "put_back_remote", remote_mock)
    audit = AsyncMock()
    monkeypatch.setattr(pub_mod, "write_audit_log", audit)
    return request, remote_mock, audit


def _sqls(world):
    return [s for s, _ in world.executed]


@pytest.mark.asyncio
class TestRoute:
    async def test_puts_a_drafted_page_back_online(self, monkeypatch):
        world = _World()
        request, remote, audit = _setup(monkeypatch, world)
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["outcome"] == "published" and out["status"] == "published" and not out["replayed"]
        assert out["message"] == "The page is online again."
        remote.assert_awaited_once_with(WP_CREDS, "6")
        assert world.finished and pb.REVERT_SQL not in _sqls(world)
        note_args = next(a for s, a in world.executed if s == pb.NOTE_SQL)
        assert note_args[0] == FLAG_ID and note_args[1] == TENANT_ID
        assert json.loads(note_args[2])["outcome"] == "published"
        kw = audit.call_args.kwargs
        assert kw["action"] == "content_piece.put_back_online" and kw["actor"] == f"tenant:{TENANT_ID}"
        assert kw["resource_id"] == PIECE_ID and kw["details"]["outcome"] == "published"
        pub_mod._subject_gate.assert_awaited_once()

    async def test_a_page_already_live_on_wordpress_is_recorded(self, monkeypatch):
        world = _World()
        request, _, _ = _setup(monkeypatch, world, remote=(pb.ALREADY_ONLINE, "publish"))
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["outcome"] == "already_online" and world.finished

    async def test_a_page_changed_on_wordpress_is_left_as_is(self, monkeypatch):
        world = _World()
        request, _, audit = _setup(monkeypatch, world, remote=(pb.CHANGED_ON_WORDPRESS, "private"))
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["outcome"] == "changed_on_wordpress" and out["status"] == "unpublished"
        assert "private" in out["message"] and "left as is" in out["message"]
        assert not world.finished and pb.REVERT_SQL in _sqls(world)
        assert audit.call_args.kwargs["details"]["wordpress_status"] == "private"

    async def test_a_wordpress_error_reverts_the_claim_and_answers_502(self, monkeypatch):
        world = _World()
        request, _, audit = _setup(monkeypatch, world, remote=RuntimeError("host unreachable"))
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 502 and "still in draft" in exc.value.detail
        revert = next(a for s, a in world.executed if s == pb.REVERT_SQL)
        assert revert[3] == "host unreachable"
        assert pb.NOTE_SQL not in _sqls(world) and not audit.called

    @pytest.mark.parametrize("page", [{}, None])
    async def test_a_page_not_drafted_by_the_retirement_or_another_tenants_is_404(self, monkeypatch, page):
        world = _World(page=page)
        world.page = None
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 404 and not remote.called

    async def test_a_double_click_after_success_replays_without_a_call(self, monkeypatch):
        world = _World(page=_page(put_back={"outcome": "published"}),
                       row=_row(status="published", unpublished_by=None))
        request, remote, audit = _setup(monkeypatch, world)
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["outcome"] == "published" and out["replayed"] and not remote.called and not audit.called

    async def test_a_repeat_click_on_a_page_left_as_is_replays(self, monkeypatch):
        world = _World(page=_page(put_back=json.dumps({"outcome": "changed_on_wordpress",
                                                       "wordpress_status": "trash"})))
        request, remote, _ = _setup(monkeypatch, world)
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["outcome"] == "changed_on_wordpress" and out["replayed"] and "trash" in out["message"]
        assert not remote.called

    async def test_a_concurrent_click_gets_409(self, monkeypatch):
        world = _World(row=_row(status="requested", stale=False))
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and exc.value.detail == pub_mod.PUT_BACK_IN_PROGRESS
        assert not remote.called

    async def test_a_click_that_loses_the_claim_race_answers_from_the_winner(self, monkeypatch):
        world = _World(claim="none", reread=_row(status="published", unpublished_by=None))
        request, remote, _ = _setup(monkeypatch, world)
        out = await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert out["replayed"] and out["outcome"] == "already_online" and not remote.called

    async def test_a_click_that_loses_to_a_running_claim_gets_409(self, monkeypatch):
        world = _World(claim="none", reread=_row(status="requested", stale=False))
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and not remote.called

    async def test_the_same_revision_live_as_another_post_is_409(self, monkeypatch):
        world = _World(claim="unique")
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and exc.value.detail == pub_mod.PUT_BACK_OTHER_LIVE
        assert not remote.called

    @pytest.mark.parametrize("over,detail", [
        ({"superseded": True}, pub_mod.PUT_BACK_NEWER),
        ({"other_live": True}, pub_mod.PUT_BACK_OTHER_LIVE),
        ({"unpublished_by": "tenant:someone"}, pub_mod.PUT_BACK_NOT_DRAFTED),
        ({"status": "failed"}, pub_mod.PUT_BACK_NOT_DRAFTED),
    ])
    async def test_refusals_before_any_call(self, monkeypatch, over, detail):
        world = _World(row=_row(**over))
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and exc.value.detail == detail and not remote.called

    async def test_a_tour_off_sale_again_is_refused(self, monkeypatch):
        world = _World()
        request, remote, _ = _setup(monkeypatch, world)
        monkeypatch.setattr(pub_mod._tl, "ensure_served",
                            AsyncMock(side_effect=TourNotServedError(ServedState("retired"), "put this page back")))
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "tour_discontinued"
        assert not remote.called

    async def test_the_subject_gate_holds_it(self, monkeypatch):
        world = _World()
        request, remote, _ = _setup(monkeypatch, world)
        monkeypatch.setattr(pub_mod, "_subject_gate", AsyncMock(side_effect=HTTPException(409, "change it")))
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 409 and not remote.called

    async def test_not_connected_is_422(self, monkeypatch):
        world = _World(integ={})
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 422 and not remote.called

    async def test_another_site_is_422(self, monkeypatch):
        world = _World(row=_row(external_url="https://old-site.example/misty/"))
        request, remote, _ = _setup(monkeypatch, world)
        with pytest.raises(HTTPException) as exc:
            await pub_mod.put_back(PUBLISH_ID, request, tenant=TENANT)
        assert exc.value.status_code == 422 and "old-site.example" in exc.value.detail and not remote.called


@pytest.mark.asyncio
async def test_list_shows_only_unsettled_drafts_of_served_tours(monkeypatch):
    served_req, retired_req = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        _page(publish_id="p-served", request_id=served_req),
        _page(publish_id="p-retired", request_id=retired_req),
        _page(publish_id="p-settled", put_back={"outcome": "changed_on_wordpress"}),
        _page(publish_id="p-back", publish_status="published", unpublished_by=None),
    ]

    class _Conn:
        async def fetch(self, sql, *args):
            assert sql == pb.DRAFTED_PAGES_SQL and args[0] == TENANT_ID
            return rows

    @asynccontextmanager
    async def acquire():
        yield _Conn()

    request = MagicMock()
    request.app.state.pool.acquire = acquire
    monkeypatch.setattr(pub_mod._tl, "request_served_states", AsyncMock(
        return_value={served_req: ServedState("served"), retired_req: ServedState("retired")}))
    out = await pub_mod.list_put_back(request, tenant=TENANT)
    assert [d["publish_id"] for d in out["data"]] == ["p-served"] and out["total"] == 1
    assert out["data"][0]["title"] == "Walking the misty ridge" and out["data"][0]["tour_name"] == "Misty Ridge Walk"


def test_put_back_uses_its_own_flag_only():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pub_mod))
    users = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(n, ast.Name) and n.id == "PUT_BACK_FLAG" for n in ast.walk(fn))}
    assert users == {"put_back"}


def test_every_put_back_statement_names_the_tenant():
    for sql in (pb.DRAFTED_PAGES_SQL, pb.DRAFTED_PAGE_SQL, pb.CLAIM_SQL, pb.REVERT_SQL, pb.FINISH_SQL, pb.NOTE_SQL):
        assert "tenant_id = $" in sql
    assert pb.ROW_TENANT_COND in ROW_SQL


# ── the restore notice ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_restore_mail_lists_each_drafted_page_with_the_action():
    n = {"event": "restored", "tour_name": "Misty Ridge Walk",
         "summary": {"back_on": "10 November 2026", "pages_drafted": [{"publish_id": "p1", "url": URL}]}}
    msg = notice_mail.render([n], to=("owner@lantern-bay.example",), agency="Lantern Bay Travel")
    assert URL in msg.text and "Put back online" in msg.text and "stay in draft" in msg.text
    assert "flag" not in msg.text.lower()
    plain = notice_mail.render([{**n, "summary": {"back_on": "10 November 2026"}}], to=("o@x.example",), agency="A")
    assert "Put back online" not in plain.text


def test_the_bell_row_carries_the_pages():
    row = {"id": 7, "event_type": "tour.pages_to_put_back", "entity_type": "tour", "entity_id": "t1",
           "payload": json.dumps({"tour_name": "Misty Ridge Walk", "pages": [{"publish_id": "p1", "url": URL},
                                                                          {"url": "no id"}]}),
           "is_read": False, "created_at": NOW}
    item = notif_mod._item(row)
    assert item["title"] == "Pages to put back online" and item["pages"] == [{"publish_id": "p1", "url": URL}]
    assert "1 of your WordPress page moved to draft" in item["message"] and "stays in draft" in item["message"]
    other = notif_mod._item({**row, "event_type": "tour.reinstated"})
    assert other["pages"] == []


def test_the_activity_feed_names_the_click():
    assert tenant_account.TENANT_VISIBLE_ACTIONS["content_piece.put_back_online"][1] == "Put a page back online"
    assert tenant_account.detail_text("content_piece.put_back_online",
                                      {"channel": "blog", "outcome": "changed_on_wordpress"}) == "WordPress · left as is"
