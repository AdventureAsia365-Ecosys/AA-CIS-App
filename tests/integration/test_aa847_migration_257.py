"""AA-847 Problem 2 — migration 257 (automatic duplicate-place merge storage) applied the way DB Migrate applies a cis/
file since X1 S22, and the merge SQL (services/places/merges.py, services/places/dedupe_run.py) on it.

conftest skips 257 in the replay (its catalog recipe needs 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), re-applies 255
(test_aa712_migration_226 drops schema geo before this module) and applies 257 as db_migrator under the live-owner
switch (errors NOT swallowed), twice. Teardown drops what 257 created, so later modules see the replay state.

  * the migration: registered once; named CHECKs; stage rows (validate, two vendors) and the budget row; the redirect
    view is catalog_owner's, readable by tripplanner / booking, shows merged rows only; the geo tables are cis_app's
    only; CREATE on schema catalog revoked;
  * apply_merge: duplicate_of, every CIS pointer repointed (component, overnight, name key, card, photo), the merged
    row's own members flattened to the new keeper (duplicate_of always names a root), one geo.place_merge row;
  * undo_merge: 409 while a later merge depends on it; restores the pointers, clears duplicate_of, writes a
    `different` pin; a second undo is refused; a name-key collision is refused;
  * repoint_dangling repairs a pointer a stale write left on a merged row;
  * owner decision S246 (1): apply_merge refuses to merge a drawn line into a point keeper (its shape would stay on
    the merged id); the point merges into the line instead, its members flattened;
  * the admin duplicate fix flattens the job's merges into the row it merges (duplicate_of always names a root),
    logs them in geo.place_merge.prior.flattened and its undo restores them;
  * dedupe_run: a fake country (isolated from other modules' rows) — not listed: rules only, the model band unasked;
    listed dry_run: the stubbed two models answer, the verdict is cached (a second run asks nothing), nothing
    written; approved apply: the diff is written with the graph build queued; the breaker holds a country whose
    changes outside the approved diff exceed the limit.
"""
import json
import os
import random
import string
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa860_migration_246 as t246  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.places import dedupe as dd  # noqa: E402
from services.places import dedupe_run, merges  # noqa: E402
import services.jobs  # noqa: E402,F401  (registers the job kinds the run enqueues)

HERE = os.path.dirname(__file__)
MIG = os.path.join(HERE, "..", "..", "api", "migrations")
MIGRATION = os.path.join(MIG, "257_geo_place_dedupe.sql")
MIGRATION_255 = os.path.join(MIG, "255_geo_place_shape.sql")
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
GEO_TABLES = ("geo.place_merge", "geo.place_pair_verdict", "geo.place_dedupe_country")


def _read(path):
    return open(path, encoding="utf-8").read()


def _apply(path):
    c = t246._migrator()
    c.autocommit = True
    c.notices = []
    try:
        c.cursor().execute(t246._switched(_read(path)))     # raises on any error
        return list(c.notices)
    finally:
        c.close()


