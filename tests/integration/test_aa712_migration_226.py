"""AA-712 — migration 226 (schema geo, destinations key) and the place pipeline against real Postgres.

Lesson S236 (as test_aa679): conftest applies migrations as a SUPERUSER, which bypasses RLS and owns
everything; Dev RDS has neither. The module fixture undoes the superuser-applied 226, seeds legacy
shared.destinations rows (duplicates under the new key, a blank country), hands the objects 226 alters
to a NOSUPERUSER NOBYPASSRLS runner and applies 226 as that runner — twice (idempotent), errors NOT
swallowed. The pipeline tests then run the job logic on an asyncpg pool AS THE RUNNER (FORCE RLS on
published_tours / raw_tours applies), with fake model / embedding / geocoder calls.
"""
import os
import sys
import uuid

import asyncpg
import psycopg2
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz712_rds_runner"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "226_geo_place_pipeline.sql")
RUNNER_TABLES = ("shared.destinations", "shared.llm_role_config", "shared.spend_budget", "shared.schema_versions",
                 "acp_shared.place_card")
READ_TABLES = ("gold_aa_internal.published_tours", "silver_aa_internal.raw_tours", "acp_contract.tour_atoms",
               "acp_contract.v_active_tour_atoms",
               "acp_shared.atom_place_card", "tripplanner.itinerary_components", "tripplanner.tour_day")
MASTER = "00000000-0000-0000-0000-000000000001"
SEED_TAG = "zz712"


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _undo_226(a):
    # Migration 228's catalog views read geo.* and the 226 destinations columns (v_destination.geo_status
    # ...), so the columns below cannot be dropped while they exist. The catalog modules re-create the
    # schema the RDS way in their own fixtures (test_aa679 = 225, test_aa712_migration_228 = 225 + 228).
    a.execute("""
        DROP SCHEMA IF EXISTS catalog CASCADE;
        DROP SCHEMA IF EXISTS geo CASCADE;
        ALTER TABLE shared.destinations DROP COLUMN IF EXISTS duplicate_of, DROP COLUMN IF EXISTS geo_status,
            DROP COLUMN IF EXISTS confidence, DROP COLUMN IF EXISTS geocoder_place_id,
            DROP COLUMN IF EXISTS geo_checked_at, DROP COLUMN IF EXISTS name_norm, DROP COLUMN IF EXISTS locality_norm,
            DROP COLUMN IF EXISTS geo_precision, DROP COLUMN IF EXISTS geocoder_provider,
            DROP COLUMN IF EXISTS geocoded_at, DROP COLUMN IF EXISTS geocoder_meta, DROP COLUMN IF EXISTS review_reason;
        ALTER TABLE shared.destinations DROP CONSTRAINT IF EXISTS destinations_located_by_check;
        ALTER TABLE shared.destinations ADD CONSTRAINT destinations_located_by_check
            CHECK (located_by IN ('mapbox', 'llm'));
        DROP INDEX IF EXISTS shared.destinations_lower_name_idx;
        CREATE UNIQUE INDEX IF NOT EXISTS destinations_lower_name_uidx ON shared.destinations (lower(name));
        ALTER TABLE acp_shared.place_card DROP COLUMN IF EXISTS destination_id;
        DELETE FROM shared.llm_role_config WHERE stage IN ('a3_place_days', 'a3_place_locate', 'a3_place_embed');
        DELETE FROM shared.spend_budget
         WHERE provider = 'geocoder' OR scope IN ('job:place_extract', 'job:place_geocode');
        ALTER TABLE shared.spend_budget DROP CONSTRAINT IF EXISTS spend_budget_provider_check;
        ALTER TABLE shared.spend_budget ADD CONSTRAINT spend_budget_provider_check
            CHECK (provider IN ('dfs', 'bedrock', 'openai', 'jev'));
        DELETE FROM shared.schema_versions WHERE version = '226';""")


