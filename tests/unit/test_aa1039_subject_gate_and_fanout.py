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
    tripplanner_hosts=frozenset({"planner.example"}),
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


@pytest.mark.parametrize("text,url,anchor", [
    ("Book now (https://othertravel.example/booking)", "https://othertravel.example/booking", ""),
    ("Book: 'https://othertravel.example/booking'", "https://othertravel.example/booking", ""),
    ('Book: "https://othertravel.example/booking".', "https://othertravel.example/booking", ""),
    ("<a href=https://othertravel.example/booking>Seats</a>", "https://othertravel.example/booking", "Seats"),
    ("<a class=x href = 'https://othertravel.example/booking'>Seats</a>", "https://othertravel.example/booking",
     "Seats"),
    ("Book at othertravel.example/booking today.", "https://othertravel.example/booking", ""),
    ("Or www.othertravel.example/deals!", "https://www.othertravel.example/deals", ""),
])
def test_links_in_parentheses_quotes_unquoted_href_and_without_a_scheme_are_read(text, url, anchor):
    links = sg.extract_links(text)
    assert [link.url for link in links] == [url]
    if anchor:
        assert links[0].text == anchor
    res = sg.evaluate(text, goal="conversion", cta=None, subject_ok=True, targets=TARGETS)
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]


@pytest.mark.parametrize("text", [
    "Write to desk@othertravel.example/booking",       # an e-mail-like token, not a link
    "We walked 3.5km/day near Lake.Town",              # no host with a path
    "See the map at https://maps.example/view",        # a link, not a sales one
])
def test_no_false_sales_link_from_plain_prose(text):
    assert sg.evaluate(text, goal="conversion", cta=None, subject_ok=True, targets=TARGETS).ok


def test_a_markdown_link_counts_once_with_its_anchor_text():
    links = sg.extract_links("[Book](https://othertravel.example/x) (https://othertravel.example/x)")
    assert [(link.url, link.text) for link in links] == [("https://othertravel.example/x", "Book")]


@pytest.mark.parametrize("path", ["-" * 36, "a" * 36, "1b4e28ba2fa111d2883f0016d3cca427abcd",
                                  "1b4e28ba-2fa1-11d2-883f-0016d3cca42z"])
def test_a_malformed_trip_page_id_is_not_a_trip_page_and_is_refused(path):
    url = f"https://go-dev.lumiguides.it.com/trip/{path}"
    assert sg.trip_page_tour_id(url, TARGETS) is None
    res = sg.evaluate(f"Book it: {url}", goal="conversion", cta=None, subject_ok=True, targets=TARGETS)
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]


def test_a_trip_page_id_is_normalised():
    assert sg.trip_page_tour_id(f"https://go-dev.lumiguides.it.com/trip/{TOUR.upper()}/", TARGETS) == TOUR


@pytest.mark.parametrize("url,text,sales", [
    ("https://museum.example/history", "history of the hall", False),
    ("https://othertravel.example/tours/mekong-days", "", True),      # a product listing path
    ("https://othertravel.example/x", "Book your seat", True),         # the anchor reads like booking
    ("https://othertravel.example/booking", "", True),                 # the path reads like booking
    ("https://www.viator.com/Hanoi/d351", "Hanoi tours", True),        # a product word to a commercial host
    ("https://social.example/agency", "follow us", False),
    ("https://hanoimuseum.example/visit/tours", "guided tours of the museum", False),   # informational
    ("https://agency.example/blog/best-trips-in-vietnam", "our top trips", False),       # an article
    ("https://othertravel.example/mekong-tour", "", False),            # a product word alone
])
def test_sales_link_detection(url, text, sales):
    assert sg.is_sales_link(sg.Link(url, text)) is sales


@pytest.mark.parametrize("url,ok", [
    ("https://www.adventure.asia/tours/lake", True),
    ("https://planner.example/plan?x=1", True),
    (f"https://go-dev.lumiguides.it.com/trip/{TOUR}", True),
    ("https://go-dev.lumiguides.it.com/trip/2c1c1c1c-2fa1-11d2-883f-0016d3cca427", False),   # not served
    ("https://agency.example/tours/lake-walk/", True),                                        # tenant page, AA tour
    ("https://agency.example/tours/own-walk", False),
    ("https://adventure.asia.evil.example/tour", False),
])
def test_allowed_sales_targets(url, ok):
    assert sg.allowed_sales_target(url, TARGETS) is ok