def undo_257(a):
    a.execute("DROP VIEW IF EXISTS catalog.v_destination_redirect")
    a.execute("DROP TABLE IF EXISTS geo.place_dedupe_country, geo.place_pair_verdict, geo.place_merge")
    a.execute("DELETE FROM shared.llm_role_config WHERE stage IN ('a3_place_dedupe', 'a3_place_dedupe_check')")
    a.execute("DELETE FROM shared.spend_budget WHERE provider = 'bedrock' AND scope = 'job:place_dedupe'")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '257'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_257(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_257(a)
        _apply(MIGRATION_255)                                # idempotent; geo may have been rebuilt by 226's test
        notices = _apply(MIGRATION)
        notices += _apply(MIGRATION)                         # idempotent re-run
        yield {"notices": notices}
    finally:
        undo_257(a)
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


# ── the migration ───────────────────────────────────────────────────────────────────────────────────────────────

def test_registered_once_with_its_checks(cur):
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '257'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT conname FROM pg_constraint
                    WHERE conrelid IN ('geo.place_merge'::regclass, 'geo.place_pair_verdict'::regclass,
                                       'geo.place_dedupe_country'::regclass) AND contype = 'c'""")
    names = {r[0] for r in cur.fetchall()}
    assert {"place_merge_decided_by_chk", "place_merge_undo_chk", "place_pair_verdict_pin_chk",
            "place_pair_verdict_order_chk", "place_dedupe_country_apply_chk"} <= names


def test_stage_rows_and_budget(cur):
    cur.execute("""SELECT stage, role, provider, model_id, account_route FROM shared.llm_role_config
                    WHERE stage IN ('a3_place_dedupe', 'a3_place_dedupe_check') ORDER BY stage""")
    assert cur.fetchall() == [("a3_place_dedupe", "validate", "openai", "gpt-6-luna", "acc3"),
                              ("a3_place_dedupe_check", "validate", "claude", "haiku", "acc3")]
    cur.execute("SELECT per_run_usd, hard_stop FROM shared.spend_budget "
                "WHERE provider = 'bedrock' AND scope = 'job:place_dedupe'")
    r = cur.fetchone()
    assert float(r[0]) == 0.5 and r[1] is True


def test_redirect_view_owner_grants_and_rows(cur):
    cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'catalog.v_destination_redirect'::regclass")
    assert cur.fetchone()[0] == "catalog_owner"
    for role in ("tripplanner", "booking"):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_destination_redirect', 'SELECT')", (role,))
        assert cur.fetchone()[0], role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    tag = uuid.uuid4().hex[:8]
    ids = []
    for name in (f"Redirect keeper {tag}", f"Redirect merged {tag}"):
        cur.execute("""INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, name_norm,
                                                        locality_norm)
                       VALUES (%s, 'Zqland', 1, 1, 'llm', 'ok', geo.name_norm(%s), '') RETURNING id""", (name, name))
        ids.append(cur.fetchone()[0])
    cur.execute("UPDATE shared.destinations SET duplicate_of = %s WHERE id = %s", (ids[0], ids[1]))
    cur.execute("SET LOCAL ROLE tripplanner")
    cur.execute("SELECT destination_id, keeper_id FROM catalog.v_destination_redirect "
                "WHERE destination_id = ANY(%s::uuid[]) OR keeper_id = ANY(%s::uuid[])", (ids, ids))
    assert cur.fetchall() == [(ids[1], ids[0])]


def test_geo_tables_are_cis_internal(cur):
    for t in GEO_TABLES:
        cur.execute("SELECT has_table_privilege('cis_app', %s, 'SELECT, INSERT, UPDATE, DELETE')", (t,))
        assert cur.fetchone()[0], t
        for role in ("tripplanner", "booking", "aa_app_user"):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, t))
            assert not cur.fetchone()[0], (role, t)


# ── merge SQL ───────────────────────────────────────────────────────────────────────────────────────────────────

def _tag():
    return "zq" + "".join(random.choice(string.ascii_lowercase) for _ in range(8))


async def _dest(c, name, country, lat, lng, located_by="geoapify", precision="point", rt="amenity", pid=None):
    meta = json.dumps({"provider": "geoapify", "result_type": rt, "name": name}) if located_by == "geoapify" else None
    return await c.fetchval(
        """INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, geo_precision, name_norm,
                                            locality_norm, geocoder_meta, geocoder_place_id)
           VALUES ($1, $2, $3, $4, $5, 'ok', $6, geo.name_norm($1), '', $7::jsonb, $8) RETURNING id::text""",
        name, country, lat, lng, located_by, precision, meta, pid)


@pytest_asyncio.fixture
async def seeded():
    tag = _tag()
    country = f"Zq{tag}"
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    src, tour = str(uuid.uuid4()), str(uuid.uuid4())
    async with pool.acquire() as c:
        d = {
            "keeper": await _dest(c, f"Kuang Si Falls {tag}", country, 19.7491, 101.9935, pid=f"g-{tag}"),
            "merged": await _dest(c, f"Tat Kuang Si {tag}", country, 19.7480, 101.9925, "llm", "area"),
            "member": await _dest(c, f"Kuang Si Waterfall {tag}", country, 19.7485, 101.9930, "llm", "area"),
            "market": await _dest(c, f"Ben Thanh Market {tag}", country, 10.7725, 106.6980, pid=f"m-{tag}"),
            "bare": await _dest(c, f"Ben Thanh {tag}", country, 10.7726, 106.6981, "llm", "area"),
        }
        comp = str(uuid.uuid4())
        await c.execute(
            """INSERT INTO geo.tour_place (component_id, source_id, tour_id, day_index, name, name_norm, country,
                                           country_norm, locality_norm, destination_id, activity, intensity_level,
                                           season_months, duration_hint, text_extract, text_hash, atom_ids)
               VALUES ($1::uuid, $2, $3::uuid, 1, $4, geo.name_norm($4), $5, lower($5), '', $6::uuid,
                       'cultural_heritage', 'moderate', ARRAY[]::int[], 'full_day', $7, 'h', ARRAY[]::text[])""",
            comp, src, tour, f"Tat Kuang Si {tag}", country, d["merged"], f"Swim at Tat Kuang Si {tag}.")
        await c.execute(
            """INSERT INTO geo.tour_day (source_id, day_index, tour_id, title, overnight_place,
                                         overnight_destination_id, places, extracted_by)
               VALUES ($1, 1, $2::uuid, 'Day 1', $3, $4::uuid, ARRAY[]::text[], 'rule')""",
            src, tour, f"Tat Kuang Si {tag}", d["merged"])
        key = (await c.fetchval("SELECT geo.name_norm($1)", f"Tat Kuang Si {tag}"), country.lower(), "")
        await c.execute(
            """INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country, status,
                                           destination_id)
               VALUES ($1, $2, $3, $4, $5, 'resolved', $6::uuid)""", *key, f"Tat Kuang Si {tag}", country,
            d["merged"])
        card = str(uuid.uuid4())
        await c.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                           VALUES ($1::uuid, $2, $3, $4, $5::uuid)""",
                        card, f"tat-kuang-si-{tag}", f"Tat Kuang Si {tag}", country, d["merged"])
        photo = str(uuid.uuid4())
        await c.execute(
            """INSERT INTO shared.place_photo (id, drive_file_id, drive_root_id, folder_path, country, file_name,
                                               destination_id, match_source, status, s3_key_large)
               VALUES ($1::uuid, $2, 'root', 'x/y', $3, 'a.jpg', $4::uuid, 'auto', 'matched', 'k')""",
            photo, f"zq847-{tag}", country, d["merged"])
    yield dict(pool=pool, tag=tag, country=country, d=d, comp=comp, src=src, key=key, card=card, photo=photo)
    async with pool.acquire() as c:
        ids = list(d.values())
        await c.execute("DELETE FROM shared.job WHERE kind IN ('tour_graph_build', 'place_dedupe') "
                        "AND created_by = 'place_dedupe'")
        await c.execute("DELETE FROM geo.place_dedupe_country WHERE country = $1", country.lower())
        await c.execute("DELETE FROM shared.place_photo WHERE id = $1::uuid", photo)
        await c.execute("DELETE FROM acp_shared.place_card WHERE card_id = $1::uuid", card)
        await c.execute("DELETE FROM geo.place_name WHERE country_norm = $1", country.lower())
        await c.execute("DELETE FROM geo.tour_day WHERE source_id = $1", src)
        await c.execute("DELETE FROM geo.tour_place WHERE source_id = $1", src)
        await c.execute("UPDATE shared.destinations SET duplicate_of = NULL WHERE id = ANY($1::uuid[])", ids)
        await c.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", ids)
    await pool.close()


