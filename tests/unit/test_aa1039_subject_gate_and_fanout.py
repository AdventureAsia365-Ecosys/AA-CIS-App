"""AA-1039 R6 slice 2 (root ADR 0019 d20 / d21), no database.

- The AA-subject gate rule (services/acp_shared/subject_gate.evaluate): which links are sales links, which targets
  are allowed, brand content, the tenant's sentence (content words, never "flag" / "QA" / a code).
- The publish route: an open gate verdict answers 409 with that sentence before any channel call; a gate read
  failure never turns into a 500.
- The fan-out jobs: only retired / restored events enqueue a fan-out; the mail render; the WordPress draft call.
Invented names and hosts only.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.acp_shared import subject_gate as sg
from services.acp_s4_blog.cms.wordpress import WordPressAdapter
from services.jobs import tour_retirement_jobs as trj
from services.tour_lifecycle import notice_mail
from tests.unit.safe_http_fakes import TARGET, response

TOUR = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
TARGETS = sg.AllowedTargets(
    aa_hosts=frozenset({"adventure.asia"}), trip_page_hosts=frozenset({"go-dev.lumiguides.it.com"}),
    tripplanner_hosts=frozenset({"aa-tripplanner.vercel.app"}),
    tenant_pages=frozenset({"agency.example/tours/lake-walk"}), served_trip_ids=frozenset({TOUR}))


# ── the rule ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_links_are_read_from_markdown_html_and_bare_urls_and_the_cta():
    text = ("See [the old town](https://museum.example/old-town). <a href=\"https://x.example/a\">Book here</a> "
            "and https://bare.example/path. Or https://museum.example/old-town again.")
    links = sg.extract_links(text, cta="Book now: https://cta.example/go")
    by = {link.url: link for link in links}
    assert set(by) == {"https://museum.example/old-town", "https://x.example/a", "https://bare.example/path",
                       "https://cta.example/go"}
    assert by["https://museum.example/old-town"].text == "the old town"
    assert by["https://x.example/a"].text == "Book here" and by["https://cta.example/go"].cta


@pytest.mark.parametrize("url,text,sales", [
    ("https://museum.example/history", "history of the hall", False),
    ("https://othertravel.example/mekong-tour", "", True),            # the path reads like a tour
    ("https://othertravel.example/x", "Book your seat", True),         # the anchor reads like booking
    ("https://social.example/agency", "follow us", False),
])
def test_sales_link_detection(url, text, sales):
    assert sg.is_sales_link(sg.Link(url, text)) is sales


@pytest.mark.parametrize("url,ok", [
    ("https://www.adventure.asia/tours/lake", True),
    ("https://aa-tripplanner.vercel.app/plan?x=1", True),
    (f"https://go-dev.lumiguides.it.com/trip/{TOUR}", True),
    ("https://go-dev.lumiguides.it.com/trip/2c1c1c1c-2fa1-11d2-883f-0016d3cca427", False),   # not served
    ("https://agency.example/tours/lake-walk/", True),                                        # tenant page, AA tour
    ("https://agency.example/tours/own-walk", False),
    ("https://adventure.asia.evil.example/tour", False),
])
def test_allowed_sales_targets(url, ok):
    assert sg.allowed_sales_target(url, TARGETS) is ok


def test_off_catalogue_link_holds_with_a_content_style_sentence():
    res = sg.evaluate("Top five river days. [Book](https://othertravel.example/river-tour)", goal="conversion",
                      cta=None, subject_ok=True, targets=TARGETS)
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]
    assert "othertravel.example" in res.tenant_message
    for word in ("flag", " qa", "off_catalog", "subject_gate", "compliance"):
        assert word not in res.tenant_message.lower()


def test_sales_goal_without_a_subject_holds_and_brand_content_passes():
    res = sg.evaluate("Meet our guides.", goal="promotion", cta=None, subject_ok=False, targets=TARGETS)
    assert res.codes == [sg.NO_AA_SUBJECT]
    res = sg.evaluate("Meet our guides. https://museum.example/hall", goal="trust_building", cta=None,
                      subject_ok=False, targets=TARGETS)
    assert res.ok and res.detail["brand_content"]
    res = sg.evaluate("Meet our guides.", goal="trust_building", cta="Book at https://othertravel.example/trip",
                      subject_ok=False, targets=TARGETS)
    assert set(res.codes) == {sg.NO_AA_SUBJECT, sg.OFF_CATALOG_LINK}


def test_a_subject_with_an_allowed_link_passes():
    res = sg.evaluate(f"Book it: https://go-dev.lumiguides.it.com/trip/{TOUR}", goal="conversion", cta=None,
                      subject_ok=True, targets=TARGETS)
    assert res.ok and res.codes == [] and res.tenant_message == ""


# ── the publish route ──────────────────────────────────────────────────────────────────────────────────────────────

def _pool():
    conn = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.real_subject_gate
@pytest.mark.asyncio
async def test_publish_gate_refuses_with_409_and_the_sentence():
    from api.routers import v1_publish
    held = sg.GateResult(False, [sg.OFF_CATALOG_LINK], "This piece links to a tour that isn't in the catalogue.")
    with patch.object(sg, "run", AsyncMock(return_value=held)) as run:
        with pytest.raises(HTTPException) as exc:
            await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", uuid.uuid4(),
                                           "blog")
    assert exc.value.status_code == 409 and exc.value.detail == held.tenant_message
    assert exc.value.headers["X-Error-Code"] == "content_needs_change"
    assert run.await_args.kwargs["stage"] == "publish"


@pytest.mark.real_subject_gate
@pytest.mark.asyncio
async def test_publish_gate_passes_and_a_read_failure_is_not_a_500():
    from api.routers import v1_publish
    with patch.object(sg, "run", AsyncMock(return_value=sg.GateResult(True))):
        assert await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", "p",
                                              "blog") is None
    with patch.object(sg, "run", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", "p",
                                              "blog") is None
    with patch.object(sg, "open_messages", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await v1_publish._pending_needs_change(_pool(), "t", ["p"]) == {}


# ── the fan-out ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_retired_and_restored_events_enqueue_a_fanout():
    with patch.object(trj, "enqueue", AsyncMock(return_value=("j", True))) as enq:
        n = await trj.enqueue_for_events(MagicMock(), [
            {"event": "retired", "idempotency_key": "k1"}, {"event": "paused", "idempotency_key": "k2"},
            {"event": "superseded", "idempotency_key": "k3"}, {"event": "restored", "idempotency_key": "k4"},
            {"event": "retired", "idempotency_key": None}])
    assert n == 2
    assert [c.kwargs["idempotency_key"] for c in enq.await_args_list] == [
        "tour_retirement_fanout:k1", "tour_retirement_fanout:k4"]


def test_retired_mail_names_pages_posts_and_dates_in_content_words():
    notice = {"event": "retired", "tour_name": "Lake Walk Weekend", "summary": {
        "stop_date": "3 November 2026", "pages": [{"url": "https://agency.example/lake", "draft_on": "10 November 2026"}],
        "posts": [{"url": "https://social.example/p/1", "channel": "facebook"}], "publishes_cancelled": 1,
        "replacement": "River Valley Days"}}
    msg = notice_mail.render([notice], to=("owner@agency.example",), agency="Invented Agency")
    assert msg.subject == "Lake Walk Weekend is no longer offered by Adventure Asia"
    for part in ("3 November 2026", "https://agency.example/lake", "10 November 2026", "https://social.example/p/1",
                 "River Valley Days", "Invented Agency"):
        assert part in msg.text
    for word in ("flag", " qa", "compliance", "tour_retired"):
        assert word not in msg.text.lower() and word not in msg.html.lower()


def test_digest_subject_and_restored_section():
    notices = [{"event": "restored", "tour_name": "Lake Walk Weekend", "summary": {"back_on": "5 November 2026"}},
               {"event": "retired", "tour_name": "Hill Trail", "summary": {"stop_date": "4 November 2026"}}]
    msg = notice_mail.render(notices, to=("a@agency.example",), agency="Invented Agency")
    assert msg.subject == "Adventure Asia tour updates for Invented Agency (2)"
    assert "back on sale" in msg.text and "Hill Trail" in msg.text


def test_partner_pool_from_the_issuer(monkeypatch):
    monkeypatch.setattr("api.core.partner_identity.partner_issuer",
                        lambda: "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_Abc123")
    ref = notice_mail.partner_pool()
    assert ref.region == "us-west-1" and ref.pool_id == "us-west-1_Abc123"
    monkeypatch.setattr("api.core.partner_identity.partner_issuer", lambda: "https://login.example/x")
    assert notice_mail.partner_pool() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (200, {"id": 41, "status": "draft"}, "drafted"),
    (200, {"id": 41, "status": "trash"}, "already_gone"),
    (404, {"code": "rest_post_invalid_id", "message": "Invalid post ID."}, "already_gone"),
])
async def test_wordpress_set_draft(status, body, expected):
    call = AsyncMock(return_value=response(status, "application/json", json.dumps(body)))
    with patch(TARGET, call):
        out = await WordPressAdapter("https://agency.example", "u", "p").set_draft(41)
    assert out == expected
    assert call.await_args.args[0] == "POST" and call.await_args.kwargs["json"] == {"status": "draft"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (200, {"id": 41, "status": "publish"}),
    (401, {"code": "rest_cannot_edit", "message": "no"}),
    (200, "<html>challenge</html>"),
])
async def test_wordpress_set_draft_refuses_anything_else(status, body):
    text = body if isinstance(body, str) else json.dumps(body)
    ctype = "text/html" if isinstance(body, str) else "application/json"
    with patch(TARGET, AsyncMock(return_value=response(status, ctype, text))):
        with pytest.raises(RuntimeError):
            await WordPressAdapter("https://agency.example", "u", "p").set_draft(41)


@pytest.mark.asyncio
async def test_sampler_part_runs_only_when_the_kinds_are_registered():
    from api.core import ops_metrics_sampler as s
    out = {}
    with patch.object(trj, "tick", AsyncMock(return_value={"drafts_due": 2})) as tick:
        await s._tour_retirement_part(MagicMock(), {"tour_retirement_fanout": 1}, None, out)
        assert tick.await_count == 0 and out == {}
        kinds = {trj.FANOUT_KIND: 1, trj.DRAFT_KIND: 1, trj.MAIL_KIND: 1}
        await s._tour_retirement_part(MagicMock(), kinds, None, out)
    assert out == {"tour_retirement": {"drafts_due": 2}}


@pytest.mark.asyncio
async def test_daily_digest_is_queued_once_from_its_hour():
    from datetime import datetime, timezone
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    with patch.object(trj, "enqueue", AsyncMock(return_value=("j1", True))) as enq:
        assert await trj.enqueue_daily_digest(pool, datetime(2026, 11, 3, 7, 59, tzinfo=timezone.utc)) == (None, False)
        assert await trj.enqueue_daily_digest(pool, datetime(2026, 11, 3, 8, 1, tzinfo=timezone.utc)) == ("j1", True)
    assert enq.await_args.kwargs["idempotency_key"] == "tour_notice_email:digest:2026-11-03"
