"""AA-835 — the CIS supply content flow on Postgres: Booking feed -> pull -> draft job -> review -> publish, and the
change handling (stale, replay, older version, deactivate, missing entity).

Booking's contract views (bk-023: booking.v_supply_outbox / v_supply_hotels / v_supply_media) are stand-in tables
created INSIDE the test's transaction and rolled back with it (lesson: stand-ins only around the test). The CIS
tables are 278 part A from the conftest replay. The LLM call is a stub (no paid call); the photo copy runs for real
against a fake S3 (make_sizes on a real JPEG). catalog.v_supply_content itself (278 part B, the catalog recipe) is
covered by test_aa835_migration_278 on the RDS-like state; here the entity / version rows it reads are asserted.
"""
import asyncio
import hashlib
import io
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.jobs import supply_content_draft_job as draft_job  # noqa: E402
from services.jobs import supply_media_purge_job as purge_job  # noqa: E402
from services.supply_content import pull, review, writer  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
BUCKET = "aa-booking-dev-supply-media-123456789012"

STANDINS = """
    CREATE SCHEMA IF NOT EXISTS booking;
    CREATE TABLE booking.v_supply_outbox (event_id uuid, event_type text, entity_type text, entity_id uuid,
                                          supply_version bigint, occurred_at timestamptz DEFAULT now(),
                                          feed_seq bigint);
    CREATE TABLE booking.v_supply_hotels (id uuid, supply_version bigint, name text, hotel_style text,
                                          star_rating smallint, address text, destination_id uuid, country_code text,
                                          lat double precision, lng double precision, check_in_time time,
                                          check_out_time time, amenities text[], room_types jsonb, status text,
                                          media jsonb);
    CREATE TABLE booking.v_supply_services (id uuid, supply_version bigint, service_type text, working_title text,
                                            unit text, destination_id uuid, country_code text, duration_minutes int,
                                            max_group_size smallint, inclusions text[], status text, media jsonb);
    CREATE TABLE booking.v_supply_media (media_id uuid, entity_type text, entity_id uuid, usage_rights boolean,
                                         s3_bucket text, s3_key text, sha256 text, content_type text);
"""


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


class _FakeS3:
    def __init__(self, objects):
        self.objects, self.puts, self.deletes = objects, {}, []

    def get_object(self, Bucket, Key):
        data = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "Body": io.BytesIO(data)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.puts.pop(Key, None)


class _FakeCloudFront:
    def __init__(self):
        self.batches = []

    def create_invalidation(self, DistributionId, InvalidationBatch):
        self.batches.append((DistributionId, InvalidationBatch))
        return {"Invalidation": {"Id": "I1"}}


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buf, "JPEG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(STANDINS)
        await c.execute("DELETE FROM shared.supply_feed_cursor WHERE consumer = $1", pull.CONSUMER)
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SUPPLY_CONTENT_LLM", raising=False)
    monkeypatch.delenv("SUPPLY_MEDIA_BUCKET", raising=False)
    monkeypatch.setenv("SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY", "50")
    monkeypatch.setenv("PHOTO_CDN_BASE", "https://img.example.test")


async def _hotel(c, hotel_id, version, *, name="Riverside Hotel", status="active", media=()):
    await c.execute("DELETE FROM booking.v_supply_hotels WHERE id = $1", hotel_id)
    await c.execute(
        """INSERT INTO booking.v_supply_hotels VALUES ($1, $2, $3, 'boutique', 4, '12 River Road', NULL, 'VN',
                                                       NULL, NULL, '14:00', '12:00', ARRAY['pool'], '[]'::jsonb, $4,
                                                       $5::jsonb)""",
        hotel_id, version, name, status, json.dumps(list(media)))


async def _event(c, entity_id, seq, version, *, event_type="supply.upsert", entity_type="hotel"):
    event_id = uuid.uuid4()
    await c.execute("INSERT INTO booking.v_supply_outbox (event_id, event_type, entity_type, entity_id, "
                    "supply_version, feed_seq) VALUES ($1, $2, $3, $4, $5, $6)",
                    event_id, event_type, entity_type, entity_id, version, seq)
    return event_id