async def _holders(c, dest):
    return {t: await c.fetchval(f"SELECT count(*) FROM {t} WHERE {col} = $1::uuid", dest)
            for t, _k, col in merges.REPOINT}


@pytest.mark.asyncio
async def test_apply_flatten_and_undo(seeded):
    d = seeded["d"]
    async with seeded["pool"].acquire() as c:
        # an earlier merge: member → merged (merged is a keeper with one member)
        async with c.transaction():
            first = await merges.apply_merge(c, {"merged_id": d["member"], "keeper_id": d["merged"],
                                                 "country": seeded["country"], "decided_by": "rule", "score": 0.9},
                                             created_by="test")
        assert first
        async with c.transaction():
            mid = await merges.apply_merge(c, {"merged_id": d["merged"], "keeper_id": d["keeper"],
                                               "country": seeded["country"], "decided_by": "rule", "score": 0.95,
                                               "features": {"reason": "exact/same_kind"}}, created_by="test")
        assert mid
        assert await _holders(c, d["merged"]) == {t: 0 for t, _k, _c in merges.REPOINT}
        assert all(v == 1 for v in (await _holders(c, d["keeper"])).values())
        dup = {r["id"]: r["duplicate_of"] for r in await c.fetch(
            "SELECT id::text AS id, duplicate_of::text AS duplicate_of FROM shared.destinations "
            "WHERE id = ANY($1::uuid[])", [d["merged"], d["member"]])}
        assert dup == {d["merged"]: d["keeper"], d["member"]: d["keeper"]}     # flattened to the root
        assert await merges.assert_roots(c) == 0
        row = await c.fetchrow("SELECT repointed, prior, decided_by FROM geo.place_merge WHERE id = $1::uuid", mid)
        assert json.loads(row["prior"]) == {"flattened": [d["member"]]} and row["decided_by"] == "rule"
        # the first merge now depends on the second: refused
        with pytest.raises(merges.MergeError) as e:
            async with c.transaction():
                await merges.undo_merge(c, first, actor="test", reason="t")
        assert e.value.status == 409
        async with c.transaction():
            out = await merges.undo_merge(c, mid, actor="test", reason="wrong merge")
        assert out["left"] == {} and sum(out["restored"].values()) == 5
        assert all(v == 1 for v in (await _holders(c, d["merged"])).values())
        dup = {r["id"]: r["duplicate_of"] for r in await c.fetch(
            "SELECT id::text AS id, duplicate_of::text AS duplicate_of FROM shared.destinations "
            "WHERE id = ANY($1::uuid[])", [d["merged"], d["member"]])}
        assert dup == {d["merged"]: None, d["member"]: d["merged"]}
        lo, hi = dd.pair_key(d["merged"], d["keeper"])
        pin = await c.fetchrow("SELECT verdict, source FROM geo.place_pair_verdict "
                               "WHERE lo_id = $1::uuid AND hi_id = $2::uuid AND input_hash = '*'", lo, hi)
        assert dict(pin) == {"verdict": "different", "source": "undo"}
        with pytest.raises(merges.MergeError):
            async with c.transaction():
                await merges.undo_merge(c, mid, actor="test", reason="twice")


