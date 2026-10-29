"""AA-995 — the 960 w ("medium") photo rendition, unit level.

- GET /content/photos/{id}?size=medium redirects to the 960 w file, or to the 1600 w file while the row has none yet;
  small / large / original never fall back; a row with no file is 404.
- photo_medium_backfill: bounded by `limit`, resumable (keyset cursor; a failed row is passed and listed), one decode
  per sha256, original first then the 1600 w WebP, stops while a photo_sync runs, stops after 10 failures in a row,
  counts a row the sync rewrote meanwhile as `changed`, DRY RUN writes nothing.
- POST /admin/photos/medium-backfill: a PHOTOS_WRITE policy route declared before /{photo_id}/assign; one job at a time.
The SQL runs on real Postgres in tests/integration/test_aa995_photo_medium_backfill.py.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core.staff_authz import PHOTOS_WRITE, POLICY
from api.routers import admin_photos
from services.jobs import photo_medium_backfill_job as B
from services.photos import sync as S
from shared.jobs.registry import NonRetryable, get_kind

SHA = "0123456789abcdef" * 4
PID = "6f1c2d3e-0000-4000-8000-000000000001"
LARGE = "photos/bhutan/f1-1600.webp"
MEDIUM = "photos/bhutan/f1-960.webp"


def _image(fmt="JPEG", w=2400, h=1600) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(buf, fmt)
    return buf.getvalue()


# ── the public route ─────────────────────────────────────────────────────────────────────────────────────────────────

class _RoutePool:
    def __init__(self, row):
        self.row, self.calls = row, []

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        return self.row


def _get(row, size):
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_RoutePool(row))))
    return asyncio.run(admin_photos.photo_image(PID, req, size=size)), req


def _row(**over):
    row = {"s3_key_large": LARGE, "s3_key_medium": MEDIUM, "s3_key_small": "photos/bhutan/f1-600.webp",
           "s3_key_original": "photos/bhutan/f1-orig.jpg", "sha256": SHA}
    row.update(over)
    return row


def test_medium_redirects_to_the_960_file(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    resp, req = _get(_row(), "medium")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://img.lumiguides.it.com/bhutan/f1-960.webp?v=0123456789ab"
    assert "s3_key_medium" in req.app.state.pool.calls[0]


def test_medium_falls_back_to_the_large_file_until_the_backfill(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    resp, _ = _get(_row(s3_key_medium=None), "medium")
    assert resp.headers["location"] == "https://img.lumiguides.it.com/bhutan/f1-1600.webp?v=0123456789ab"


def test_medium_never_falls_back_to_the_original(monkeypatch):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    with pytest.raises(HTTPException) as e:
        _get(_row(s3_key_medium=None, s3_key_large=None), "medium")
    assert e.value.status_code == 404


@pytest.mark.parametrize("size", ["small", "large", "original"])
def test_other_sizes_never_fall_back(monkeypatch, size):
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    with pytest.raises(HTTPException) as e:
        _get(_row(**{f"s3_key_{size}": None}), size)
    assert e.value.status_code == 404


def test_the_size_query_accepts_medium():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(admin_photos.public_router)
    app.state.pool = _RoutePool(None)
    client = TestClient(app)
    assert client.get(f"/content/photos/{PID}?size=medium", follow_redirects=False).status_code == 404
    assert client.get(f"/content/photos/{PID}?size=huge", follow_redirects=False).status_code == 422


# ── the backfill job ─────────────────────────────────────────────────────────────────────────────────────────────────

class _JobPool:
    """Plays the three job queries over an in-memory list of place_photo rows."""

    def __init__(self, rows, sync_running=False, update_result="UPDATE 1"):
        self.rows = rows
        self.sync_running = sync_running
        self.update_result = update_result
        self.updates = []

    def _missing(self, country):
        return [r for r in self.rows if r.get("medium") is None and r["s3_key_large"]
                and (country is None or r["country"] == country)]

    async def fetchval(self, sql, *args):
        if sql == B.SYNC_RUNNING_SQL:
            return 1 if self.sync_running else None
        assert sql == B.COUNT_SQL
        return len(self._missing(args[0]))

    async def fetch(self, sql, country, sha, last_id, n):
        assert sql == B.BATCH_SQL
        rows = sorted(self._missing(country), key=lambda r: (r["sha256"] or "", r["id"]))
        return [r for r in rows if (r["sha256"] or "", r["id"]) > (sha, last_id)][:n]

    async def execute(self, sql, pid, key, sha):
        assert sql == B.SET_SQL
        self.updates.append((pid, key, sha))
        if self.update_result == "UPDATE 1":
            next(r for r in self.rows if r["id"] == pid)["medium"] = key
        return self.update_result


class _S3:
    def __init__(self, objects):
        self.objects, self.gets, self.puts = dict(objects), [], {}

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        assert ContentType == "image/webp" and "max-age" in CacheControl
        self.puts[Key] = Body


class _Ctx:
    def __init__(self, pool, payload):
        self.pool, self.payload, self.result, self.progressed = pool, payload, None, []

    async def progress(self, **f):
        self.progressed.append(f)

    def set_result(self, r):
        self.result = r


def _photo(n, sha=SHA, country="Bhutan", original=True):
    fid = f"f{n}"
    slug = S.country_slug(country)
    return {"id": f"00000000-0000-4000-8000-{n:012d}", "country": country, "drive_file_id": fid, "sha256": sha,
            "s3_key_large": f"photos/{slug}/{fid}-1600.webp",
            "s3_key_original": f"photos/{slug}/{fid}-orig.jpg" if original else None, "medium": None}


def _run(pool, payload=None, s3=None):
    ctx = _Ctx(pool, payload or {})
    return asyncio.run(B.run(ctx, s3=s3)), ctx


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(S, "photo_bucket", lambda: "bucket-under-test")


def test_kind_is_a_registered_background_kind():
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = get_kind(B.KIND)
    assert k is not None and k.background and k.concurrency == 1 and not k.deploy_blocking


def test_renders_from_the_original_and_decodes_each_sha_once():
    from PIL import Image
    rows = [_photo(1), _photo(2), _photo(3, sha="f" * 64)]   # 1 and 2: the same file in two tour folders
    s3 = _S3({r["s3_key_original"]: _image() for r in rows})
    res, ctx = _run(_JobPool(rows), s3=s3)
    assert res["written"] == 3 and res["reused"] == 1 and res["from_original"] == 2 and res["failed"] == 0
    assert res["remaining"] == 0 and res["stopped"] is None
    assert sorted(s3.gets) == ["photos/bhutan/f1-orig.jpg", "photos/bhutan/f3-orig.jpg"]
    assert sorted(s3.puts) == ["photos/bhutan/f1-960.webp", "photos/bhutan/f2-960.webp", "photos/bhutan/f3-960.webp"]
    assert Image.open(io.BytesIO(s3.puts["photos/bhutan/f3-960.webp"])).size[0] == 960
    assert [r["medium"] for r in rows] == [S.s3_key("Bhutan", f"f{n}", "medium") for n in (1, 2, 3)]
    assert ctx.result == res and ctx.progressed[-1]["phase"] == "done"


def test_undecodable_original_falls_back_to_the_1600_webp():
    row = _photo(1)
    s3 = _S3({row["s3_key_original"]: b"HEIC bytes Pillow can not open",
              row["s3_key_large"]: _image("WEBP", 1600, 1067)})
    res, _ = _run(_JobPool([row]), s3=s3)
    assert res["from_large"] == 1 and res["written"] == 1
    row2 = _photo(2, original=False)
    s3 = _S3({row2["s3_key_large"]: _image("WEBP", 1600, 1067)})
    res, _ = _run(_JobPool([row2]), s3=s3)
    assert res["from_large"] == 1 and s3.gets == [row2["s3_key_large"]]


def test_bounded_by_limit_and_the_next_run_resumes():
    rows = [_photo(n, sha=f"{n:064x}") for n in range(1, 8)]
    s3 = _S3({r["s3_key_original"]: _image(w=1000, h=700) for r in rows})
    pool = _JobPool(rows)
    first, _ = _run(pool, {"limit": 3}, s3=s3)
    assert first["written"] == 3 and first["remaining"] == 4
    second, _ = _run(pool, {"limit": 10}, s3=s3)
    assert second["missing_before"] == 4 and second["written"] == 4 and second["remaining"] == 0


def test_a_failed_row_is_passed_listed_and_left_for_the_next_run():
    rows = [_photo(1, sha="a" * 64), _photo(2, sha="b" * 64)]
    s3 = _S3({rows[1]["s3_key_original"]: _image()})      # row 1 has no file in the bucket at all
    res, _ = _run(_JobPool(rows), s3=s3)
    assert res["failed"] == 1 and res["written"] == 1 and res["remaining"] == 1
    assert res["errors"][0]["photo_id"] == rows[0]["id"] and "KeyError" in res["errors"][0]["error"]


def test_stops_after_ten_failures_in_a_row():
    rows = [_photo(n, sha=f"{n:064x}") for n in range(1, 15)]
    with pytest.raises(NonRetryable) as e:
        _run(_JobPool(rows), s3=_S3({}))
    assert "10 photos failed in a row" in str(e.value)


def test_stops_while_a_photo_sync_runs():
    rows = [_photo(1)]
    s3 = _S3({rows[0]["s3_key_original"]: _image()})
    res, _ = _run(_JobPool(rows, sync_running=True), s3=s3)
    assert res["stopped"] == "photo_sync_running" and res["written"] == 0 and s3.puts == {}


def test_row_rewritten_by_the_sync_meanwhile_counts_as_changed():
    rows = [_photo(1)]
    s3 = _S3({rows[0]["s3_key_original"]: _image()})
    res, _ = _run(_JobPool(rows, update_result="UPDATE 0"), s3=s3)
    assert res["changed"] == 1 and res["written"] == 0


def test_dry_run_counts_only():
    rows = [_photo(1), _photo(2, country="Sri Lanka")]
    s3 = _S3({})
    res, _ = _run(_JobPool(rows), {"dry_run": True, "country": "Sri Lanka"}, s3=s3)
    assert res["missing_before"] == 1 and res["remaining"] == 1 and s3.gets == [] and s3.puts == {}


@pytest.mark.parametrize("payload", [{"limit": 0}, {"limit": B.MAX_LIMIT + 1}, {"limit": "50"}, {"limit": True},
                                     {"country": 5}])
def test_bad_payload_is_non_retryable(payload):
    with pytest.raises(NonRetryable):
        _run(_JobPool([]), payload, s3=_S3({}))


# ── the admin enqueue route ──────────────────────────────────────────────────────────────────────────────────────────

def test_enqueue_route_is_a_policy_write_declared_before_the_catch_all():
    assert POLICY[("POST", "/admin/photos/medium-backfill")] == PHOTOS_WRITE
    paths = [r.path for r in admin_photos.router.routes]
    assert paths.index("/admin/photos/medium-backfill") < paths.index("/admin/photos/{photo_id}/assign")


class _EnqPool:
    def __init__(self, active):
        self.active = active

    async def fetchrow(self, sql, *args):
        assert args == (B.KIND,)
        return self.active


def _enqueue(monkeypatch, body, active=None):
    seen = {}

    async def fake_enqueue(pool, kind, payload, **kw):
        seen.update(kind=kind, payload=payload, **kw)
        return "job-1", True
    monkeypatch.setattr(admin_photos, "verify_admin_secret", lambda s: None)
    monkeypatch.setattr("shared.jobs.registry.enqueue", fake_enqueue)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_EnqPool(active))))
    return asyncio.run(admin_photos.medium_backfill(body, req, "secret", "staff-1")), seen


def test_enqueue_creates_one_job(monkeypatch):
    out, seen = _enqueue(monkeypatch, admin_photos.MediumBackfillRequest(limit=200, country="Bhutan"))
    assert out == {"job_id": "job-1", "created": True, "status": "queued"}
    assert seen["kind"] == B.KIND and seen["created_by"] == "admin:staff-1"
    assert seen["payload"] == {"dry_run": False, "country": "Bhutan", "limit": 200}


def test_enqueue_returns_the_active_job(monkeypatch):
    out, seen = _enqueue(monkeypatch, admin_photos.MediumBackfillRequest(), active={"id": "job-0", "status": "running"})
    assert out == {"job_id": "job-0", "created": False, "status": "running"} and seen == {}


def test_enqueue_refuses_a_bad_limit(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _enqueue(monkeypatch, admin_photos.MediumBackfillRequest(limit=B.MAX_LIMIT + 1))
    assert e.value.status_code == 422