# Forms Python-Markdown (extensions extra + nl2br, the publisher's render) and browsers turn into a working link: each
# one is read with its anchor text (or the words before it) and refused when it books off the catalogue.
@pytest.mark.parametrize("text", [
    '[Book now](https://competitor.example/p/42 "Hanoi")',              # a title
    "[Book now](<https://competitor.example/p/42>)",                    # angle brackets
    "[Book now][1]\n\n[1]: https://competitor.example/p/42",            # a reference link
    "[Book now](//competitor.example/p/42)",                            # protocol-relative
    '<a href="//competitor.example/p/42">Book now</a>',
    '<a href="https&#58;//competitor.example/p/42">Book now</a>',       # an entity in the scheme
    '<a href="&#x68;ttps://competitor.example/p/42">Book now</a>',
    "[Book now](HTTPS://competitor.example/p/42)",                      # upper-case scheme
    '<a href="https:\\\\competitor.example/p/42">Book now</a>',            # backslashes
    '<a href="ht\ntps://competitor.example/p/42">Book now</a>',          # a newline inside the scheme
    "<a href='https://competitor.example/p/42'>Book now",              # never closed
    "Book your cruise here: https://competitor.example/p/42",           # the words before a bare URL
    "<!-- https://competitor.example/booking -->",                      # hidden on WordPress, a link on Facebook
])
def test_every_published_link_form_is_read_and_refused(text):
    links = sg.extract_links(text)
    assert any(link.url.startswith("https://competitor.example/") for link in links), links
    res = sg.evaluate(text, goal="promotion", cta=None, subject_ok=True, targets=TARGETS)
    assert not res.ok and res.codes == [sg.OFF_CATALOG_LINK]
    # without an AA subject and with a brand goal, the same link still counts as a sales link
    res = sg.evaluate(text, goal="trust_building", cta=None, subject_ok=False, targets=TARGETS)
    assert set(res.codes) == {sg.OFF_CATALOG_LINK, sg.NO_AA_SUBJECT}


@pytest.mark.parametrize("text", [
    "[guided tours of the museum](https://hanoimuseum.example/visit/tours)",
    "[our ten best trips](/blog/top-10-trips)",                         # relative: the tenant's own site
    "[top trips](https://agency.example/blog/top-10-trips)",
    '<a href="mailto:desk@othertravel.example">Book a call</a>',         # not a web link
    "Book your tour with us today. More about the museum: https://museum.example/history",
    "Plan your trip on https://tripplanner.lumiguides.it.com/trips/abc",  # the Dev TripPlanner site
])
def test_informational_and_own_links_pass(text):
    res = sg.evaluate(text, goal="promotion", cta=None, subject_ok=True,
                      targets=sg.AllowedTargets(TARGETS.aa_hosts, TARGETS.trip_page_hosts, sg.tripplanner_hosts()))
    assert res.ok, res.detail