@pytest.mark.asyncio
async def test_undo_refuses_a_name_key_collision(seeded):
    d = seeded["d"]
    async with seeded["pool"].acquire() as c:
        async with c.transaction():
            mid = await merges.apply_merge(c, {"merged_id": d["bare"], "keeper_id": d["market"],
                                               "country": seeded["country"], "decided_by": "llm"}, created_by="test")
        other = await _dest(c, f"Ben Thanh {seeded['tag']} copy", seeded["country"], 10.77, 106.69, "llm", "area")
        await c.execute("UPDATE shared.destinations SET name_norm = (SELECT name_norm FROM shared.destinations "
                        "WHERE id = $1::uuid) WHERE id = $2::uuid", d["bare"], other)
        try:
            with pytest.raises(merges.MergeError) as e:
                async with c.transaction():
                    await merges.undo_merge(c, mid, actor="test", reason="t")
            assert e.value.status == 409 and "name key" in json.dumps(e.value.detail)
        finally:
            await c.execute("DELETE FROM shared.destinations WHERE id = $1::uuid", other)


@pytest.mark.asyncio
async def test_repoint_dangling(seeded):
    d = seeded["d"]
    async with seeded["pool"].acquire() as c:
        async with c.transaction():
            await merges.apply_merge(c, {"merged_id": d["merged"], "keeper_id": d["keeper"],
                                         "country": seeded["country"], "decided_by": "rule"}, created_by="test")
        # a stale place_extract persist writes the merged id back after the merge
        await c.execute("UPDATE geo.tour_place SET destination_id = $1::uuid WHERE component_id = $2::uuid",
                        d["merged"], seeded["comp"])
        async with c.transaction():
            out = await merges.repoint_dangling(c)
        assert out.get("geo.tour_place", 0) >= 1
        assert await c.fetchval("SELECT destination_id::text FROM geo.tour_place WHERE component_id = $1::uuid",
                                seeded["comp"]) == d["keeper"]


