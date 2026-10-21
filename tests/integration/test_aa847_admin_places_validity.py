"""AA-847 — content-validity report and fixes (services/places/validity*.py, /admin/places/validity|coverage|fix)
against real Postgres (conftest applies every migration).

One live Laos tour (raw_tours.country is checked against real countries, so the assertions select the seeded tour
by its source id; the fixes are scoped to it with source_id, never to the whole country of the shared test DB) names
Kuang Si twice (a geocoded point and the model's point 150 m away), the Ho Chi Minh Trail (a linear feature stored
as a confirmed point) and the Bolaven Plateau (an area feature stored as a point). The merged duplicate is held by a
component, an overnight, a geo.place_name key, a place card and a photo, so every repoint is checked.

  * report: one finding per defect with its reason code; the route answers the page, the counts and attribution;
  * duplicate_place: the dry run writes nothing; apply sets duplicate_of, repoints every CIS row, writes one audit
    row with the prior values, enqueues a graph build; the store lookup of the merged key answers the keeper (no
    new geocode, no new row); undo restores every row and cannot run twice;
  * not_a_pin / linear_feature: precision area; a linear feature is also hidden (suspect + linear_feature);
  * coverage lists the country; the fix route refuses an unknown check.
"""
import json
import os
import random
import string
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from _audit_cleanup import delete_audit_rows

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_places  # noqa: E402
from services.places import store, validity, validity_fix  # noqa: E402
from services.places.locate import Located  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "aa847-integration-secret"
MASTER = validity.MASTER_TENANT_ID


async def _dest(conn, name, country, lat, lng, located_by="geoapify", precision="point"):
    return await conn.fetchval(
        """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision,
                                            name_norm, locality_norm)
           VALUES ($1, $2, $3, $4, $5, 'ok', $6, geo.name_norm($1), '') RETURNING id::text""",
        name, country, lat, lng, located_by, precision)