def test_the_dev_tripplanner_domain_is_an_allowed_sales_target(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_SITE_HOSTS", raising=False)
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_URL", raising=False)
    assert sg.DEFAULT_TRIPPLANNER_HOSTS == ("tripplanner.lumiguides.it.com",)
    hosts = sg.tripplanner_hosts()
    assert hosts == {"tripplanner.lumiguides.it.com"}
    # The retired Vercel host is no sales target any more (AA-1038: anyone can claim a deleted *.vercel.app name).
    retired = "https://aa-tripplanner.vercel.app/plan"
    assert not sg.allowed_sales_target(retired, sg.AllowedTargets(tripplanner_hosts=hosts))
    targets = sg.AllowedTargets(tripplanner_hosts=hosts)
    res = sg.evaluate("[Book your trip](https://tripplanner.lumiguides.it.com/trips/x)", goal="conversion",
                      cta=None, subject_ok=True, targets=targets)
    assert res.ok


@pytest.mark.parametrize("href,url", [
    ("//x.example/a", "https://x.example/a"),
    ("HTTP://x.example/a", "http://x.example/a"),
    ("https:x.example/a", "https://x.example/a"),
    ("www.x.example", "https://www.x.example"),
    ("x.example/a?b=1", "https://x.example/a?b=1"),
    ("/blog/a", None), ("#top", None), ("tel:+1", None), ("javascript:alert(1)", None), ("", None), (None, None),
])
def test_resolve_href_like_a_browser(href, url):
    assert sg.resolve_href(href) == url


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
async def test_publish_gate_passes_and_a_read_failure_fails_closed_with_503():
    from api.routers import v1_publish
    with patch.object(sg, "run", AsyncMock(return_value=sg.GateResult(True))):
        assert await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", "p",
                                              "blog") is None
    with patch.object(sg, "run", AsyncMock(side_effect=RuntimeError("db down"))):
        with pytest.raises(HTTPException) as exc:
            await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", "p", "blog")
    assert exc.value.status_code == 503 and exc.value.detail == v1_publish.SUBJECT_GATE_UNAVAILABLE
    assert exc.value.headers["X-Error-Code"] == "content_check_unavailable"
    assert "try" in exc.value.detail.lower() and "flag" not in exc.value.detail.lower()
    with patch.object(sg, "open_messages", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await v1_publish._pending_needs_change(_pool(), "t", ["p"]) == {}


@pytest.mark.real_subject_gate
@pytest.mark.asyncio
async def test_publish_gate_skips_a_repeat_of_the_same_revision():
    """A publish of the same text already live or in flight is answered by claim() (replay / in progress), not held:
    the text passed the gate then, or went live before the gate existed."""
    from api.routers import v1_publish
    pool = _pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchval = AsyncMock(return_value=True)
    with patch.object(sg, "run", AsyncMock(return_value=sg.GateResult(False, [sg.OFF_CATALOG_LINK], "x"))) as run:
        assert await v1_publish._subject_gate(pool, {"request_id": "r", "content_text": "x"}, "t", "p", "blog",
                                              key="p:blog:abc") is None
    run.assert_not_awaited()
    conn.fetchval = AsyncMock(return_value=False)
    with patch.object(sg, "run", AsyncMock(return_value=sg.GateResult(False, [sg.OFF_CATALOG_LINK], "x"))):
        with pytest.raises(HTTPException) as exc:
            await v1_publish._subject_gate(pool, {"request_id": "r", "content_text": "x"}, "t", "p", "blog",
                                           key="p:blog:abc")
    assert exc.value.status_code == 409


@pytest.mark.real_subject_gate
@pytest.mark.asyncio
async def test_publish_gate_fails_closed_on_a_malformed_trip_page_link_db_error():
    """The served-state read rejecting a value ($1::uuid[]) must not let the piece through."""
    from api.routers import v1_publish
    with patch.object(sg, "check", AsyncMock(side_effect=ValueError("badly formed hexadecimal UUID string"))), \
            patch.object(sg, "released", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await v1_publish._subject_gate(_pool(), {"request_id": "r", "content_text": "x"}, "t", "p", "blog")
    assert exc.value.status_code == 503


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
        "stop_date": "3 November 2026",
        "pages": [{"url": "https://agency.example/lake", "draft_on": "10 November 2026"}],
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


def test_the_digest_takes_digest_notices_and_immediate_ones_left_unmailed_an_hour():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    assert trj._in_digest({"delivery": "digest", "created_at": now}, now=now)
    assert not trj._in_digest({"delivery": "immediate", "created_at": now - timedelta(minutes=20)}, now=now)
    assert trj._in_digest({"delivery": "immediate", "created_at": now - timedelta(hours=2)}, now=now)


@pytest.mark.asyncio
async def test_a_fanout_replay_enqueues_the_unmailed_immediate_notice_again():
    from shared.jobs import queue
    from shared.jobs.registry import JobContext
    job = queue.Job(id=str(uuid.uuid4()), kind=trj.FANOUT_KIND, payload={"event_key": "ev-1"}, status="running",
                    attempt=2, max_attempts=5, cost_usd=0.0, progress={})
    replay = {"status": "done", "event": "retired", "flags_created": 0, "new_immediate_notices": 0,
              "immediate_notices": ["n-1"]}
    with patch.object(trj.fanout, "fan_out", AsyncMock(return_value=replay)), \
            patch.object(trj, "enqueue", AsyncMock(return_value=("mail-1", False))) as enq:
        out = await trj.run_fanout(JobContext(MagicMock(), job, {}))
    assert out["mail_jobs"] == 1 and "immediate_notices" not in out
    assert enq.await_args.kwargs["idempotency_key"] == "tour_notice_email:n-1"


@pytest.mark.asyncio
async def test_a_fanout_before_the_tables_exist_fails_retryable_and_is_requeued_by_name():
    """Code deployed before CIS 360: the job must not end done (the sweep would never take the event again). It fails
    with a retryable error whose text the sweep's re-queue matches once the tables exist."""
    from shared.jobs import queue, worker
    from shared.jobs.registry import JobContext
    job = queue.Job(id=str(uuid.uuid4()), kind=trj.FANOUT_KIND, payload={"event_key": "ev-1"}, status="running",
                    attempt=1, max_attempts=5, cost_usd=0.0, progress={})
    with patch.object(trj.fanout, "fan_out", AsyncMock(side_effect=trj.fanout.SchemaMissing())):
        with pytest.raises(trj.fanout.SchemaMissing) as exc:
            await trj.run_fanout(JobContext(MagicMock(), job, {}))
    error, retryable, _ = worker.failure_outcome(exc.value)
    assert retryable and "fanout_schema_missing" in error and f"CIS {trj.fanout.MIGRATION}" in error
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=3)
    assert await trj.requeue_schema_missing(pool) == 3
    assert pool.fetchval.await_args.args[1] == "fanout_schema_missing"


def test_the_admin_503_names_the_migration_from_one_constant():
    from api.routers import admin_tenant_compliance as atc
    assert f"migration {trj.fanout.MIGRATION} " in atc.UNAVAILABLE
    assert trj.fanout.MIGRATION == "360"
