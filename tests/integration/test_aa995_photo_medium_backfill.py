"""AA-995 — photo_medium_backfill against real Postgres (new SQL needs a real-DB test, lesson S229/S231).

- COUNT_SQL / BATCH_SQL pick only rows with a 1600 w file, no 960 w key yet and not rejected, in one country; the
  keyset cursor walks (sha256, id) and a run bounded by `limit` leaves the rest for the next run.
- SET_SQL writes the key only while the row still has the sha256 the rendition was made from.
- SYNC_RUNNING_SQL sees a running photo_sync job (the run stops, writes nothing).
- The /content/photos route's row read names s3_key_medium (migration 301).
Rows live under a test-only country and are deleted by the fixture.
"""
import io
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.jobs import photo_medium_backfill_job as B  # noqa: E402  (registers the kind)
from services.photos import sync as S  # noqa: E402

COUNTRY = "Aa995 Testland"
ROOT = "aa995_backfill_probe"


def _image() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1800, 1200), (20, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(host=DB_HOST, port=int(DB_PORT), database=DB_NAME, user=DB_USER, password=DB_PASS,
                                  min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.place_photo WHERE drive_root_id = $1", ROOT)
    await p.execute("DELETE FROM shared.job WHERE kind = 'photo_sync' AND created_by = $1", ROOT)
    yield p
    await p.execute("DELETE FROM shared.place_photo WHERE drive_root_id = $1", ROOT)
    await p.execute("DELETE FROM shared.job WHERE kind = 'photo_sync' AND created_by = $1", ROOT)
    await p.close()


async def _photo(pool, fid, *, sha="a" * 64, status="matched", large=True, medium=None):
    slug = S.country_slug(COUNTRY)
    return await pool.fetchval(
        """INSERT INTO shared.place_photo (drive_file_id, drive_root_id, folder_path, country, file_name, status,
                                           s3_key_large, s3_key_small, s3_key_original, s3_key_medium, sha256)
           VALUES ($1, $2, 'x', $3, 'a.jpg', $4, $5, $6, $7, $8, $9) RETURNING id::text""",
        f"{fid}-{uuid.uuid4().hex[:6]}", ROOT, COUNTRY, status,
        f"photos/{slug}/{fid}-1600.webp" if large else None, f"photos/{slug}/{fid}-600.webp" if large else None,
        f"photos/{slug}/{fid}-orig.jpg" if large else None, medium, sha)


class _S3:
    def __init__(self):
        self.puts = {}
        self.image = _image()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.image)}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.puts[Key] = Body


class _Ctx:
    def __init__(self, pool, payload):
        self.pool, self.payload, self.result = pool, payload, None

    async def progress(self, **f):
        pass

    def set_result(self, r):
        self.result = r


async def _mediums(pool):
    rows = await pool.fetch("SELECT id::text AS id, s3_key_medium FROM shared.place_photo WHERE drive_root_id = $1",
                            ROOT)
    return {r["id"]: r["s3_key_medium"] for r in rows}


@pytest.mark.asyncio
async def test_picks_only_missing_rows_and_resumes(pool, monkeypatch):
    monkeypatch.setattr(S, "photo_bucket", lambda: "bucket-under-test")
    a1 = await _photo(pool, "f1", sha="a" * 64)
    a2 = await _photo(pool, "f2", sha="a" * 64)             # the same file in another tour folder
    b = await _photo(pool, "f3", sha="b" * 64)
    rejected = await _photo(pool, "f4", sha="c" * 64, status="rejected")
    no_file = await _photo(pool, "f5", sha=None, status="error", large=False)
    done = await _photo(pool, "f6", sha="d" * 64, medium="photos/x/f6-960.webp")
    assert await pool.fetchval(B.COUNT_SQL, COUNTRY) == 3

    s3 = _S3()
    first = await B.run(_Ctx(pool, {"limit": 2, "country": COUNTRY}), s3=s3)
    assert first["written"] == 2 and first["reused"] == 1 and first["remaining"] == 1
    second = await B.run(_Ctx(pool, {"country": COUNTRY}), s3=s3)
    assert second["missing_before"] == 1 and second["written"] == 1 and second["remaining"] == 0

    got = await _mediums(pool)
    assert got[a1].endswith("-960.webp") and got[a2].endswith("-960.webp") and got[b].endswith("-960.webp")
    assert got[rejected] is None and got[no_file] is None and got[done] == "photos/x/f6-960.webp"
    assert len(s3.puts) == 3 and all(k.startswith("photos/aa995-testland/") for k in s3.puts)


@pytest.mark.asyncio
async def test_set_only_while_the_sha_is_unchanged(pool):
    pid = await _photo(pool, "f1", sha="a" * 64)
    assert await pool.execute(B.SET_SQL, pid, "k-old", "b" * 64) == "UPDATE 0"     # the sync rewrote the bytes
    assert await pool.execute(B.SET_SQL, pid, "k", "a" * 64) == "UPDATE 1"
    assert await pool.execute(B.SET_SQL, pid, "k2", "a" * 64) == "UPDATE 0"         # already set: never overwritten
    assert (await _mediums(pool))[pid] == "k"


@pytest.mark.asyncio
async def test_stops_while_a_photo_sync_job_runs(pool, monkeypatch):
    monkeypatch.setattr(S, "photo_bucket", lambda: "bucket-under-test")
    if await pool.fetchval("SELECT 1 FROM shared.job WHERE kind = 'photo_sync' AND status = 'running'"):
        pytest.skip("another photo_sync job is running in this DB")
    await _photo(pool, "f1")
    await pool.execute(
        """INSERT INTO shared.job (kind, payload, status, created_by, locked_by, locked_until, started_at)
           VALUES ('photo_sync', '{}'::jsonb, 'running', $1, 'test-worker', now() + interval '5 minutes', now())""",
        ROOT)
    s3 = _S3()
    res = await B.run(_Ctx(pool, {"country": COUNTRY}), s3=s3)
    assert res["stopped"] == "photo_sync_running" and res["written"] == 0 and s3.puts == {}


@pytest.mark.asyncio
async def test_photo_route_reads_the_medium_key(pool, monkeypatch):
    from types import SimpleNamespace

    from api.routers import admin_photos
    monkeypatch.delenv("PHOTO_CDN_BASE", raising=False)
    pid = await _photo(pool, "f1", medium=f"photos/{S.country_slug(COUNTRY)}/f1-960.webp")
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    resp = await admin_photos.photo_image(pid, req, size="medium")
    assert resp.headers["location"].split("?")[0].endswith("/aa995-testland/f1-960.webp")
    await pool.execute("UPDATE shared.place_photo SET s3_key_medium = NULL WHERE id = $1::uuid", pid)
    resp = await admin_photos.photo_image(pid, req, size="medium")
    assert resp.headers["location"].split("?")[0].endswith("/aa995-testland/f1-1600.webp")