async def _entity(c, source_id):
    return await c.fetchrow("SELECT id::text AS id, applied_supply_version, active, content_status, "
                            "published_version_id::text AS published_version_id, title "
                            "FROM shared.supply_entity WHERE source_id = $1", str(source_id))


async def _run_job(c, source_id, version, monkeypatch, s3):
    monkeypatch.setattr("services.supply_content.photos._s3", lambda: s3)
    monkeypatch.setattr(writer, "draft_content", lambda entity_type, facts: (
        {"description": f"A boutique hotel called {facts['name']}.", "highlights": ["Pool"],
         "good_to_know": ["Check-in from 14:00"]},
        {"model": "sonnet", "tokens_in": 900, "tokens_out": 200, "cost_usd": 0.02}))
    costs = []
    ctx = SimpleNamespace(pool=_OneConnPool(c), payload={"source_id": str(source_id), "supply_version": version,
                                                         "trigger": "feed"},
                          job_id=str(uuid.uuid4()), job=SimpleNamespace(attempt=1), add_cost=costs.append)
    return await draft_job.run(ctx)


@pytest.mark.asyncio
async def test_feed_to_published_and_change_handling(conn, monkeypatch):
    c, pool = conn, _OneConnPool(conn)
    hotel = uuid.uuid4()
    ok_media, no_rights = uuid.uuid4(), uuid.uuid4()
    image = _jpeg()
    sha = hashlib.sha256(image).hexdigest()
    key = f"supply-media/hotel/{hotel}/{ok_media}.jpg"
    media = [{"media_id": str(ok_media), "usage_rights": True, "content_type": "image/jpeg", "width": 64,
              "height": 48, "sha256": sha},
             {"media_id": str(no_rights), "usage_rights": False, "content_type": "image/jpeg", "width": 64,
              "height": 48, "sha256": "b" * 64}]
    await c.execute("INSERT INTO booking.v_supply_media VALUES ($1, 'hotel', $2, true, $3, $4, $5, 'image/jpeg'), "
                    "($6, 'hotel', $2, false, $3, $7, $8, 'image/jpeg')",
                    ok_media, hotel, BUCKET, key, sha, no_rights, f"supply-media/hotel/{hotel}/{no_rights}.jpg",
                    "b" * 64)
    s3 = _FakeS3({(BUCKET, key): image})

    # 1. created in Booking -> one applied event, a draft entity, one draft job
    await _hotel(c, hotel, 1, media=media)
    await _event(c, hotel, 101, 1)
    out = await pull.pull_once(pool)
    assert (out["applied"], out["drafts_queued"], out["cursor"]) == (1, 1, 101)
    e = await _entity(c, hotel)
    assert (e["applied_supply_version"], e["content_status"], e["active"]) == (1, "draft", True)
    assert await c.fetchval("SELECT outcome FROM shared.supply_event_inbox WHERE source_id = $1", str(hotel)) \
        == "applied"
    assert await c.fetchval("SELECT count(*) FROM shared.job WHERE idempotency_key = $1",
                            pull.draft_key(str(hotel), 1)) == 1
    assert await c.fetchval("SELECT count(*) FROM shared.supply_media WHERE entity_id = $1::uuid", e["id"]) == 2

    # 2. the draft job: photos copied (rights only), one llm draft, in review
    result = await _run_job(c, hotel, 1, monkeypatch, s3)
    assert result["content_version"] == 1 and result["photos"] == {"copied": 1, "failed": []}
    assert sorted(s3.puts) == [f"photos/supply/hotel/{ok_media}-1600.webp", f"photos/supply/hotel/{ok_media}-600.webp"]
    detail = await review.get_detail(pool, e["id"])
    draft = detail["current_draft"]
    assert draft["origin"] == "llm" and [p["media_id"] for p in draft["photos"]] == [str(ok_media)]
    assert detail["entity"]["content_status"] == "in_review" and detail["diff"]
    url = await c.fetchval("SELECT url_1600 FROM shared.supply_media WHERE booking_media_id = $1", ok_media)
    assert url == f"https://img.example.test/supply/hotel/{ok_media}-1600.webp?v={sha[:12]}"

    # 3. a photo without usage rights cannot be published; the llm draft can
    edit = await review.create_human_version(c, e["id"], {"description": "Edited text.", "photos": [
        {"media_id": str(no_rights)}]}, actor="staff:test")
    with pytest.raises(review.ReviewError) as err:
        await review.publish(c, e["id"], edit["id"], actor="staff:test")
    assert err.value.status == 409 and err.value.code == "photo_rights"
    published = await review.publish(c, e["id"], draft["id"], actor="system:aa835-evidence")
    assert published["content_status"] == "published"
    e = await _entity(c, hotel)
    assert (e["content_status"], e["published_version_id"]) == ("published", draft["id"])
    assert await c.fetchval("SELECT status FROM shared.supply_content_version WHERE id = $1::uuid", edit["id"]) \
        == "superseded"

    # 4. renamed in Booking: stale (no text served), a new draft job; the old version is outdated
    await _hotel(c, hotel, 2, name="Riverside Grand", media=media)
    await _event(c, hotel, 102, 2)
    out = await pull.pull_once(pool)
    assert (out["applied"], out["drafts_queued"]) == (1, 1)
    e = await _entity(c, hotel)
    assert (e["content_status"], e["title"], e["applied_supply_version"]) == ("stale", "Riverside Grand", 2)
    late = await review.create_human_version(c, e["id"], {"description": "Written for v2."}, actor="staff:t")
    assert late["supply_version"] == 2
    result = await _run_job(c, hotel, 1, monkeypatch, s3)          # the v1 job, late: skipped
    assert result["skipped"] == "superseded"

    # 5. replay from the start: every key is a duplicate, nothing changes
    await c.execute("UPDATE shared.supply_feed_cursor SET last_feed_seq = 0 WHERE consumer = $1", pull.CONSUMER)
    out = await pull.pull_once(pool)
    assert (out["duplicate"], out["applied"], out["cursor"]) == (2, 0, 102)

    # 6. an event whose facts are not newer is recorded as skipped_older
    await _event(c, hotel, 103, 1)
    out = await pull.pull_once(pool)
    assert out["skipped_older"] == 1 and (await _entity(c, hotel))["content_status"] == "stale"

    # 7. deactivated in Booking: unpublished, rows kept, no draft job
    await _hotel(c, hotel, 3, status="inactive", media=media)
    await _event(c, hotel, 104, 3, event_type="supply.deactivate")
    out = await pull.pull_once(pool)
    assert (out["applied"], out["unpublished"], out["drafts_queued"]) == (1, 1, 0)
    e = await _entity(c, hotel)
    assert (e["active"], e["content_status"], e["published_version_id"]) == (False, "unpublished", draft["id"])
    with pytest.raises(review.ReviewError) as err:
        await review.publish(c, e["id"], late["id"], actor="staff:t")
    assert err.value.code == "inactive"

    # 8. an entity that left the facts view: missing, nothing created
    gone = uuid.uuid4()
    await _event(c, gone, 105, 1, entity_type="service")
    out = await pull.pull_once(pool)
    assert out["missing"] == 1 and await _entity(c, gone) is None

    status = await review.feed_status(pool)
    assert status["cursor"] == 105 and status["pending"] == 0 and status["feed_available"]
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_pull_error_is_recorded_and_the_cursor_stays(conn):
    pool = _OneConnPool(conn)
    await pull.pull_once(pool)                                       # creates the cursor row
    await conn.execute("INSERT INTO booking.v_supply_outbox (event_id, event_type, entity_type, entity_id, "
                       "supply_version, feed_seq) VALUES ($1, 'supply.upsert', 'room', $2, 1, 7)",
                       uuid.uuid4(), uuid.uuid4())
    with pytest.raises(ValueError):
        await pull.pull_once(pool)
    row = await conn.fetchrow("SELECT last_feed_seq, last_error FROM shared.supply_feed_cursor WHERE consumer = $1",
                              pull.CONSUMER)
    assert row["last_feed_seq"] == 0 and "outside the contract" in row["last_error"]


