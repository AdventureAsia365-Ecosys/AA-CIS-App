"""AA-835 — the supply feed consumer's rules (services/supply_content/pull.py), the draft job's switches
(services/jobs/supply_content_draft_job.py), the photo source checks and the route policy. The end-to-end flow on
Postgres is tests/integration/test_aa835_supply_flow.py."""
import asyncio
from types import SimpleNamespace

import pytest

from api.core import staff_authz
from services.jobs import supply_content_draft_job as job
from services.supply_content import photos, pull
from shared.cost_guard import BudgetExceeded

MEDIA = "11111111-1111-4111-8111-111111111111"
ENTITY = "22222222-2222-4222-8222-222222222222"


def _state(version=3, published=None, status="draft", active=True):
    return pull.EntityState("e1", version, active, status, published)


def test_outcome_orders_by_version():
    assert pull.outcome_for(None, 1) == "applied"
    assert pull.outcome_for(_state(3), 4) == "applied"
    assert pull.outcome_for(_state(3), 3) == "skipped_older"
    assert pull.outcome_for(_state(3), 2) == "skipped_older"
    assert pull.outcome_for(_state(3), None) == "missing"


def test_status_after_apply():
    assert pull.status_after_apply(None, True) == "draft"
    assert pull.status_after_apply(_state(status="in_review"), True) == "draft"
    assert pull.status_after_apply(_state(published="v1", status="published"), True) == "stale"
    assert pull.status_after_apply(_state(published="v1", status="unpublished", active=False), True) == "stale"
    assert pull.status_after_apply(_state(published="v1", status="published"), False) == "unpublished"
    assert pull.status_after_apply(None, False) == "unpublished"


def test_draft_key_is_per_entity_version():
    assert pull.draft_key("abc", 4) == "supply_content_draft:abc:4"


def test_job_kind_is_background_and_capped():
    from shared.jobs.registry import get_kind

    kind = get_kind(job.KIND)
    assert kind is not None and kind.background and kind.concurrency == 1 and kind.max_attempts == 2


def test_kill_switch_and_cap(monkeypatch):
    monkeypatch.delenv("SUPPLY_CONTENT_LLM", raising=False)
    assert job.llm_enabled()
    monkeypatch.setenv("SUPPLY_CONTENT_LLM", "off")
    assert not job.llm_enabled()
    monkeypatch.delenv("SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY", raising=False)
    assert job.max_drafts_per_day() == 20
    monkeypatch.setenv("SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY", "3")
    assert job.max_drafts_per_day() == 3


class _Pool:
    def __init__(self, entity, drafts_today=0):
        self.entity, self.drafts_today = entity, drafts_today

    async def fetchrow(self, sql, *args):
        return self.entity

    async def fetchval(self, sql, *args):
        return self.drafts_today


def _ctx(pool, payload):
    return SimpleNamespace(pool=pool, payload=payload, job_id="j1", job=SimpleNamespace(attempt=1))


ENT = {"id": "e1", "entity_type": "hotel", "title": "Riverside", "facts": "{}", "applied_supply_version": 2,
       "active": True}


def test_job_skips_without_a_paid_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no LLM call expected")

    monkeypatch.setattr("services.supply_content.writer.draft_content", boom)
    payload = {"source_id": ENTITY, "supply_version": 2, "trigger": "feed"}
    monkeypatch.setenv("SUPPLY_CONTENT_LLM", "off")
    assert asyncio.run(job.run(_ctx(_Pool(ENT), payload)))["skipped"] == "llm_off"
    monkeypatch.delenv("SUPPLY_CONTENT_LLM")
    assert asyncio.run(job.run(_ctx(_Pool({**ENT, "active": False}), payload)))["skipped"] == "inactive"
    assert asyncio.run(job.run(_ctx(_Pool({**ENT, "applied_supply_version": 3}), payload)))["skipped"] == "superseded"
    monkeypatch.setenv("SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY", "5")
    with pytest.raises(BudgetExceeded):
        asyncio.run(job.run(_ctx(_Pool(ENT, drafts_today=5), payload)))


def test_draft_photos_keep_booking_order_and_only_usable():
    facts = {"media": [{"media_id": "b"}, {"media_id": "a"}, {"media_id": "c"}]}
    assert [p["media_id"] for p in job.draft_photos(facts, {"a", "b"}, "Riverside")] == ["b", "a"]


def _src(**over):
    row = {"media_id": MEDIA, "entity_type": "hotel", "entity_id": ENTITY, "usage_rights": True,
           "s3_bucket": "aa-booking-dev-supply-media-123456789012",
           "s3_key": f"supply-media/hotel/{ENTITY}/{MEDIA}.jpg", "sha256": "a" * 64, "content_type": "image/jpeg"}
    row.update(over)
    return row


@pytest.mark.parametrize("over", [
    {"usage_rights": False},
    {"entity_id": "33333333-3333-4333-8333-333333333333"},
    {"sha256": "b" * 64},
    {"s3_bucket": "aa-cis-bronze-123456789012"},
    {"s3_key": f"supply-media/hotel/{ENTITY}/{ENTITY}.jpg"},
    {"s3_key": f"supply-media/service/{ENTITY}/{MEDIA}.jpg"},
])
def test_photo_source_refused(over):
    with pytest.raises(photos.PhotoRefused):
        photos.check_source(_src(**over), source_id=ENTITY, sha256="a" * 64)


def test_photo_source_ok_and_bucket_pin(monkeypatch):
    photos.check_source(_src(), source_id=ENTITY, sha256="a" * 64)
    monkeypatch.setenv("SUPPLY_MEDIA_BUCKET", "aa-booking-dev-supply-media-000000000000")
    with pytest.raises(photos.PhotoRefused):
        photos.check_source(_src(), source_id=ENTITY, sha256="a" * 64)


@pytest.mark.parametrize("method,route", [
    ("POST", "/admin/supply-content/{entity_id}/versions"),
    ("POST", "/admin/supply-content/{entity_id}/draft"),
    ("POST", "/admin/supply-content/{entity_id}/versions/{version_id}/publish"),
    ("POST", "/admin/supply-content/{entity_id}/versions/{version_id}/reject"),
    ("POST", "/admin/supply-content/{entity_id}/unpublish"),
])
def test_writes_need_a_content_role(method, route):
    for role, allowed in (("admin", True), ("content", True), ("reviewer", True), ("viewer", False)):
        reason = staff_authz.denial_reason(staff_authz.principal_from_role(role), method, route)
        assert (reason is None) is allowed, (role, route, reason)
    assert staff_authz.denial_reason(staff_authz.MACHINE, method, route) is not None
