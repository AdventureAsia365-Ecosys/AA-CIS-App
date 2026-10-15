"""AA-819 follow-up (S237) — a tenant CTA that claims reviews or ratings is saved as typed, the
response carries a non-blocking `cta_warning`, and AA admins get ONE bell notification per tenant +
CTA text. F19's CTA exemption is untouched. No DB: an in-memory fake connection stands in for
asyncpg and records the shared.notifications rows."""
import datetime
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin, admin_pipeline, v1_content_writing
from services.acp_content_writing import authenticity
from services.acp_shared import cta_review_notice

TENANT = str(uuid.uuid4())
REQUEST_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()
REVIEW_CTA = "Read the five-star reviews our travellers left"


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("cta, expected", [
    ("Read the five-star reviews our travellers left", ["five-star reviews"]),
    ("Rated 4.9 on TripAdvisor", ["Rated 4.9", "TripAdvisor"]),
    ("See what our guests say", ["what our guests say"]),
    ("Read our reviews before you book", ["reviews"]),
    ("Book the 5-star trip", ["5-star"]),
    ("Check us out on Trustpilot", ["Trustpilot"]),
    ("Read our testimonials", ["testimonials"]),
    ("Loved by 2,000 travellers - ask us for dates", ["Loved by 2,000 travellers"]),
    ("★★★★★ - book your place", ["★★★★★"]),
    ("Rated 5/5 by our guests", ["Rated 5/5"]),
    # review-claim fixes (adversarial review)
    ("Rated excellent by our guests", ["Rated excellent"]),
    ("Rated #1 tour operator in Laos", ["Rated #1"]),
    ("Join the travellers who rated us 5 stars", ["who rated us", "5 stars"]),
    ("Award-winning: 98% of our guests recommend us", ["98% of our guests recommend"]),
    ("Check our ratings", ["ratings"]),
])
def test_cta_review_claims_true_positives(cta, expected):
    assert authenticity.cta_review_claims(cta) == expected


@pytest.mark.parametrize("cta", [
    "Talk to an Atlas & Hearth travel designer",
    "Get the full itinerary",
    "Book your trek",
    "Ask us for the full itinerary and dates",
    "Review the full itinerary",          # 'review' as a verb is not a review claim
    "Sleep under the stars in the Gobi",
    "Stay in a five-star hotel in Hanoi",  # a hotel class, not a rating of the brand
    "Graded 3/5 for difficulty - book your place",
    "Rated trips for families",           # 'rated' as an adjective, no claim
    "Book now - rated PG for adventure",
    "Get your itinerary reviewed by a local expert",  # a service, not a review claim
    "Check our trek difficulty ratings",  # a trek grade
    "See our rates and dates",
    "",
    None,
])
def test_cta_review_claims_true_negatives(cta):
    assert authenticity.cta_review_claims(cta) == []


def test_f19_still_exempts_the_cta_line():
    """The CTA exemption in flagged() is unchanged: the review CTA is not flagged in the body."""
    body = f"A quiet week on the Mekong.\n\n{REVIEW_CTA}"
    assert authenticity.flagged(body, REVIEW_CTA) == []


def test_cta_key_ignores_case_and_spacing():
    assert cta_review_notice.cta_key("Rated 4.9  on TripAdvisor") == cta_review_notice.cta_key(
        " rated 4.9 on tripadvisor ")
    assert cta_review_notice.cta_key("Rated 4.9") != cta_review_notice.cta_key("Rated 4.8")


def test_cta_warning_shape():
    assert cta_review_notice.cta_warning("Book your trek") is None
    assert cta_review_notice.cta_warning("Rated 4.9 on TripAdvisor") == {
        "code": "cta_review_claim", "phrases": ["Rated 4.9", "TripAdvisor"]}


# ---------------------------------------------------------------- fake asyncpg