async def _copy_columns(c, media_id):
    return await c.fetchrow("SELECT cis_key_1600, cis_key_600, url_1600, url_600, copied_sha256, copied_at "
                            "FROM shared.supply_media WHERE booking_media_id = $1", media_id)


async def _purge_jobs(c, media_id):
    return await c.fetch("SELECT idempotency_key, payload::text AS payload FROM shared.job WHERE kind = $1 "
                         "AND payload->>'media_id' = $2", purge_job.KIND, str(media_id))


@pytest.mark.asyncio
async def test_withdrawn_rights_and_removed_entity_purge_the_cis_copies(conn, monkeypatch):
    c, pool = conn, _OneConnPool(conn)
    monkeypatch.setenv("PHOTO_CDN_DISTRIBUTION_ID", "EDIST")
    hotel, photo = uuid.uuid4(), uuid.uuid4()
    image = _jpeg()
    sha = hashlib.sha256(image).hexdigest()
    key = f"supply-media/hotel/{hotel}/{photo}.jpg"
    await c.execute("INSERT INTO booking.v_supply_media VALUES ($1, 'hotel', $2, true, $3, $4, $5, 'image/jpeg')",
                    photo, hotel, BUCKET, key, sha)
    s3 = _FakeS3({(BUCKET, key): image})
    media = [{"media_id": str(photo), "usage_rights": True, "content_type": "image/jpeg", "sha256": sha}]

    await _hotel(c, hotel, 1, media=media)
    await _event(c, hotel, 201, 1)
    await pull.pull_once(pool)
    await _run_job(c, hotel, 1, monkeypatch, s3)
    copies = sorted(s3.puts)
    assert len(copies) == 2 and (await _copy_columns(c, photo))["copied_sha256"] == sha

    # usage rights withdrawn in Booking: one purge job, the view-facing flags already off
    await _hotel(c, hotel, 2, media=[{**media[0], "usage_rights": False}])
    await _event(c, hotel, 202, 2)
    out = await pull.pull_once(pool)
    assert out["purges_queued"] == 1
    jobs = await _purge_jobs(c, photo)
    assert len(jobs) == 1 and jobs[0]["idempotency_key"].startswith(f"supply_media_purge:{photo}:")

    # the same state seen again (replayed cursor, another event) queues nothing new
    await _event(c, hotel, 203, 2)
    assert (await pull.pull_once(pool))["purges_queued"] == 0

    cf = _FakeCloudFront()
    ctx = SimpleNamespace(pool=pool, payload=json.loads(jobs[0]["payload"]), job_id=str(uuid.uuid4()),
                          job=SimpleNamespace(attempt=1), add_cost=lambda x: None)
    result = await purge_job.run(ctx, s3=s3, cf=cf)
    assert sorted(s3.deletes) == copies and not s3.puts
    row = await _copy_columns(c, photo)
    assert all(row[k] is None for k in row.keys())
    assert result["invalidation"] == "I1"
    assert cf.batches[0][1]["Paths"]["Items"] == [f"/supply/hotel/{photo}-*"]
    assert (await purge_job.run(ctx, s3=s3, cf=cf))["skipped"] == "copy_gone"    # a retry is a no-op

    # rights restored: copied again on the next draft; then the hotel leaves the facts view -> purged again
    await _hotel(c, hotel, 3, media=media)
    await _event(c, hotel, 204, 3)
    await pull.pull_once(pool)
    await _run_job(c, hotel, 3, monkeypatch, s3)
    assert (await _copy_columns(c, photo))["copied_sha256"] == sha
    await c.execute("DELETE FROM booking.v_supply_hotels WHERE id = $1", hotel)
    await _event(c, hotel, 205, 4)
    out = await pull.pull_once(pool)
    assert out["missing"] == 1 and out["purges_queued"] == 1
    assert len(await _purge_jobs(c, photo)) == 2                                 # a new key for the new copy
    assert not await c.fetchval("SELECT active FROM shared.supply_media WHERE booking_media_id = $1", photo)


def test_job_kind_registered():
    from shared.jobs.registry import get_kind

    assert get_kind(draft_job.KIND).background and get_kind(purge_job.KIND).background
    assert asyncio.iscoroutinefunction(draft_job.run)