_LINE = {"type": "LineString", "coordinates": [[102.0, 20.0], [102.05, 20.1], [102.1, 20.2]]}


async def _drawn_line(c, name, country, lat, lng):
    """A line destination the catalog draws: geo_precision 'line' + an ok geo.place_shape row."""
    dest = await _dest(c, name, country, lat, lng, "geoapify", "line", rt="unknown")
    g = json.dumps(_LINE)
    await c.execute(
        """INSERT INTO geo.place_shape (destination_id, kind, status, source, geom, geom_mid, geom_lo, min_lat, min_lng,
                                        max_lat, max_lng, length_km, osm_type, osm_id)
           VALUES ($1::uuid, 'line', 'ok', 'overpass', $2::jsonb, $2::jsonb, $2::jsonb, 20.0, 102.0, 20.2, 102.1, 25.0,
                   'relation', 1)""", dest, g)
    return dest


@pytest.mark.asyncio
async def test_a_job_merge_never_hides_a_drawn_line(seeded):
    """Owner decision S246 (1), review repro: K = a point that already keeps one row, L = the same river drawn as a
    line. apply_merge refuses L → K under the lock (L's shape would stay on the merged id, never drawn); K → L merges,
    K's member is flattened to L, and L keeps its precision and its shape (still drawn)."""
    d, country, tag = seeded["d"], seeded["country"], seeded["tag"]
    async with seeded["pool"].acquire() as c:
        k = await _dest(c, f"Mekong {tag}", country, 20.1, 102.05)
        member = await _dest(c, f"Mekong {tag} pier", country, 20.1001, 102.0501, "llm", "area")
        line = await _drawn_line(c, f"Mekong River {tag}", country, 20.1, 102.05)
        try:
            async with c.transaction():
                assert await merges.apply_merge(c, {"merged_id": member, "keeper_id": k, "country": country,
                                                    "decided_by": "rule"}, created_by="test")
            async with c.transaction():
                refused = await merges.apply_merge(c, {"merged_id": line, "keeper_id": k, "country": country,
                                                       "decided_by": "llm"}, created_by="test")
            assert refused is None
            assert await c.fetchval("SELECT duplicate_of FROM shared.destinations WHERE id = $1::uuid", line) is None
            assert await c.fetchval("SELECT count(*) FROM geo.place_merge WHERE merged_id = $1::uuid", line) == 0
            async with c.transaction():
                mid = await merges.apply_merge(c, {"merged_id": k, "keeper_id": line, "country": country,
                                                   "decided_by": "llm"}, created_by="test")
            assert mid
            dup = {r["id"]: r["duplicate_of"] for r in await c.fetch(
                "SELECT id::text AS id, duplicate_of::text AS duplicate_of FROM shared.destinations "
                "WHERE id = ANY($1::uuid[])", [k, member, line])}
            assert dup == {k: line, member: line, line: None}
            drawn = await c.fetchrow(
                """SELECT d.geo_precision, ps.status FROM shared.destinations d
                     JOIN geo.place_shape ps ON ps.destination_id = d.id WHERE d.id = $1::uuid""", line)
            assert dict(drawn) == {"geo_precision": "line", "status": "ok"}
            assert await merges.assert_roots(c) == 0
        finally:
            await c.execute("UPDATE shared.destinations SET duplicate_of = NULL WHERE id = ANY($1::uuid[])",
                            [k, member, line])
            await c.execute("DELETE FROM shared.destinations WHERE id = ANY($1::uuid[])", [k, member, line])


