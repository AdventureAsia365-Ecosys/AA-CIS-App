"""AA-1006 follow-up (S263 F3) — GET /admin/a4/content-log paging against real Postgres (all migrations applied by
conftest): `offset` pages the newest-first rows, `total` and `by_channel` count every match (the grouped count query
runs over the same joins and WHERE as the page query), and a first page that holds every match needs no count query.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_a4  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
_SECRET = "aa1006-test-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # Same two columns test_aa815_publish_log_sql.py adds: migration 124 (pgvector) does not apply in the test DB.
    await p.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS angle_gate_option_id UUID")
    await p.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel TEXT")
    tid = uuid.uuid4()
    await p.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'Lantern Trails', $2, 3)",
        tid, f"aa1006-{tid.hex[:8]}")
    yield p, tid
    await p.execute("DELETE FROM acp_shared.publish_log WHERE tenant_id = $1", tid)
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    await p.close()


async def _piece(p, tid, channel, status="approved", minutes_ago=0):
    req = await p.fetchval(
        "INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
        "VALUES ($1, 'a1006', $2, 'approved') RETURNING request_id", tid, channel)
    return await p.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, channel, created_at) "
        "VALUES ($1, $2, 1, 'Morning light over the rope bridge', $3, '[]'::jsonb, '[]'::jsonb, $4, "
        "now() - make_interval(mins => $5)) RETURNING piece_id",
        tid, req, status, channel, minutes_ago)


def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


async def _log(pool, tid, **kw):
    args = dict(tenant_id=str(tid), tour_id=None, status=None, channel=None, published=None,
                date_from=None, date_to=None, x_admin_secret=_SECRET)
    args.update(kw)
    return await admin_a4.get_content_log(_request(pool), **args)


@pytest.mark.asyncio
async def test_pages_newest_first_with_the_real_total(ctx):
    p, tid = ctx
    channels = ["blog", "blog", "linkedin", "facebook", "blog"]
    ids = [await _piece(p, tid, ch, minutes_ago=i) for i, ch in enumerate(channels)]

    first = await _log(p, tid, limit=2, offset=0)
    second = await _log(p, tid, limit=2, offset=2)
    last = await _log(p, tid, limit=2, offset=4)

    assert [r["piece_id"] for r in first["data"]] == [str(i) for i in ids[0:2]]
    assert [r["piece_id"] for r in second["data"]] == [str(i) for i in ids[2:4]]
    assert [r["piece_id"] for r in last["data"]] == [str(ids[4])]
    for page in (first, second, last):
        assert page["total"] == 5
        assert page["by_channel"] == {"blog": 3, "linkedin": 1, "facebook": 1}
    assert (first["offset"], second["offset"], last["offset"]) == (0, 2, 4)


@pytest.mark.asyncio
async def test_count_follows_the_filters(ctx):
    p, tid = ctx
    await _piece(p, tid, "blog", status="held")
    await _piece(p, tid, "blog", status="approved", minutes_ago=1)
    await _piece(p, tid, "linkedin", status="held", minutes_ago=2)
    published = await _piece(p, tid, "blog", status="approved", minutes_ago=3)
    await p.execute(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, external_id, external_url, "
        "published_at) VALUES ($1, $2, 'blog', 'published', 'x1', 'https://example.test/p/1', now())",
        published, tid)

    held = await _log(p, tid, status="held", limit=1)
    assert len(held["data"]) == 1
    assert held["total"] == 2
    assert held["by_channel"] == {"blog": 1, "linkedin": 1}

    live = await _log(p, tid, published="yes", limit=1)
    assert [r["piece_id"] for r in live["data"]] == [str(published)]
    assert live["total"] == 1

    not_live_blog = await _log(p, tid, channel="blog", published="no", limit=1, offset=1)
    assert not_live_blog["total"] == 2
    assert not_live_blog["by_channel"] == {"blog": 2}


@pytest.mark.asyncio
async def test_first_page_holding_every_match_counts_from_the_page(ctx):
    p, tid = ctx
    await _piece(p, tid, "blog")
    await _piece(p, tid, "facebook", minutes_ago=1)

    out = await _log(p, tid, limit=200)
    assert len(out["data"]) == 2
    assert out["total"] == 2
    assert out["by_channel"] == {"blog": 1, "facebook": 1}