async def _component(conn, source_id, tour_id, day, name, country, dest):
    comp = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm, country,
                                       country_norm, destination_id, activity, intensity_level, duration_hint,
                                       text_extract, text_hash)
           VALUES ($1::uuid, $2, $3::uuid, $4, $5, geo.name_norm($5), $6, lower($6), $7::uuid, 'cultural_heritage',
                   'moderate', 'full_day', 'text', 'h')""", comp, source_id, tour_id, day, name, country, dest)
    return comp


@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    admin_places._validity_cache.clear()
    tag = "zq" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    country = "Laos"
    actor = f"admin:aa847-{tag}"
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tour_id, group = str(uuid.uuid4()), str(uuid.uuid4())
    itinerary = (f"Day 1 — Luang Prabang\nSwim at Kuang Si Falls {tag}.\n\n"
                 f"Day 2 — Ride\nThe Ho Chi Minh Trail {tag} by bike, then the Bolaven Plateau {tag}.")
    async with pool.acquire() as c:
        # the AA tenant for the FORCE RLS tour tables (CI's superuser bypasses RLS; a local non-superuser does not)
        await c.execute("SELECT set_config('app.tenant_id', $1, false)", MASTER)
        await c.execute(
            """INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                                                         source_group_id, source_status, duration)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, 'active', '2 days')""",
            tour_id, MASTER, BATCH_ID, f"Laos {tag}", country, group)
        await c.execute(
            """INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name,
                                                             aa_summary, aa_itineraries, master_status)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'Summary', $5, 'active')""",
            tour_id, str(uuid.uuid4()), MASTER, f"Laos {tag}", itinerary)
        d = {
            "keep": await _dest(c, f"Kuang Si Falls {tag}", country, 19.7491, 101.9935),
            "dup": await _dest(c, f"Tat Kuang Si {tag}", country, 19.7480, 101.9925, "llm", "area"),
            "trail": await _dest(c, f"Ho Chi Minh Trail {tag}", country, 16.46, 107.20),
            "plateau": await _dest(c, f"Bolaven Plateau {tag}", country, 15.10, 106.30),
        }
        comps = {
            "keep": await _component(c, group, tour_id, 1, f"Kuang Si Falls {tag}", country, d["keep"]),
            "dup": await _component(c, group, tour_id, 1, f"Tat Kuang Si {tag}", country, d["dup"]),
            "trail": await _component(c, group, tour_id, 2, f"Ho Chi Minh Trail {tag}", country, d["trail"]),
            "plateau": await _component(c, group, tour_id, 2, f"Bolaven Plateau {tag}", country, d["plateau"]),
        }
        await c.execute(
            """INSERT INTO geo.tour_day (source_id, day_index, tour_id, title, overnight_place,
                                         overnight_destination_id, places, extracted_by)
               VALUES ($1, 1, $2::uuid, 'Day 1', $3, $4::uuid, ARRAY[]::text[], 'rule')""",
            group, tour_id, f"Tat Kuang Si {tag}", d["dup"])
        dup_key = (await c.fetchval("SELECT geo.name_norm($1)", f"Tat Kuang Si {tag}"), country.lower(), "")
        await c.execute(
            """INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country, status,
                                           destination_id)
               VALUES ($1, $2, $3, $4, $5, 'resolved', $6::uuid)""", *dup_key, f"Tat Kuang Si {tag}", country,
            d["dup"])
        card = str(uuid.uuid4())
        await c.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                           VALUES ($1::uuid, $2, $3, $4, $5::uuid)""",
                        card, f"tat-kuang-si-{tag}", f"Tat Kuang Si {tag}", country, d["dup"])
        photo = str(uuid.uuid4())
        await c.execute(
            """INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name,
                                               tour_id, destination_id, match_source, status, s3_key_large)
               VALUES ($1::uuid, $2, 'root', 'Laos/x', $3, 'a.jpg', $4::uuid, $5::uuid, 'auto', 'matched', 'k')""",
            photo, f"zq847-{tag}", country, tour_id, d["dup"])
    yield SimpleNamespace(pool=pool, tag=tag, country=country, actor=actor, tour_id=tour_id, group=group, d=d,
                          comps=comps, dup_key=dup_key, card=card, photo=photo)
    async with pool.acquire() as c:
        await c.execute("SELECT set_config('app.tenant_id', $1, false)", MASTER)
        ids = list(d.values())
        await c.execute("DELETE FROM shared.place_photo WHERE id = $1::uuid", photo)
        await c.execute("DELETE FROM acp_shared.place_card WHERE card_id = $1::uuid", card)
        await c.execute("DELETE FROM geo.place_name WHERE destination_id = ANY($1::uuid[]) OR display_name LIKE $2",
                        ids, f"%{tag}")
        await c.execute("DELETE FROM geo.tour_day WHERE source_id = $1", group)
        await c.execute("DELETE FROM geo.tour_place WHERE source_id = $1", group)
        await c.execute("UPDATE shared.destinations SET duplicate_of = NULL WHERE id = ANY($1::uuid[])", ids)
        await c.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[]) OR name LIKE $2", ids, f"%{tag}")
        await c.execute("DELETE FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid", tour_id)
        await c.execute("DELETE FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid", tour_id)
        await c.execute("DELETE FROM shared.job WHERE created_by = $1", actor)
    await delete_audit_rows(pool, "actor = $1", actor)
    await pool.close()


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


async def _fix(env, **body):
    return await admin_places.validity_fix(admin_places.ValidityFixRequest(**body), _request(env.pool),
                                           x_admin_secret=SECRET, x_admin_user_id=env.actor.split(":", 1)[1])


# ── the report ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _mine(env, items):
    return [i for i in items if i.source_id == env.group]