@pytest.mark.asyncio
async def test_the_admin_duplicate_fix_flattens_job_merges_and_logs_them(seeded):
    """The admin fix merges a row D the job already made a keeper (member → D): the member follows D to the keeper
    (duplicate_of always names a root, single-hop redirects), the admin's geo.place_merge row records it in
    prior.flattened (the job's key), and the fix's undo puts the member back on D."""
    from services.places import validity_fix
    from _audit_cleanup import delete_audit_rows
    d, country = seeded["d"], seeded["country"]
    actor = f"admin:zq847-{seeded['tag']}"
    pool = seeded["pool"]
    try:
        async with pool.acquire() as c:
            async with c.transaction():
                assert await merges.apply_merge(c, {"merged_id": d["member"], "keeper_id": d["merged"],
                                                    "country": country, "decided_by": "rule"}, created_by="test")
            fix = {"check": "duplicate_place", "country": country, "source_id": None, "findings": 1, "skipped": [],
                   "changes": [{"destination_id": d["merged"], "name": "x", "country": country,
                                "keeper_id": d["keeper"], "keeper_name": "k", "before": {"duplicate_of": None},
                                "after": {"duplicate_of": d["keeper"]}, "repointed": {}, "shape": {},
                                "flattened": []}]}
            async with c.transaction():
                done = await validity_fix.apply(c, fix, actor=actor)
            dup = {r["id"]: r["duplicate_of"] for r in await c.fetch(
                "SELECT id::text AS id, duplicate_of::text AS duplicate_of FROM shared.destinations "
                "WHERE id = ANY($1::uuid[])", [d["merged"], d["member"]])}
            assert dup == {d["merged"]: d["keeper"], d["member"]: d["keeper"]}
            assert await merges.assert_roots(c) == 0
            assert done["changes"][0]["flattened"] == [d["member"]]
            row = await c.fetchrow("SELECT decided_by, prior FROM geo.place_merge WHERE id = $1::uuid",
                                   done["merge_ids"][0])
            assert row["decided_by"] == "admin"
            assert json.loads(row["prior"]) == {"shape": {}, "flattened": [d["member"]]}
            async with c.transaction():
                undone = await validity_fix.undo(c, done["audit_id"], actor=actor, dry_run=False)
            assert undone["merges_undone"] == 1
            dup = {r["id"]: r["duplicate_of"] for r in await c.fetch(
                "SELECT id::text AS id, duplicate_of::text AS duplicate_of FROM shared.destinations "
                "WHERE id = ANY($1::uuid[])", [d["merged"], d["member"]])}
            assert dup == {d["merged"]: None, d["member"]: d["merged"]}
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM shared.job WHERE created_by = $1", actor)
        await delete_audit_rows(pool, "actor = $1", actor)


def _stub_models():
    calls = []

    async def gen(system, user):
        calls.append(json.loads(user))
        return json.dumps({"verdict": "same", "confidence": 0.95, "reason": "stub"})
    m = dedupe_run.Models(gen, gen, ["a3_place_dedupe:stub", "a3_place_dedupe_check:stub"])
    m.calls_log = calls
    return m