def _seed_legacy(a):
    """Legacy TripPlanner rows: two spellings of Hoi An (one key now), a blank country used by a
    Bhutan tour, an unlocated row, a same name in two countries (allowed now)."""
    a.execute("DELETE FROM shared.destinations WHERE name LIKE %s", (f"{SEED_TAG}%",))
    rows = [("Hội An", "Vietnam", "llm"), ("Hoi-An", "Vietnam", "mapbox"), ("Paro", "", "mapbox"),
            ("Old Quarter", "Vietnam", None), ("Lost Place", "", None)]
    ids = {}
    for name, country, by in rows:
        a.execute("INSERT INTO shared.destinations (name, country, lat, lng, located_by) "
                  "VALUES (%s, %s, 10, 100, %s) RETURNING id", (f"{SEED_TAG} {name}", country, by))
        ids[name] = a.fetchone()[0]
    # TripPlanner tables (frozen) say Paro is used by a Bhutan tour
    a.execute("""CREATE SCHEMA IF NOT EXISTS tripplanner;
                 CREATE TABLE IF NOT EXISTS tripplanner.itinerary_components (destination_id uuid, source_tour_id text);
                 CREATE TABLE IF NOT EXISTS tripplanner.tour_day
                     (overnight_destination_id uuid, source_tour_id text);""")
    bt = str(uuid.uuid4())
    a.execute("SET app.tenant_id = %s", (MASTER,))
    a.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country)
                 VALUES (%s, %s, %s, 'zz712 bhutan', 'Bhutan')""", (bt, MASTER, BATCH_ID))
    a.execute("RESET app.tenant_id")
    a.execute("INSERT INTO tripplanner.itinerary_components VALUES (%s, %s)", (ids["Paro"], bt))
    return ids


@pytest.fixture(scope="module", autouse=True)
def rds_like_226():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    _undo_226(a)
    ids = _seed_legacy(a)
    a.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{RUNNER}'")
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")
    a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS CREATEROLE INHERIT", (DB_PASS,))
    a.execute(f"GRANT CREATE ON DATABASE {DB_NAME} TO {RUNNER}")
    a.execute(f"GRANT USAGE ON SCHEMA shared, acp_shared, acp_contract, gold_aa_internal, silver_aa_internal, "
              f"tripplanner TO {RUNNER}")
    for schema in ("shared", "acp_shared"):          # aa_cis_admin owns them on RDS
        a.execute(f"ALTER SCHEMA {schema} OWNER TO {RUNNER}")
    for t in RUNNER_TABLES:
        a.execute(f"ALTER TABLE {t} OWNER TO {RUNNER}")
    for t in READ_TABLES:
        a.execute(f"GRANT SELECT ON {t} TO {RUNNER}")
    a.execute(f"GRANT SELECT, INSERT, UPDATE ON shared.job TO {RUNNER}")
    # CIS 279 (AA-851, replayed part A): the country_iso2 trigger on shared.destinations resolves the country as the
    # writer, so a writer needs what 279 gives cis_app (SELECT on shared.countries, EXECUTE on the resolver).
    a.execute("SELECT to_regprocedure('shared.country_iso2(text)') IS NOT NULL")
    if a.fetchone()[0]:
        a.execute(f"GRANT SELECT ON shared.countries TO {RUNNER}")
        a.execute(f"GRANT EXECUTE ON FUNCTION shared.country_iso2(text) TO {RUNNER}")

    runner = _connect(RUNNER)
    runner.autocommit = True
    runner.notices = []
    try:
        sql = open(MIGRATION, encoding="utf-8").read()
        runner.cursor().execute(sql)          # raises on any error
        runner.cursor().execute(sql)          # idempotent re-run
        notices = list(runner.notices)
    finally:
        runner.close()
    try:
        yield {"ids": ids, "notices": notices}
    finally:
        a.execute("DELETE FROM shared.destinations WHERE name LIKE %s AND duplicate_of IS NOT NULL", (f"{SEED_TAG}%",))
        a.execute("DELETE FROM geo.graph_build; DELETE FROM geo.tour_day; DELETE FROM geo.tour_place; "
                  "DELETE FROM geo.place_name; DELETE FROM geo.place_outlier; DELETE FROM geo.tour_build;")
        a.execute("DELETE FROM shared.destinations WHERE name LIKE %s", (f"{SEED_TAG}%",))
        a.execute("DROP SCHEMA IF EXISTS tripplanner CASCADE")
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")
        admin.close()


@pytest.fixture
def cur():
    conn = _connect()
    conn.autocommit = True
    c = conn.cursor()
    yield c
    conn.close()


# ── migration ──────────────────────────────────────────────────────────────────────────────────

def test_226_registered_and_tables_exist(cur):
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '226'")
    assert cur.fetchone(), "migration 226 did not register"
    for t in ("tour_build", "tour_day", "tour_place", "place_name", "place_outlier", "geocode_call", "graph_build",
              "tour_stop", "graph_node", "graph_edge", "tour_leg", "tour_junction"):
        cur.execute("SELECT to_regclass(%s)", (f"geo.{t}",))
        assert cur.fetchone()[0], f"geo.{t} missing"
    cur.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'geo'")
    assert cur.fetchone()[0] == RUNNER


def test_sql_name_norm_equals_python(cur):
    from services.places.names import name_norm
    samples = ["Hội An", "Hoi-An", "The Old Quarter", "Điện Biên Phủ", "Khövsgöl Nuur", "Xi’an", "Straße",
               "ÆRØ", "the", "A an the Zoo", "Mỹ Sơn", "Œuvre  d'Art", "Çatalhöyük", "São Paulo", "Ŧest",
               "Phố cổ Hà Nội", "Bagan (Pagan)", "  ", "Ngọc Sơn Temple, Hoàn Kiếm Lake"]
    for s in samples:
        cur.execute("SELECT geo.name_norm(%s)", (s,))
        assert cur.fetchone()[0] == name_norm(s), s


def test_destinations_key_dedupe_and_blank_country(cur, rds_like_226):
    ids = rds_like_226["ids"]
    cur.execute("SELECT name, country, name_norm, geo_status, duplicate_of FROM shared.destinations "
                "WHERE id = ANY(%s::uuid[])", ([str(v) for v in ids.values()],))
    rows = {r[0].replace(f"{SEED_TAG} ", ""): r for r in cur.fetchall()}
    # the Mapbox-located spelling is kept, the other points at it
    assert rows["Hoi-An"][4] is None and rows["Hội An"][4] == ids["Hoi-An"]
    assert rows["Hội An"][3] == "suspect" and rows["Hoi-An"][2] == f"{SEED_TAG} hoi an"
    # blank country filled from the tour that used it; the unused one is suspect
    assert rows["Paro"][1] == "Bhutan" and rows["Paro"][3] == "ok"
    assert rows["Lost Place"][1] == "" and rows["Lost Place"][3] == "suspect"
    # the same place key in another country is allowed (the name itself must still be globally
    # unique while TripPlanner's lower(name) index is kept — review finding 1)
    cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, name_norm) VALUES "
                "(%s, 'Bhutan', 1, 1, %s) RETURNING id", (f"{SEED_TAG} Hoi An", f"{SEED_TAG} hoi an"))
    assert cur.fetchone()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, name_norm) VALUES "
                    "(%s, 'vietnam', 1, 1, %s)", (f"{SEED_TAG} Hoi An 2", f"{SEED_TAG} hoi an"))
    cur.execute("SELECT to_regclass('shared.destinations_lower_name_uidx')")
    assert cur.fetchone()[0] is not None


def test_tripplanner_writers_still_work_after_226(cur):
    # review finding 1: TripPlanner's live writers upsert ON CONFLICT (lower(name)) until slice 6
    tp_upsert = ("INSERT INTO shared.destinations (name, country, lat, lng, located_by, located_at) "
                 "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (lower(name)) DO UPDATE SET country = "
                 "EXCLUDED.country, lat = EXCLUDED.lat, lng = EXCLUDED.lng, located_by = EXCLUDED.located_by, "
                 "located_at = now() RETURNING id")
    cur.execute(tp_upsert, (f"{SEED_TAG} Tp Place", "Laos", 19.9, 102.1, "llm"))
    first = cur.fetchone()[0]
    cur.execute(tp_upsert, (f"{SEED_TAG} TP PLACE", "Laos", 19.8, 102.2, "mapbox"))
    assert cur.fetchone()[0] == first
    cur.execute("INSERT INTO shared.destinations (name, country, lat, lng) VALUES (%s, 'Laos', 1, 1) "
                "ON CONFLICT (lower(name)) DO UPDATE SET name = shared.destinations.name RETURNING id",
                (f"{SEED_TAG} tp place",))
    assert cur.fetchone()[0] == first
    cur.execute("DELETE FROM shared.destinations WHERE id = %s", (first,))


@pytest.mark.asyncio
async def test_upsert_destination_with_the_global_name_index(pool, cur):
    from services.places import store
    from services.places.locate import Located
    # a row TripPlanner wrote after 226 (no name_norm), same country: CIS adopts it
    cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, located_by) "
                "VALUES ('Zz712 Pakbeng', 'Laos', 19.89, 101.13, 'llm') RETURNING id")
    tp_row = str(cur.fetchone()[0])
    async with pool.acquire() as conn:
        async with conn.transaction():
            dest, action = await store.upsert_destination(
                conn, ("zz712 pakbeng", "laos", ""), "Zz712 Pakbeng",
                Located(19.9, 101.14, "Laos", "geoapify", "pid",
                        meta={"provider": "geoapify", "result_type": "city", "confidence": 0.0}))
    assert (dest, action) == (tp_row, "updated")
    cur.execute("SELECT name_norm, locality_norm, located_by, geo_precision, geocoder_provider, "
                "geocoded_at IS NOT NULL, geocoder_meta->>'result_type', review_reason "
                "FROM shared.destinations WHERE id = %s", (tp_row,))
    assert cur.fetchone() == ("zz712 pakbeng", "", "geoapify", "point", "geoapify", True, "city", None)
    # the same name is another place in another country: a qualified name, the caller's
    # transaction survives the clash (savepoint)
    async with pool.acquire() as conn:
        async with conn.transaction():
            dest2, action2 = await store.upsert_destination(conn, ("zz712 pakbeng", "thailand", ""),
                                                            "Zz712 Pakbeng", Located(15.0, 100.0, "Thailand", "llm"))
            assert await conn.fetchval("SELECT 1") == 1
    assert action2 == "inserted" and dest2 != tp_row
    cur.execute("SELECT name, name_norm, country FROM shared.destinations WHERE id = %s", (dest2,))
    assert cur.fetchone() == ("Zz712 Pakbeng (Thailand)", "zz712 pakbeng", "Thailand")
    # a generic name keeps its plain name; the locality lives in locality_norm (finding 8)
    async with pool.acquire() as conn:
        dest3, _ = await store.upsert_destination(conn, ("zz712 base camp", "nepal", "namche"), "Zz712 Base Camp",
                                                  Located(27.8, 86.7, "Nepal", "llm",
                                                          meta={"provider": "geoapify", "outcome": "skipped_generic"},
                                                          review_reason="unconfirmed_hard_type"),
                                                  locality="Namche")
        dest4, _ = await store.upsert_destination(conn, ("zz712 base camp", "nepal", "lukla"), "Zz712 Base Camp",
                                                  Located(27.7, 86.73, "Nepal", "llm"), locality="Lukla")
    cur.execute("SELECT id::text, name FROM shared.destinations WHERE id = ANY(%s::uuid[])", ([dest3, dest4],))
    assert dict(cur.fetchall()) == {dest3: "Zz712 Base Camp", dest4: "Zz712 Base Camp (Lukla)"}
    cur.execute("SELECT located_by, geo_precision, geocoder_provider, review_reason FROM shared.destinations "
                "WHERE id = %s", (dest3,))
    assert cur.fetchone() == ("llm", "area", "geoapify", "unconfirmed_hard_type")
    cur.execute("DELETE FROM shared.destinations WHERE id = ANY(%s::uuid[])", ([tp_row, dest2, dest3, dest4],))


@pytest.mark.asyncio
async def test_outlier_observations_are_replaced_and_pruned(pool, cur):
    from services.places import store
    cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, located_by, name_norm) "
                "VALUES ('Zz712 Far', 'Laos', 19.0, 102.0, 'mapbox', 'zz712 far') RETURNING id")
    d = str(cur.fetchone()[0])
    async with pool.acquire() as conn:
        await store.record_outliers(conn, [(d, "zz712-s1", 1, 900.0), (d, "zz712-s2", 2, 800.0)],
                                    evaluated_sources=["zz712-s1", "zz712-s2"])
        await store.record_outliers(conn, [], evaluated_sources=["zz712-s1"])   # re-confirmed: no longer
        cur.execute("SELECT source_id FROM geo.place_outlier WHERE destination_id = %s", (d,))
        assert [r[0] for r in cur.fetchall()] == ["zz712-s2"]
        assert await store.prune_outliers(conn, []) == 0                        # empty live set: no wipe
        assert await store.prune_outliers(conn, ["zz712-s1"]) >= 1              # s2 not live any more
    cur.execute("SELECT count(*) FROM geo.place_outlier WHERE destination_id = %s", (d,))
    assert cur.fetchone()[0] == 0
    cur.execute("DELETE FROM shared.destinations WHERE id = %s", (d,))


def test_stage_keys_budgets_and_constraints(cur):
    cur.execute("SELECT stage, role, model_id, shadow_model_id, shadow_sample_pct FROM shared.llm_role_config "
                "WHERE stage LIKE 'a3_place_%%' AND stage <> 'a3_place_card' AND stage <> 'a3_place_card_check' "
                "ORDER BY stage")
    assert cur.fetchall() == [("a3_place_days", "validate", "sonnet", "gpt-5.6-luna", 20),
                              ("a3_place_embed", "embed", "cohere-embed-v4", None, 0),
                              ("a3_place_locate", "validate", "sonnet", None, 0)]
    cur.execute("SELECT provider, scope, per_run_usd::float, per_day_usd::float FROM shared.spend_budget "
                "WHERE provider = 'geocoder' OR scope LIKE 'job:place_%%' ORDER BY 1, 2")
    assert cur.fetchall() == [("bedrock", "job:place_extract", 5.0, 40.0), ("bedrock", "job:place_geocode", 3.0, None),
                              ("geocoder", "global", None, 25.0), ("geocoder", "job:place_geocode", 50.0, None)]
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, located_by) "
                    "VALUES ('zz712 x', 'Laos', 1, 1, 'guess')")
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'acp_shared' "
                "AND table_name = 'place_card' AND column_name = 'destination_id'")
    assert cur.fetchone()


def test_no_grant_to_other_apps(cur):
    for role in ("tripplanner", "booking", "aa_app_user"):
        cur.execute("SELECT has_schema_privilege(%s, 'geo', 'USAGE')", (role,))
        assert cur.fetchone()[0] is False, role


# ── pipeline as the non-bypass runner ──────────────────────────────────────────────────────────

ITIN = ("Day 1 — Arrival in Hanoi\nWelcome dinner in the Old Quarter. Overnight in Hanoi.\n\n"
        "Day 2 — Hanoi to Ha Long Bay\nCruise among the karsts.\n\n"
        "Day 3 — Departure\nTransfer to the airport for your flight.")


def _seed_tour(c, *, name, group=None, source_status="active", itinerary=ITIN, atoms=()):
    tour_id = str(uuid.uuid4())
    c.execute("SET app.tenant_id = %s", (MASTER,))
    c.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                     source_group_id, source_status) VALUES (%s, %s, %s, %s, 'Vietnam', %s, %s)""",
              (tour_id, MASTER, BATCH_ID, name, group or tour_id, source_status))
    c.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name,
                     aa_summary, aa_highlights, aa_itineraries, master_status)
                 VALUES (%s, %s, %s, %s, 'S', '[]'::jsonb, %s, 'active')""",
              (tour_id, str(uuid.uuid4()), MASTER, name, itinerary))
    c.execute("RESET app.tenant_id")
    for day, place, at in atoms:
        c.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place, activity_type,
                         deleted, is_empty_marker) VALUES (%s, %s, %s, %s, %s, %s, false, false)""",
                  (f"zz712-{uuid.uuid4()}", tour_id, f"{place} — a visit", day, place, at))
    return tour_id


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(f"postgresql://{RUNNER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
                                  min_size=1, max_size=4)
    yield p
    await p.close()


