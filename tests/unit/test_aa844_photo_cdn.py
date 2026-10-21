"""AA-844 — photos CDN redirect + the daily photo_sync schedule (unit level).

- cdn_url maps photos/<slug>/<file> to <PHOTO_CDN_BASE>/<slug>/<file>?v=<sha256[:12]>; keys outside
  photos/ or PHOTO_CDN_BASE=off give None (presign fallback).
- /content/photos/{id} redirects to the CDN with a 1-day cache, or to a presigned URL (50 min).
- the schedule route is a POLICY write and a MACHINE_ROUTES entry (the ops-probe Lambda calls it
  with the secret and no role).
- scheduled_status: all_ok only when every country ran that day, succeeded, with 0 errors.
The SQL (enqueue_scheduled, LAST_SCHEDULED_SQL) runs on real Postgres in
tests/integration/test_aa844_photo_sync_schedule.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core.staff_authz import MACHINE_ROUTES, PHOTOS_WRITE, POLICY
from api.routers import admin_photos
from services.jobs import photo_sync_job
from services.photos import sync as S

SHA = "0123456789abcdef" * 4
KEY = "photos/south-korea/1AbC_d-9-1600.webp"


def test_cdn_url_strips_the_prefix_and_appends_v(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    assert S.cdn_url(KEY, SHA) == "https://img.lumiguides.it.com/south-korea/1AbC_d-9-1600.webp?v=0123456789ab"
    assert S.cdn_url(KEY, None) == "https://img.lumiguides.it.com/south-korea/1AbC_d-9-1600.webp"
    monkeypatch.setenv("PHOTO_CDN_BASE", "https://cdn.example.test/")
    assert S.cdn_url(KEY, SHA) == "https://cdn.example.test/south-korea/1AbC_d-9-1600.webp?v=0123456789ab"


def test_cdn_url_key_matches_what_sync_writes():
    key = S.s3_key("Sri Lanka", "fileX", "large")
    assert key.startswith(S.PHOTO_KEY_PREFIX)
    assert S.cdn_url(key).endswith("/sri-lanka/fileX-1600.webp")


@pytest.mark.parametrize("key", [None, "", "scripts/x.py", "batch-input/photos/x.webp", "/photos/x.webp"])
def test_cdn_url_none_outside_photos_prefix(monkeypatch, key):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    assert S.cdn_url(key, SHA) is None


@pytest.mark.parametrize("value", ["off", "OFF", " off ", ""])
def test_cdn_url_none_when_switched_off(monkeypatch, value):
    monkeypatch.setenv("PHOTO_CDN_BASE", value)
    assert S.photo_cdn_base() is None
    assert S.cdn_url(KEY, SHA) is None


class _Pool:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        return self.row


def _request(row):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(row))))


_ROW = {"s3_key_large": KEY, "s3_key_small": "photos/south-korea/1AbC_d-9-600.webp",
        "s3_key_original": "photos/south-korea/1AbC_d-9-orig.jpg", "sha256": SHA}
_PID = "6f1c2d3e-0000-4000-8000-000000000001"


def test_photo_image_redirects_to_the_cdn_for_a_day(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    req = _request(_ROW)
    resp = asyncio.run(admin_photos.photo_image(_PID, req, size="small"))
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://img.lumiguides.it.com/south-korea/1AbC_d-9-600.webp?v=0123456789ab"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert "sha256" in req.app.state.pool.calls[0] and "status <> 'rejected'" in req.app.state.pool.calls[0]


def test_photo_image_presign_fallback_when_cdn_off(monkeypatch):
    monkeypatch.setenv("PHOTO_CDN_BASE", "off")
    seen = {}

    class _S3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            seen.update(op=op, ExpiresIn=ExpiresIn, **Params)
            return "https://bucket.s3.test/presigned"

    monkeypatch.setattr(admin_photos, "_client", lambda: _S3())
    monkeypatch.setattr(S, "photo_bucket", lambda: "bucket-under-test")
    resp = asyncio.run(admin_photos.photo_image(_PID, _request(_ROW), size="large"))
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://bucket.s3.test/presigned"
    assert resp.headers["cache-control"] == "public, max-age=3000"
    assert seen == {"op": "get_object", "ExpiresIn": 3600, "Bucket": "bucket-under-test", "Key": KEY}


def test_photo_image_404_for_rejected_or_missing(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin_photos.photo_image(_PID, _request(None), size="large"))
    assert e.value.status_code == 404


def test_schedule_route_is_a_policy_write_and_a_machine_route():
    key = ("POST", "/admin/photos/sync/schedule")
    assert POLICY[key] == PHOTOS_WRITE
    assert "AA-844" in MACHINE_ROUTES[key]
    paths = [r.path for r in admin_photos.router.routes]
    # Declared before /{photo_id}/assign (literal path first).
    assert paths.index("/admin/photos/sync/schedule") < paths.index("/admin/photos/{photo_id}/assign")


def _row(country, status, errors, created_at, finished=True):
    return {"country": country, "id": f"job-{country}", "status": status, "errors": errors,
            "created_at": created_at, "finished_at": created_at + timedelta(minutes=5) if finished else None}


def test_scheduled_status_all_ok_only_when_every_country_ran_clean():
    day = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)
    countries = ["A", "B"]
    assert photo_sync_job.scheduled_status([], countries) is None

    ok = photo_sync_job.scheduled_status([_row("A", "succeeded", 0, day), _row("B", "succeeded", 0, day)], countries)
    assert ok["day"] == "2026-10-21" and ok["all_ok"] is True
    assert [c["country"] for c in ok["countries"]] == countries

    errs = photo_sync_job.scheduled_status([_row("A", "succeeded", 0, day), _row("B", "succeeded", 2, day)], countries)
    assert errs["all_ok"] is False

    # A failed job has no result -> errors NULL -> not ok.
    failed = photo_sync_job.scheduled_status(
        [_row("A", "succeeded", 0, day), _row("B", "failed", None, day)], countries)
    assert failed["all_ok"] is False and failed["countries"][1]["status"] == "failed"

    # B's latest scheduled job is from the day before: listed as not run that day.
    stale = photo_sync_job.scheduled_status(
        [_row("A", "succeeded", 0, day), _row("B", "succeeded", 0, day - timedelta(days=1))], countries)
    assert stale["all_ok"] is False
    assert stale["countries"][1] == {"country": "B", "status": None, "errors": None, "job_id": None,
                                     "finished_at": None}


def test_scheduled_payload_is_one_incremental_country_per_job():
    # The shape enqueue_scheduled sends: run() reads folders, trigger and match_destinations from it.
    assert photo_sync_job.SCHEDULE_CREATED_BY == "scheduler:aa-844"
    assert photo_sync_job.SCHEDULE_KEY_PREFIX == "photo_sync:schedule:"
    assert len({S.country_slug(f["country"]) for f in S.CON_FOLDERS}) == len(S.CON_FOLDERS)