@pytest.mark.asyncio
async def test_dedupe_run_dry_run_approve_apply(seeded, monkeypatch):
    pool, country, d = seeded["pool"], seeded["country"], seeded["d"]
    ck = country.lower()
    # not listed: rules only, the band is not asked
    m0 = _stub_models()
    r0 = await dedupe_run.run(pool, mode="dry_run", country=country, models=m0)
    c0 = r0["countries"][ck]
    assert c0["status"] == "dry_run" and not m0.calls_log and c0["band_unasked"] >= 1
    assert [d["merged"], d["keeper"]] in c0["diff"] or [d["member"], d["keeper"]] in c0["diff"]
    # listed dry_run: the two models answer (stub), the verdict is cached, nothing is written
    async with pool.acquire() as c:
        await merges.set_country(c, country, "dry_run", actor="test")
    m1 = _stub_models()
    r1 = await dedupe_run.run(pool, mode="apply", country=country, models=m1)
    c1 = r1["countries"][ck]
    assert c1["status"] == "dry_run" and len(m1.calls_log) == 3 * c1["band_asked"] and c1["band_asked"] >= 1
    assert [d["bare"], d["market"]] in c1["diff"]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM geo.place_merge WHERE country = $1", country) == 0
    m2 = _stub_models()
    r2 = await dedupe_run.run(pool, mode="dry_run", country=country, models=m2)
    assert not m2.calls_log and r2["countries"][ck]["diff_hash"] == c1["diff_hash"]   # cached: no new call
    # approve the diff and apply
    async with pool.acquire() as c:
        await merges.set_country(c, country, "apply", actor="test", diff=c1["diff"])
    r3 = await dedupe_run.run(pool, mode="apply", country=country, models=_stub_models())
    c3 = r3["countries"][ck]
    assert c3["status"] == "applied" and c3["outside"] == 0 and c3["applied"] == c1["diff_size"]
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT duplicate_of::text FROM shared.destinations WHERE id = $1::uuid",
                                d["bare"]) == d["market"]
        assert await c.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'tour_graph_build' "
                                "AND created_by = 'place_dedupe' AND status = 'queued'") >= 1
        assert r3["non_root_merges"] == 0
    r4 = await dedupe_run.run(pool, mode="apply", country=country, models=_stub_models())
    assert r4["countries"][ck]["diff_size"] == 0


@pytest.mark.asyncio
async def test_breaker_holds_changes_outside_the_approved_diff(seeded, monkeypatch):
    pool, country = seeded["pool"], seeded["country"]
    async with pool.acquire() as c:
        await merges.set_country(c, country, "apply", actor="test", diff=[])
    monkeypatch.setattr(dd, "BREAKER_MIN", 0)
    r = await dedupe_run.run(pool, mode="apply", country=country, models=_stub_models())
    c = r["countries"][country.lower()]
    assert c["status"] == "held_volume" and c["outside"] >= 1 and c["breaker_limit"] == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM geo.place_merge WHERE country = $1", country) == 0
        await conn.execute("DELETE FROM shared.notifications WHERE event_type = 'place_dedupe_held' "
                           "AND entity_id = $1", country.lower())


@pytest.mark.asyncio
async def test_a_rule_version_change_drops_the_country_to_dry_run(seeded):
    pool, country = seeded["pool"], seeded["country"]
    async with pool.acquire() as c:
        await merges.set_country(c, country, "apply", actor="test", diff=[])
        await c.execute("UPDATE geo.place_dedupe_country SET rule_version = 'r0' WHERE country = $1",
                        country.lower())
    r = await dedupe_run.run(pool, mode="apply", country=country, models=_stub_models())
    assert r["countries"][country.lower()]["status"] == "rule_version_changed"
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT mode, approved_diff_hash, rule_version FROM geo.place_dedupe_country "
                               "WHERE country = $1", country.lower())
    assert dict(row) == {"mode": "dry_run", "approved_diff_hash": None, "rule_version": dd.RULE_VERSION}
