"""AA-671 — GET /v1/tours/pool facets (activities, intensity, cover image) and their filters, against real Postgres.

The Marketplace in the portal filters the Adventure Asia catalog by activity, intensity and "recently added", and its
cards show a cover photo. The pool rows read them from CIS data the catalog already serves:
  - activities / intensity: the tour's graph components (catalog.v_tour_component on the tenant pool, its source
    geo.tour_place + usable shared.destinations on the admin pool), intensity = the highest component level;
  - cover: the tour's position-1 photo (catalog.v_tour_photo / shared.v_tour_photos).

Checked here: both pools give the same answer (the flag can flip either way), a component on a suspect destination
does not count (v_tour_component's rule), the filters narrow the rows and the count, and the premise of the per-pool
SQL: aa_app_user cannot read geo.tour_place, so the admin-pool SQL on the tenant pool is a 42501.

Harness and stand-ins: tests/integration/test_aa859_t2_version_reads_pool.py (one connection, SET LOCAL ROLE
aa_app_user for the tenant pool, all rolled back).
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _rls_fixture_helpers import migration_body  # noqa: E402
from test_aa859_t2_version_reads_pool import (  # noqa: E402
    BATCH_ID, DSN, MASTER, TTV_LIVE_STANDIN_DDL, _request, _tenant,
)
from api.routers import v1_tours  # noqa: E402


async def _destination(conn, name, *, suspect=False):
    dest = await conn.fetchval("INSERT INTO shared.destinations (name, country, lat, lng) VALUES ($1, 'Vietnam', 10, "
                               "106) RETURNING id", name)
    if suspect:
        await conn.execute("UPDATE shared.destinations SET geo_status = 'suspect' WHERE id = $1", dest)
    return dest


async def _tour(conn, name, *, days_ago=0):
    tour_id = uuid.uuid4()
    await conn.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, "
                       "duration, source_status) VALUES ($1, $2, $3, $4, 'Vietnam', '5 days', 'active')",
                       tour_id, MASTER, BATCH_ID, name)
    pt = await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status, published_at) VALUES ($1, $2, $3, $4, 'S', '[]'::jsonb, 'active', "
        "now() - make_interval(days => $5::int)) RETURNING id", tour_id, uuid.uuid4(), MASTER, name, days_ago)
    return tour_id, pt


async def _component(conn, tour_id, destination_id, activity, intensity, day=1):
    await conn.execute(
        """INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm, country,
                                       country_norm, destination_id, activity, intensity_level, duration_hint,
                                       text_extract, text_hash)
           VALUES ($1, $7, $2, $3, 'p', 'p', 'Vietnam', 'vietnam', $4, $5, $6, 'half_day', 't', 'h')""",
        uuid.uuid4(), tour_id, day, destination_id, activity, intensity,
        str(tour_id))   # source_id = raw_tours.source_key (258: COALESCE(source_group_id, tour_id)::text)


async def _photo(conn, tour_id, file_name, match_source):
    photo = uuid.uuid4()
    await conn.execute(
        """INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name, tour_id,
                                           match_source, status, s3_key_large, s3_key_small)
           VALUES ($1, $2, 'root', 'Vietnam/x', 'Vietnam', $3, $4, $5, 'matched', 'k/l.jpg', 'k/s.jpg')""",
        photo, f"drive-{photo.hex}", file_name, tour_id, match_source)
    return photo


@pytest_asyncio.fixture
async def ctx():
    conn = await asyncpg.connect(DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")
        await conn.execute(TTV_LIVE_STANDIN_DDL)
        await conn.execute(migration_body(229))
        tag = uuid.uuid4().hex[:8]
        a = await _tenant(conn)
        good = await _destination(conn, f"AA671 Hanoi {tag}")
        bad = await _destination(conn, f"AA671 Nowhere {tag}", suspect=True)
        t1, pt1 = await _tour(conn, f"AA671 {tag} trek")
        await _component(conn, t1, good, "trekking", "active")
        await _component(conn, t1, good, "cultural_heritage", "leisurely", day=2)
        await _component(conn, t1, bad, "wildlife_nature", "strenuous", day=3)   # suspect place: not counted
        await _photo(conn, t1, "b.jpg", "auto")
        cover = await _photo(conn, t1, "z.jpg", "manual")                          # manual first (position 1)
        t2, pt2 = await _tour(conn, f"AA671 {tag} plain", days_ago=60)
        yield SimpleNamespace(conn=conn, a=a, tag=tag, pt1=pt1, pt2=pt2, cover=cover)
    finally:
        await tr.rollback()
        await conn.close()


async def _browse(c, flag_on, **filters):
    return await v1_tours.browse_pool(_request(c.conn, flag_on), {"sub": c.a}, page=1, page_size=20, country=None,
                                      search=f"AA671 {c.tag}", duration_min=None, duration_max=None, sort="newest",
                                      **filters)


def _by_id(answer):
    return {d["id"]: d for d in answer["data"]}


@pytest.mark.asyncio
async def test_facets_and_cover_match_on_both_pools(ctx):
    on, off = await _browse(ctx, True), await _browse(ctx, False)
    assert on == off
    rows = _by_id(on)
    assert set(rows) == {ctx.pt1, ctx.pt2}
    one, two = rows[ctx.pt1], rows[ctx.pt2]
    assert one["activities"] == ["cultural_heritage", "trekking"]
    assert one["intensity"] == "challenging"   # active = the highest usable level; strenuous is on a suspect place
    assert one["cover_image_url"].endswith(f"/content/photos/{ctx.cover}?size=small")
    assert one["cover_image_large_url"].endswith(f"/content/photos/{ctx.cover}?size=large")
    assert (two["activities"], two["intensity"], two["cover_image_url"], two["cover_image_large_url"]) == \
        ([], None, None, None)
    for internal in ("activity_codes", "intensity_rank", "cover_photo_path"):
        assert internal not in one
    assert on["facets"] == {"activities": list(v1_tours.POOL_ACTIVITIES),
                            "intensities": list(v1_tours.POOL_INTENSITIES)}


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on", [True, False], ids=["tenant pool", "admin pool"])
@pytest.mark.parametrize("filters, expected", [
    ({"activity": "trekking"}, "1"),
    ({"activity": "cultural_heritage", "intensity": "challenging"}, "1"),
    ({"activity": "wildlife_nature"}, ""),
    ({"intensity": "strenuous"}, ""),
    ({"intensity": "easy"}, ""),
    ({"added_within_days": 30}, "1"),
    ({"added_within_days": 90}, "12"),
], ids=["activity", "activity+intensity", "suspect-activity", "suspect-intensity", "no-easy", "recent", "older"])
async def test_filters_narrow_rows_and_count(ctx, flag_on, filters, expected):
    answer = await _browse(ctx, flag_on, **filters)
    want = {"1": ctx.pt1, "2": ctx.pt2}
    assert set(_by_id(answer)) == {want[k] for k in expected}
    assert answer["pagination"]["total"] == len(expected)


@pytest.mark.asyncio
async def test_admin_pool_sql_is_refused_on_the_tenant_pool(ctx, monkeypatch):
    """Premise of the per-pool SQL: aa_app_user has no SELECT on geo.tour_place, so the catalog views are the only
    way it reads the facets (and cis_app, without catalog grants on Dev, needs the source tables)."""
    monkeypatch.setitem(v1_tours._POOL_FACET_JOIN, True, v1_tours._POOL_FACET_JOIN[False])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _browse(ctx, True)