class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Enough of asyncpg for the brand save, the write route and the notification writer."""

    def __init__(self, notifications, fail_notify=False):
        self.notifications = notifications  # shared across connections, like the table
        self.fail_notify = fail_notify
        self.executed = []

    def transaction(self):
        return _Txn()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "INSERT INTO shared.notifications" in sql:
            if self.fail_notify:
                raise RuntimeError("notifications table unavailable")
            self.notifications.append({"tenant_id": args[0], "actor_type": args[1], "event_type": args[2],
                                       "entity_type": args[3], "entity_id": args[4],
                                       "payload": json.loads(args[5]), "target_roles": args[6]})
        return "OK"

    async def fetchval(self, sql, *args):
        if "count(*) FROM shared.notifications" in sql:  # the daily cap (no clock here: all rows are recent)
            event, tenant_id = args
            return sum(1 for n in self.notifications if n["event_type"] == event and n["tenant_id"] == tenant_id)
        if "FROM shared.notifications" in sql:
            event, tenant_id, key = args
            return 1 if any(n["event_type"] == event and n["tenant_id"] == tenant_id
                            and n["payload"].get("cta_key") == key for n in self.notifications) else None
        return 0  # COALESCE(MAX(version), 0) of the brand save

    async def fetchrow(self, sql, *args):
        if "FROM shared.tenants" in sql:
            return {"name": "Atlas & Hearth", "slug": "atlas-hearth"}
        return None


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _brand_rows(conn):
    return [args for sql, args in conn.executed if "INSERT INTO shared.tenant_brand_rules" in sql]


# ---------------------------------------------------------------- POST /admin/brand-identity (tenant JWT)

async def _save_brand(conn, cta, tenant_id=TENANT):
    body = admin_pipeline.BrandIdentityUpdate(brand_name="Atlas & Hearth", default_cta=cta)
    return await admin_pipeline.update_brand_identity(body, _request(_pool(conn)), tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_brand_save_with_review_cta_saves_warns_and_notifies_once():
    store = []
    conn = FakeConn(store)
    out = await _save_brand(conn, REVIEW_CTA)

    assert out["status"] == "updated"
    assert out["cta_warning"] == {"code": "cta_review_claim", "phrases": ["five-star reviews"]}
    assert _brand_rows(conn)[0][-1] == REVIEW_CTA  # saved word for word
    assert len(store) == 1
    n = store[0]
    assert n["event_type"] == "tenant.cta.review_claim"
    assert n["target_roles"] == ["admin"]
    assert n["tenant_id"] == TENANT
    assert n["entity_id"] == "atlas-hearth"
    p = n["payload"]
    assert p["tenant_slug"] == "atlas-hearth" and p["tenant_name"] == "Atlas & Hearth"
    assert p["where"] == "brand_default_cta"
    assert p["cta"] == REVIEW_CTA and p["phrases"] == ["five-star reviews"]
    assert "Atlas & Hearth (atlas-hearth)" in p["message"] and REVIEW_CTA in p["message"]

    # a re-save of the same text (any case / spacing) warns again but does not notify again
    out2 = await _save_brand(FakeConn(store), "  read the FIVE-STAR reviews our travellers left ")
    assert out2["cta_warning"]["code"] == "cta_review_claim"
    assert len(store) == 1

    # a different review CTA is a new case
    await _save_brand(FakeConn(store), "Rated 4.9 on TripAdvisor")
    assert len(store) == 2


@pytest.mark.asyncio
async def test_brand_save_with_normal_cta_has_no_warning_and_no_notification():
    store = []
    conn = FakeConn(store)
    out = await _save_brand(conn, "Talk to an Atlas & Hearth travel designer")
    assert "cta_warning" not in out
    assert store == []
    assert _brand_rows(conn)


@pytest.mark.asyncio
async def test_brand_save_survives_a_notification_failure():
    conn = FakeConn([], fail_notify=True)
    out = await _save_brand(conn, REVIEW_CTA)
    assert out["status"] == "updated"
    assert out["cta_warning"]["code"] == "cta_review_claim"
    assert _brand_rows(conn)[0][-1] == REVIEW_CTA


@pytest.mark.asyncio
async def test_aa_internal_brand_gets_the_warning_but_no_notification():
    store = []
    out = await _save_brand(FakeConn(store), REVIEW_CTA, tenant_id=admin_pipeline._AA_INTERNAL_TENANT_ID)
    assert out["cta_warning"]["code"] == "cta_review_claim"
    assert store == []


# ---------------------------------------------------------------- POST /v1/content-writing/.../write

def _started(cta):
    return {
        "piece": {"piece_id": str(PIECE_ID), "status": "processing", "content_text": "",
                  "angle_gate_request_id": str(REQUEST_ID)},
        "context": {"atom_text": "x", "cta": cta},
    }


async def _write(conn, typed_cta, used_cta):
    body = v1_content_writing.WriteBody(cta=typed_cta)
    with patch.object(v1_content_writing.service, "start_write", new=AsyncMock(return_value=_started(used_cta))), \
         patch("shared.jobs.registry.enqueue", new=AsyncMock(return_value=("job-1", True))), \
         patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
        return await v1_content_writing.write(REQUEST_ID, body, _request(_pool(conn)), tenant={"sub": TENANT})


@pytest.mark.asyncio
async def test_write_with_typed_review_cta_warns_and_notifies_once():
    store = []
    out = await _write(FakeConn(store), REVIEW_CTA, REVIEW_CTA)
    assert out["ready_state"] == "in_progress"  # the write still starts
    assert out["cta_warning"] == {"code": "cta_review_claim", "phrases": ["five-star reviews"]}
    assert len(store) == 1 and store[0]["payload"]["where"] == "write_cta"

    await _write(FakeConn(store), REVIEW_CTA, REVIEW_CTA)
    assert len(store) == 1


@pytest.mark.asyncio
async def test_write_ignores_a_typed_cta_the_request_did_not_use():
    store = []
    out = await _write(FakeConn(store), REVIEW_CTA, "Ask us for dates")
    assert "cta_warning" not in out
    assert store == []


@pytest.mark.asyncio
async def test_write_without_typed_cta_is_unchanged():
    store = []
    out = await _write(FakeConn(store), None, REVIEW_CTA)  # brand default, checked when it was saved
    assert set(out) == {"piece_id", "angle_gate_request_id", "channel", "ready_state"}
    assert store == []


@pytest.mark.asyncio
async def test_write_survives_a_notification_failure():
    out = await _write(FakeConn([], fail_notify=True), REVIEW_CTA, REVIEW_CTA)
    assert out["ready_state"] == "in_progress"
    assert out["cta_warning"]["code"] == "cta_review_claim"


@pytest.mark.asyncio
async def test_check_saved_cta_survives_a_broken_rule():
    with patch.object(cta_review_notice, "cta_review_claims", side_effect=RuntimeError("bad regex")):
        assert await cta_review_notice.check_saved_cta(MagicMock(), TENANT, REVIEW_CTA, "write_cta") is None


@pytest.mark.asyncio
async def test_templated_review_ctas_are_capped_per_tenant_per_day():
    """An API client that puts the tour name in a review CTA makes a new text on every write: the
    bell stops at DAILY_CAP rows for that tenant, the warning still comes back every time."""
    store = []
    pool = _pool(FakeConn(store))
    for i in range(cta_review_notice.DAILY_CAP + 3):
        warning = await cta_review_notice.check_saved_cta(pool, TENANT, f"Rated 4.9 - book tour {i}", "write_cta")
        assert warning and warning["code"] == "cta_review_claim"
    assert len(store) == cta_review_notice.DAILY_CAP
    # another tenant is not affected by the first one's cap
    other = str(uuid.uuid4())
    await cta_review_notice.check_saved_cta(pool, other, "Rated 4.9 - book tour 0", "write_cta")
    assert len(store) == cta_review_notice.DAILY_CAP + 1


@pytest.mark.asyncio
async def test_notification_payload_caps_a_very_long_cta():
    store = []
    long_cta = "Read our reviews " + "x" * 5000
    await cta_review_notice.check_saved_cta(_pool(FakeConn(store)), TENANT, long_cta, "write_cta")
    assert len(store[0]["payload"]["cta"]) == 500


# ---------------------------------------------------------------- POST /v1/content-writing/cta-check

@pytest.mark.asyncio
async def test_cta_check_endpoint_is_the_same_rule_and_writes_nothing():
    hit = await v1_content_writing.cta_check(v1_content_writing.CtaCheckBody(cta="Rated 4.9 on TripAdvisor"),
                                             tenant={"sub": TENANT})
    assert hit == {"cta_warning": {"code": "cta_review_claim", "phrases": ["Rated 4.9", "TripAdvisor"]}}
    miss = await v1_content_writing.cta_check(v1_content_writing.CtaCheckBody(cta="Book your trek"),
                                              tenant={"sub": TENANT})
    assert miss == {"cta_warning": None}


# ---------------------------------------------------------------- admin bell

@pytest.mark.asyncio
async def test_admin_bell_lists_the_notification_with_a_label_and_message():
    now = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    payload = {"message": "Atlas & Hearth (atlas-hearth) set a call to action ...", "cta": REVIEW_CTA}
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{
        "id": 7, "event_type": "tenant.cta.review_claim", "entity_type": "tenant_cta",
        "entity_id": "atlas-hearth", "payload": json.dumps(payload), "target_roles": ["admin"],
        "is_read": False, "dispatched_at": now, "created_at": now,
    }])
    with patch.object(admin, "verify_admin_secret", return_value=None):
        out = await admin.list_notifications(_request(_pool(conn)), x_admin_secret="x", unread_only=False,
                                             limit=10, offset=0)
    item = out["items"][0]
    assert item["title"] == "Tenant CTA mentions reviews or ratings"
    assert item["message"] == payload["message"]