async def test_report_finds_each_defect_with_its_reason(env):
    async with env.pool.acquire() as c:
        summary, items = await validity.report(c, env.country)
    mine = _mine(env, items)
    got = sorted((i.check, i.reason) for i in mine)
    assert ("same_place", "near_duplicate") in got
    assert ("not_a_pin", "linear_feature") in got
    assert ("not_a_pin", "area_feature") in got
    dup = next(i for i in mine if i.reason == "near_duplicate")
    assert dup.keeper_id == env.d["keep"] and set(dup.destination_ids) == {env.d["keep"], env.d["dup"]}
    assert summary["by_country"]["Laos"]["tours"] >= 1 and summary["by_country"]["Laos"]["tours_flagged"] >= 1

    out = await admin_places.validity_report(_request(env.pool), country=env.country, check="not_a_pin",
                                             page=1, page_size=200, x_admin_secret=SECRET)
    assert {i["reason"] for i in out["items"] if i["source_id"] == env.group} == {"linear_feature", "area_feature"}
    assert out["total"] >= 2 and out["page_size"] == 200
    one = await admin_places.validity_report(_request(env.pool), country=env.country, check="not_a_pin",
                                             page=1, page_size=1, x_admin_secret=SECRET)
    assert len(one["items"]) == 1 and one["total"] == out["total"]
    assert out["attribution"]["geoapify"] == "Powered by Geoapify"
    assert "OpenStreetMap" in out["attribution"]["osm"]
    assert out["summary"]["catalog"]["checks"]["not_a_pin"]["items"] == out["total"]
    with pytest.raises(HTTPException) as e:
        await admin_places.validity_report(_request(env.pool), check="nope", x_admin_secret=SECRET)
    assert e.value.status_code == 422


async def test_coverage_lists_the_country(env):
    out = await admin_places.coverage(_request(env.pool), x_admin_secret=SECRET)
    row = next(r for r in out["countries"] if r["country"] == "Laos")
    assert row["live"] >= 1 and row["in_graph"] <= row["live"]
    assert out["totals"]["live"] >= 1


# ── duplicate_place: dry run, apply, store lookup, undo ────────────────────────────────────────────────────────────

async def _holders(c, dest):
    return {
        "tour_place": await c.fetchval("SELECT count(*) FROM geo.tour_place WHERE destination_id = $1::uuid", dest),
        "tour_day": await c.fetchval("SELECT count(*) FROM geo.tour_day WHERE overnight_destination_id = $1::uuid",
                                     dest),
        "place_name": await c.fetchval("SELECT count(*) FROM geo.place_name WHERE destination_id = $1::uuid", dest),
        "place_card": await c.fetchval("SELECT count(*) FROM acp_shared.place_card WHERE destination_id = $1::uuid",
                                       dest),
        "place_photo": await c.fetchval("SELECT count(*) FROM shared.place_photo WHERE destination_id = $1::uuid",
                                        dest),
    }