class _Llm:
    def __init__(self, days_answer=None, locate_answer=None):
        self.days_answer, self.locate_answer, self.calls = days_answer, locate_answer, []

    async def days(self, system, user):
        self.calls.append("days")
        return self.days_answer

    async def locate(self, system, user):
        self.calls.append("locate")
        return self.locate_answer


async def _embed(texts):
    return [[0.01] * 1536 for _ in texts]


@pytest.mark.asyncio
async def test_extract_geocode_relink_graph_end_to_end(pool, cur):
    from services.places import pipeline, store
    from services.places.geocoder import ModelOnlyGeocoder

    group = str(uuid.uuid4())
    tour_id = _seed_tour(cur, name="zz712 Vietnam", group=group,
                         atoms=[(1, "Old Quarter", "culture"), (1, "Hoan Kiem Lake", "culture"),
                                (2, "Ha Long Bay", "other"), (2, "Cruise Junk Hotel", "stay"),
                                (3, "Noi Bai Airport", "transit")])
    async with pool.acquire() as conn:
        tour = await store.live_tour(conn, tour_id)            # FORCE RLS: works via set_config
    assert tour and tour["source_id"] == group

    llm = _Llm(days_answer='{"days": [{"day": 1, "overnight": "Hanoi"}, '
                           '{"day": 2, "start": "Hanoi", "end": "Ha Long Bay", "overnight": "Ha Long Bay"}, '
                           '{"day": 3, "overnight": null}]}')
    deps = pipeline.ExtractDeps(generate_days=llm.days, embed=_embed)
    res = await pipeline.extract_tour_places(pool, tour, deps, job_id=None)
    assert res["status"] == "ok", res
    assert llm.calls == ["days"]                               # day 2 had no overnight from the rules
    assert res["components"] == 3 and res["skipped"] == {"logistics_place": 2}
    assert res["names_new_pending"] >= 3

    cur.execute("SELECT day_index, overnight_place, anchor_place, overnight_destination_id FROM geo.tour_day "
                "WHERE source_id = %s ORDER BY 1", (group,))
    assert [r[:3] for r in cur.fetchall()] == [(1, "Hanoi", "Hanoi"), (2, "Ha Long Bay", "Ha Long Bay"),
                                               (3, None, None)]
    cur.execute("SELECT count(*), count(*) FILTER (WHERE destination_id IS NULL) FROM geo.tour_place "
                "WHERE source_id = %s", (group,))
    assert cur.fetchone() == (3, 3)

    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'geo' "
                "AND table_name = 'tour_place' AND column_name = 'embedding')")
    if cur.fetchone()[0]:      # pgvector present (Dev); the CI image has none and skips the column
        cur.execute("SELECT count(*) FROM geo.tour_place WHERE source_id = %s AND embedding IS NOT NULL", (group,))
        assert cur.fetchone()[0] == 3 and res["embeddings_new"] == 3

    # unchanged inputs: no model call, nothing rewritten
    llm.calls.clear()
    again = await pipeline.extract_tour_places(pool, tour, deps)
    assert again["status"] == "skipped_unchanged" and llm.calls == []

    # geocode (model only): one locate call for every pending name
    # "Old Quarter" is a generic name: located as "Old Quarter (Hanoi)", the night's town (design §4)
    loc = _Llm(locate_answer='{"Old Quarter (Hanoi)": [21.034, 105.85, "Vietnam"], "Hoan Kiem Lake": [21.029, 105.852, '
                             '"Vietnam"], "Ha Long Bay": [20.91, 107.18, "Vietnam"], "Hanoi": [21.03, 105.85, '
                             '"Vietnam"]}')
    geo = await pipeline.geocode_pending(pool, loc.locate, ModelOnlyGeocoder(), limit=100)
    assert loc.calls == ["locate"] and geo["resolved"] >= 4 and geo["by"] == {"llm": geo["inserted"]}
    assert geo["relinked"]["places"] == 3 and geo["relinked"]["nights"] == 2
    cur.execute("SELECT count(*) FROM geo.tour_place WHERE source_id = %s AND destination_id IS NULL", (group,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT d.located_by, d.geo_status, d.name_norm FROM shared.destinations d JOIN geo.place_name p "
                "ON p.destination_id = d.id WHERE p.name_norm = 'ha long bay'")
    assert cur.fetchone() == ("llm", "ok", "ha long bay")

    # graph: built from live sources, swapped in, only 3 builds kept
    for _ in range(4):
        g = await pipeline.build_tour_graph(pool)
    assert g["status"] == "ok" and g["stops"] >= 2 and g["edges"] >= 1
    cur.execute("SELECT count(*), count(*) FILTER (WHERE is_current) FROM geo.graph_build")
    assert cur.fetchone() == (3, 1)
    cur.execute("SELECT count(*) FROM geo.tour_stop s JOIN geo.graph_build b USING (build_id) "
                "WHERE b.is_current AND s.source_id = %s", (group,))
    assert cur.fetchone()[0] == 2


@pytest.mark.asyncio
async def test_extract_refuses_a_version_that_is_no_longer_live(pool, cur):
    from services.places import pipeline, store
    group = str(uuid.uuid4())
    old = _seed_tour(cur, name="zz712 old", group=group, atoms=[(1, "Hue Citadel", "culture")])
    async with pool.acquire() as conn:
        tour = await store.live_tour(conn, old)
    # set-active of a newer version happens while the old version's job is running
    new = _seed_tour(cur, name="zz712 new", group=group, source_status="superseded",
                     atoms=[(1, "Hue Citadel", "culture")])
    cur.execute("SET app.tenant_id = %s", (MASTER,))
    cur.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'superseded' WHERE tour_id = %s", (old,))
    cur.execute("UPDATE silver_aa_internal.raw_tours SET source_status = 'active' WHERE tour_id = %s", (new,))
    cur.execute("RESET app.tenant_id")
    res = await pipeline.extract_tour_places(pool, tour, pipeline.ExtractDeps(embed=_embed))
    assert res["status"] == "skipped_not_live"
    cur.execute("SELECT count(*) FROM geo.tour_build WHERE source_id = %s", (group,))
    assert cur.fetchone()[0] == 0                              # rolled back, not even a placeholder


@pytest.mark.asyncio
async def test_geocode_failures_stay_pending_then_unknown(pool, cur):
    from services.places import pipeline
    from services.places.geocoder import ModelOnlyGeocoder
    cur.execute("INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country) "
                "VALUES ('zz712 nowhere', 'laos', '', 'Zz712 Nowhere', 'Laos')")
    loc = _Llm(locate_answer='{"Zz712 Nowhere": null}')
    for _ in range(3):
        cur.execute("UPDATE geo.place_name SET next_retry_at = now() WHERE name_norm = 'zz712 nowhere'")
        await pipeline.geocode_pending(pool, loc.locate, ModelOnlyGeocoder(), limit=100)
    cur.execute("SELECT status, tries FROM geo.place_name WHERE name_norm = 'zz712 nowhere'")
    assert cur.fetchone() == ("unknown", 3)


@pytest.mark.asyncio
async def test_manual_destination_is_never_overwritten(pool, cur):
    from services.places import store
    from services.places.locate import Located
    cur.execute("INSERT INTO shared.destinations (name, country, lat, lng, located_by, geo_status, name_norm) "
                "VALUES ('Zz712 Fixed', 'Laos', 19.0, 102.0, 'manual', 'manual', 'zz712 fixed') RETURNING id")
    fixed = str(cur.fetchone()[0])
    async with pool.acquire() as conn:
        dest, action = await store.upsert_destination(conn, ("zz712 fixed", "laos", ""), "Zz712 Fixed",
                                                      Located(20.0, 103.0, "Laos", "mapbox"), force=True)
    assert (dest, action) == (fixed, "kept")
    cur.execute("SELECT lat FROM shared.destinations WHERE id = %s", (fixed,))
    assert cur.fetchone()[0] == 19.0
    cur.execute("DELETE FROM shared.destinations WHERE id = %s", (fixed,))


@pytest.mark.asyncio
async def test_backfill_plan_and_estimate_make_no_call(pool, cur):
    from services.places import backfill
    _seed_tour(cur, name="zz712 backfill", atoms=[(1, "Zz712 Temple", "culture")],
               itinerary="Day 1 — Somewhere\nA day.\n\nDay 2 — Elsewhere\nAnother.\n\nDay 3 — Home\nBye.")
    async with pool.acquire() as conn:
        tours = await backfill.plan(conn, country="Vietnam", limit=50, only_stale=True)
        est = await backfill.estimate(conn, tours, 0.005, False)
        busy = await backfill.wave_busy(conn)
    assert any(t["aa_name"] == "zz712 backfill" for t in tours)
    assert est["day_calls"] >= 1 and est["new_names"] >= 1 and est["geocoder_requests"] == 0
    assert est["expected_cost_usd"] > 0 and isinstance(busy, dict)


@pytest.mark.asyncio
async def test_geocoder_requests_today_counts_the_utc_day(pool, cur):
    from services.places import store
    # AA-847: the guard sums geo.geocode_call.credits (CIS 255); this module re-created geo from 226 alone
    with open(os.path.join(os.path.dirname(MIGRATION), "255_geo_place_shape.sql")) as f:
        cur.execute(f.read())
    cur.execute("DELETE FROM geo.geocode_call WHERE provider = 'zz712geo'")
    cur.execute("INSERT INTO geo.geocode_call (provider, requests, called_at) VALUES "
                "('zz712geo', 7, now()), ('zz712geo', 5, now()), ('zz712geo', 100, now() - interval '2 days')")
    # a Place Details call with full geometry: 1 request, 2 credits (written by 255-aware code)
    cur.execute("INSERT INTO geo.geocode_call (provider, requests, credits, endpoint, called_at) VALUES "
                "('zz712geo', 1, 2, 'place_details', now())")
    async with pool.acquire() as conn:
        assert await store.geocoder_requests_today(conn, "zz712geo") == 14
        assert await store.geocoder_requests_today(conn, "nobody") == 0
    cur.execute("DELETE FROM geo.geocode_call WHERE provider = 'zz712geo'")


def test_located_by_accepts_geoapify_not_amazon(cur):
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'destinations_located_by_check'")
    d = cur.fetchone()[0]
    assert "geoapify" in d and "amazon_location" not in d and "manual" in d