async def test_duplicate_merge_dry_run_apply_lookup_and_undo(env):
    dup, keep = env.d["dup"], env.d["keep"]
    scope = {"check": "duplicate_place", "country": env.country, "source_id": env.group}
    dry = await _fix(env, **scope)
    assert dry["dry_run"] is True and len(dry["changes"]) == 1
    ch = dry["changes"][0]
    assert ch["destination_id"] == dup and ch["keeper_id"] == keep
    assert {t: len(v) for t, v in ch["repointed"].items()} == {
        "geo.tour_place": 1, "geo.tour_day": 1, "geo.place_name": 1, "acp_shared.place_card": 1,
        "shared.place_photo": 1}
    assert set(dry["cross_app_references"]) == set(validity_fix.CROSS_APP_TABLES)
    async with env.pool.acquire() as c:
        assert await c.fetchval("SELECT duplicate_of FROM shared.destinations WHERE id = $1::uuid", dup) is None
        assert (await _holders(c, dup))["place_card"] == 1

    done = await _fix(env, **scope, dry_run=False)
    assert done["applied"] == 1 and done["audit_id"] and done["graph_job"]
    async with env.pool.acquire() as c:
        assert await c.fetchval("SELECT duplicate_of::text FROM shared.destinations WHERE id = $1::uuid", dup) == keep
        assert set((await _holders(c, dup)).values()) == {0}
        held = await _holders(c, keep)
        assert held["tour_place"] == 2 and held["tour_day"] == 1 and held["place_name"] == 1
        assert held["place_card"] == 1 and held["place_photo"] == 1
        audit = await c.fetchrow("SELECT action, actor, tenant_id, details FROM acp_shared.audit_log "
                                 "WHERE id = $1::uuid", done["audit_id"])
        assert audit["action"] == "places.validity_fix.duplicate_place" and audit["actor"] == env.actor
        assert audit["tenant_id"] is None
        details = json.loads(audit["details"]) if isinstance(audit["details"], str) else audit["details"]
        assert details["changes"][0]["before"] == {"duplicate_of": None}
        job = await c.fetchrow("SELECT kind FROM shared.job WHERE id = $1::uuid", done["graph_job"])
        assert job["kind"] == "tour_graph_build"
        # the report no longer sees the pair: the merged row is not usable any more
        _, items = await validity.report(c, env.country)
        assert not [i for i in _mine(env, items) if i.reason == "near_duplicate"]
        # the store lookup of the merged spelling answers the keeper: no geocode, no new row
        assert (await store.known_destinations(c, [env.dup_key]))[env.dup_key] == keep
        n_before = await c.fetchval("SELECT count(*) FROM shared.destinations WHERE name LIKE $1", f"%{env.tag}")
        got = await store.upsert_destination(c, env.dup_key, f"Tat Kuang Si {env.tag}",
                                             Located(19.7480, 101.9925, env.country, "geoapify"))
        assert got == (keep, "kept")
        assert await c.fetchval("SELECT count(*) FROM shared.destinations WHERE name LIKE $1",
                                f"%{env.tag}") == n_before

    dry_undo = await _fix(env, undo=done["audit_id"])
    assert dry_undo == {"undo_of": done["audit_id"], "check": "duplicate_place", "restored": 1, "dry_run": True}
    undone = await _fix(env, undo=done["audit_id"], dry_run=False)
    assert undone["restored"] == 1 and undone["audit_id"]
    async with env.pool.acquire() as c:
        assert await c.fetchval("SELECT duplicate_of FROM shared.destinations WHERE id = $1::uuid", dup) is None
        assert await _holders(c, dup) == {"tour_place": 1, "tour_day": 1, "place_name": 1, "place_card": 1,
                                          "place_photo": 1}
        assert (await _holders(c, keep))["tour_place"] == 1
    with pytest.raises(HTTPException) as e:
        await _fix(env, undo=done["audit_id"], dry_run=False)
    assert e.value.status_code == 409


# ── not_a_pin / linear_feature ──────────────────────────────────────────────────────────────────────────────────────

async def _dest_row(c, dest):
    return dict(await c.fetchrow("SELECT geo_precision, geo_status, review_reason FROM shared.destinations "
                                 "WHERE id = $1::uuid", dest))


async def test_area_and_linear_fixes_and_undo(env):
    area = await _fix(env, check="not_a_pin", country=env.country, source_id=env.group, dry_run=False)
    assert [c["destination_id"] for c in area["changes"]] == [env.d["plateau"]]
    linear = await _fix(env, check="linear_feature", country=env.country, source_id=env.group, dry_run=False)
    assert [c["destination_id"] for c in linear["changes"]] == [env.d["trail"]]
    async with env.pool.acquire() as c:
        assert await _dest_row(c, env.d["plateau"]) == {"geo_precision": "area", "geo_status": "ok",
                                                        "review_reason": None}
        assert await _dest_row(c, env.d["trail"]) == {"geo_precision": "area", "geo_status": "suspect",
                                                      "review_reason": "linear_feature"}
        _, items = await validity.report(c, env.country)
        assert not [i for i in _mine(env, items) if i.check == "not_a_pin"]
    await _fix(env, undo=linear["audit_id"], dry_run=False)
    async with env.pool.acquire() as c:
        assert await _dest_row(c, env.d["trail"]) == {"geo_precision": "point", "geo_status": "ok",
                                                      "review_reason": None}


async def test_fix_refuses_an_unknown_check_and_a_bad_undo_id(env):
    for body in ({"check": "nope"}, {"undo": "not-a-uuid"}):
        with pytest.raises(HTTPException) as e:
            await _fix(env, **body)
        assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await _fix(env, undo=str(uuid.uuid4()))
    assert e.value.status_code == 404
