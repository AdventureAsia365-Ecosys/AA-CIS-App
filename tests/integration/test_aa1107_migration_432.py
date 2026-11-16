"""AA-1107 (S282) — migrations 431 + 432: the catalog HTTP places read (catalog.v_tour_place, tour_id = ANY) without a
full acp_contract.tour_atoms scan, without a per-row geo.name_norm filter on geo.tour_place, with the day heading
matched once per chunk (catalog._tour_day_text) and geo.name_norm's ASCII fast path.

431 (expand) is replayed by conftest like any file; test_aa712_migration_226 drops schema geo, so the module fixture
re-applies 255 and 431 (both idempotent). conftest skips 432 (catalog recipe, needs 253 / 256 / 289). Each test runs in
ONE transaction on the RDS-like state of test_aa860_migration_245 (module fixture): 253 / 256 / 257 / 258 / 273 / 289
are applied as cis_owner (DB Migrate's live-owner switch), the tours are seeded, every row of v_tour_place is read as
the catalog reader (aa_app_user), 432 is applied, the checks run, and the transaction is rolled back.

Checks: the same rows, every column, before and after 432 (several tours, multi-day places, a ranged day heading,
spelling and accent variants, a pipeline link through an accent variant, a card, a legacy name match, a line stretch,
a place without coordinates, deleted / empty-marker / blank atoms, a tour that is not live, a live tour without placed
atoms) and over the whole view; catalog._tour_day_text and catalog.v_tour_day unchanged; geo.name_norm gives the same
key before and after on ASCII, accented, typographic-apostrophe, ligature, article and NULL inputs (and the Python
twin agrees); same columns and types; registered once, re-runnable, catalog_owner's, security_barrier, the three
consumer grants; the reader's plan keys the atoms scan and the tour_place lookup by index. All names are invented.
"""
import json
import os
import re
import sys
import uuid

import psycopg2.errors
import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
BEFORE = ("253_catalog_l4_read_model.sql", "256_catalog_line_places.sql", "257_geo_place_dedupe.sql",
          "258_catalog_live_source_key.sql", "273_published_tours_updated_at.sql", "289_catalog_ranged_day_heading.sql")
INDEX = "431_aa1107_tour_place_name_key_idx.sql"
MIGRATION = "432_aa1107_catalog_places_read_fast.sql"
# services/catalog_http/reader.py _DETAIL_SQL["places"] with DETAIL_COND (psycopg2 placeholder)
READER_SQL = ("SELECT source_id, day_number, position, place_name, place_display_name, destination_id, "
              "destination_name, country, lat, lng, precision FROM catalog.v_tour_place "
              "WHERE source_system = 'cis' AND tour_id = ANY(%s::uuid[])")
READER_ORDERED = READER_SQL + " ORDER BY source_id, day_number, position"
ITINERARY_A = (
    "Day 1 — Arrive in Velmora\nWalk up to Quillan Ridge, then on to the Sôrel Falls.\n\n"
    "Day 2 — Velmora to Brask Harbour\nCruise on Lake Merrowin to Brask Harbour.\n\n"
    "Days 3-4 — Kaldor Pass trek\nTwo days on the old pass trail.\n\n"
    "Day 5 — Depart\nTransfer out."
)
ITINERARY_B = "Day 1 — Velmora old town\nA slow day in Velmora.\n\nDay 2 — Depart\nTransfer out."
# geo.name_norm inputs: ASCII (the fast path), accents, ligatures, typographic apostrophe, articles, digits, NULL
NAME_SAMPLES = ["Velmora", "  The Quillan Ridge ", "A Brask-Harbour (north)", "Sôrel Falls", "SÔREL FALLS",
                "Kaldor’s Pass", "Kaldor's Pass", "Æskel Øy", "Straße Merrowin", "Œuvre Bay", "the a an", "The",
                "Ðồng Velm 2", "", "  ", "x" * 300, None, "İzel", "Velmora\tOld\nTown"]
LINE = {"type": "LineString", "coordinates": [[100.1, 20.1], [100.2, 20.2], [100.3, 20.25]]}


@pytest.fixture(scope="module", autouse=True)
def geo_255_431():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 and 431 (idempotent)."""
    c = t245._connect()
    c.autocommit = True
    for name in ("255_geo_place_shape.sql", INDEX):
        with open(os.path.join(MIG, name), encoding="utf-8") as f:
            c.cursor().execute(f.read())
    c.close()


def _body(name):
    sql = open(os.path.join(MIG, name), encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c, name):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body(name))
    c.execute("RESET ROLE")


def _cols(c, rel):
    c.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
              (rel,))
    return c.fetchall()


def _as(c, role, sql, *args):
    c.execute(f"SET LOCAL ROLE {role}")
    c.execute(sql, args)
    out = c.fetchall()
    c.execute("RESET ROLE")
    return out


def _rows(c, tours, role="aa_app_user"):
    return _as(c, role, "SELECT * FROM catalog.v_tour_place WHERE source_system = 'cis' AND tour_id = ANY(%s::uuid[]) "
                        "ORDER BY tour_id, day_number, position", tours)


def _whole(c, role="tripplanner"):
    return _as(c, role, "SELECT * FROM catalog.v_tour_place ORDER BY tour_id, day_number, position")


def _days(c):
    c.execute("SELECT * FROM catalog._tour_day_text ORDER BY tour_id, day_number")
    text = c.fetchall()
    return text, _as(c, "tripplanner", "SELECT * FROM catalog.v_tour_day ORDER BY tour_id, day_number")


def _norms(c):
    c.execute("SELECT s, geo.name_norm(s) FROM unnest(%s::text[]) WITH ORDINALITY AS u(s, i) ORDER BY i",
              (NAME_SAMPLES,))
    return c.fetchall()


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded ids and the rows read before 432."""


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor(cursor_factory=_Cur)
    try:
        c.execute("ALTER TABLE gold_aa_internal.published_tours DROP COLUMN IF EXISTS updated_at")   # Dev before 273
        c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        if not c.fetchone():
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE:
            _apply(c, name)
        c.seeded = s = _seed(c)
        c.cols_before = _cols(c, "catalog.v_tour_place")
        c.rows_before = _rows(c, s["tours"])
        c.whole_before = _whole(c)
        c.reader_before = _as(c, "aa_app_user", READER_ORDERED, s["tours"])
        c.days_before = _days(c)
        c.norm_before = _norms(c)
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tour(c, *, name, itinerary, master_status="active", country="Laos"):
    tour_id = str(uuid.uuid4())
    c.execute("""INSERT INTO silver_aa_internal.raw_tours
                     (tour_id, tenant_id, batch_id, src_name, country, source_group_id, source_status)
                 VALUES (%s, %s, %s, %s, %s, %s, 'active')""",
              (tour_id, t228.MASTER_TENANT, t228.BATCH_ID, name, country, str(uuid.uuid4())))
    c.execute("""INSERT INTO gold_aa_internal.published_tours
                     (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, aa_itineraries, master_status)
                 VALUES (%s, %s, %s, %s, 'Summary', %s, %s)""",
              (tour_id, str(uuid.uuid4()), t228.MASTER_TENANT, name, itinerary, master_status))
    c.execute("SELECT source_key FROM silver_aa_internal.raw_tours WHERE tour_id = %s", (tour_id,))
    return tour_id, c.fetchone()[0]


def _atom(c, tour_id, *, day, place, activity="sightseeing", deleted=False, empty=False):
    atom_id = f"zz1107-{uuid.uuid4()}"
    c.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place, activity_type,
                                                      deleted, is_empty_marker)
                 VALUES (%s, %s, 'atom text', %s, %s, %s, %s, %s)""",
              (atom_id, tour_id, day, place, activity, deleted, empty))
    return atom_id


def _component(c, src, tour, *, day, name, destination_id):
    """t228._component with a Laos country (the norm columns are not what the view compares)."""
    return t228._component(c, src, tour, day=day, name=name, destination_id=destination_id)


def _seed(c):
    tag = uuid.uuid4().hex[:6]
    a, src_a = _tour(c, name=f"Velmora loop {tag}", itinerary=ITINERARY_A)
    b, src_b = _tour(c, name=f"Velmora short {tag}", itinerary=ITINERARY_B)
    retired, src_r = _tour(c, name=f"Retired {tag}", itinerary=ITINERARY_B, master_status="trashed")
    bare, _ = _tour(c, name=f"No places {tag}", itinerary=ITINERARY_B)

    velmora = t228._dest(c, "Velmora", country="Laos", lat=20.11, lng=100.12)
    sorel = t228._dest(c, "Sorel Falls Viewpoint", country="Laos", lat=20.14, lng=100.19)
    quillan = t228._dest(c, f"Quillan Ridge card {tag}", country="Laos", lat=20.13, lng=100.15, located_by="manual")
    brask = t228._dest(c, "Brask Harbour", country="Laos", lat=20.2, lng=100.3)
    merrowin = t228._dest(c, "Lake Merrowin", country="Laos", lat=20.18, lng=100.22, precision="line")
    t228._dest(c, "Kaldor Pass", country="Laos", lat=20.4, lng=100.5, geo_status="suspect")   # never chosen

    # tour A: spelling / accent variants, a ranged heading (days 3-4), a day without text (6)
    for place in ("Velmora", "Velmora", "velmora"):
        _atom(c, a, day=1, place=place)
    _atom(c, a, day=1, place="Quillan Ridge")
    sorel_plain = _atom(c, a, day=1, place="Sorel Falls")            # more frequent spelling...
    _atom(c, a, day=1, place="Sorel Falls")
    _atom(c, a, day=1, place="Sôrel Falls")                          # ...but the day text spells it with the accent
    _atom(c, a, day=1, place="Unnamed Grove", activity="wildlife")  # no destination anywhere: no coordinates
    _atom(c, a, day=1, place="Ghost Quay", deleted=True)             # deleted: no row
    _atom(c, a, day=2, place="Nowhere Cove", empty=True)            # empty marker: no row
    _atom(c, a, day=2, place="   ")                                  # blank: no row
    _atom(c, a, day=2, place="Brask Harbour", activity="boat")
    _atom(c, a, day=2, place="  Lake Merrowin ")                     # trimmed
    _atom(c, a, day=3, place="Kaldor Pass", activity="trekking")
    _atom(c, a, day=4, place="Kaldor Pass", activity="trekking")
    _atom(c, a, day=6, place="Toller Bay")
    # tour B and the not-live tour: the same name on another tour stays apart
    _atom(c, b, day=1, place="Velmora")
    _atom(c, retired, day=1, place="Velmora")
    _atom(c, bare, day=1, place="")

    # the place pipeline: an upper-case accent variant of the chosen spelling links "Sôrel Falls" (name_norm key);
    # an unlinked component and a same-name component on another day do not count; a different spelling never links
    _component(c, src_a, a, day=1, name="SÔREL FALLS", destination_id=sorel)
    _component(c, src_a, a, day=1, name="Velmora", destination_id=None)
    _component(c, src_a, a, day=3, name="Quillan Ridge", destination_id=velmora)
    _component(c, src_a, a, day=2, name="Brask Harbor", destination_id=velmora)
    _component(c, src_b, b, day=1, name="Velmora", destination_id=velmora)
    _component(c, src_r, retired, day=1, name="Velmora", destination_id=velmora)

    # a place card on one Quillan Ridge atom (card destination), and one on a Sorel Falls atom (the pipeline wins)
    card = str(uuid.uuid4())
    c.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                 VALUES (%s, %s, %s, 'Laos', %s)""", (card, f"quillan-{tag}", f"Quillan Ridge card {tag}", quillan))
    c.execute("""SELECT atom_id FROM acp_contract.tour_atoms WHERE tour_id = %s AND place = 'Quillan Ridge'""", (a,))
    c.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (c.fetchone()[0], card))
    card2 = str(uuid.uuid4())
    c.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                 VALUES (%s, %s, 'Sorel Falls', 'Laos', %s)""", (card2, f"sorel-{tag}", brask))
    c.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (sorel_plain, card2))

    # the line stretch of day 2 on Lake Merrowin (current build)
    build = t228._build(c, current=True)
    c.execute("""INSERT INTO geo.tour_segment (build_id, source_id, day_index, destination_id, segment, basis,
                                               from_destination_id, to_destination_id, segment_km)
                 VALUES (%s, %s, 2, %s, %s, 'day_anchors', %s, %s, 31.5)""",
              (build, src_a, merrowin, json.dumps(LINE), velmora, brask))
    return {"tag": tag, "a": a, "b": b, "retired": retired, "bare": bare, "tours": [a, b, retired, bare],
            "velmora": velmora, "sorel": sorel, "quillan": quillan, "brask": brask, "merrowin": merrowin}


def test_same_rows_every_column_for_the_listed_tours(cur):
    s = cur.seeded
    after = _rows(cur, s["tours"])
    assert after == cur.rows_before
    # the fixture really covers the cases
    cur.execute("SELECT * FROM catalog.v_tour_place LIMIT 0")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in after]
    a = [(r["day_number"], r["position"], r["place_name"], r["destination_id"] and str(r["destination_id"]),
          r["atom_count"]) for r in rows if str(r["tour_id"]) == s["a"]]
    assert a == [
        (1, 1, "Velmora", s["velmora"], 3),          # legacy name match; three spellings, one place key
        (1, 2, "Quillan Ridge", s["quillan"], 1),    # the card's destination
        (1, 3, "Sôrel Falls", s["sorel"], 1),        # the text's spelling; pipeline link by name_norm (accent)
        (1, 4, "Sorel Falls", s["sorel"], 2),        # another place key, same name_norm: same pipeline link
        (1, 5, "Unnamed Grove", None, 1),            # no coordinates
        (2, 1, "Brask Harbour", s["brask"], 1),
        (2, 2, "Lake Merrowin", s["merrowin"], 1),
        (3, 1, "Kaldor Pass", None, 1),              # only a suspect destination: none
        (4, 1, "Kaldor Pass", None, 1),              # the ranged heading covers day 4
        (6, 1, "Toller Bay", None, 1),               # no day text: the row stays
    ]
    grove = next(r for r in rows if r["place_name"] == "Unnamed Grove")
    assert grove["lat"] is None and grove["lng"] is None and grove["activity_types"] == ["wildlife"]
    lake = next(r for r in rows if r["place_name"] == "Lake Merrowin")
    assert lake["segment_basis"] == "day_anchors" and lake["segment"] == LINE and lake["precision"] == "line"
    assert [(r["day_number"], r["place_name"], str(r["destination_id"])) for r in rows if str(r["tour_id"]) == s["b"]] \
        == [(1, "Velmora", s["velmora"])]
    assert not [r for r in rows if str(r["tour_id"]) in (s["retired"], s["bare"])]


def test_reader_projection_unchanged(cur):
    after = _as(cur, "aa_app_user", READER_ORDERED, cur.seeded["tours"])
    assert after == cur.reader_before
    assert len(after) == 11


def test_whole_view_unchanged(cur):
    assert _whole(cur) == cur.whole_before
    assert len(cur.whole_before) >= 11


def test_columns_types_owner_barrier_grants(cur):
    assert _cols(cur, "catalog.v_tour_place") == cur.cols_before
    cur.execute("""SELECT pg_get_userbyid(relowner), coalesce(reloptions, '{}') FROM pg_class
                    WHERE oid = 'catalog.v_tour_place'::regclass""")
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in opts
    for role in ("aa_app_user", "tripplanner", "booking"):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_tour_place', 'SELECT')", (role,))
        assert cur.fetchone()[0] is True, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    # S282 review: 431's expression index evaluates geo.name_norm as the writing role (cis_app, place_extract).
    cur.execute("""SELECT has_function_privilege('cis_app', 'geo.name_norm(text)', 'EXECUTE'),
                          EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                                   WHERE p.oid = 'geo.name_norm(text)'::regprocedure
                                     AND a.grantee = 'cis_app'::regrole AND a.privilege_type = 'EXECUTE')""")
    assert cur.fetchone() == (True, True)


def test_registered_once_and_rerunnable(cur):
    _apply(cur, MIGRATION)
    cur.execute("SELECT version, count(*) FROM shared.schema_versions WHERE version IN ('431', '432') "
                "GROUP BY version ORDER BY version")
    assert cur.fetchall() == [("431", 1), ("432", 1)]
    assert _rows(cur, cur.seeded["tours"]) == cur.rows_before


def test_needs_431(cur):
    cur.execute("SAVEPOINT s")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '431'")
    with pytest.raises(psycopg2.errors.RaiseException, match="apply 253, 256, 289 and 431 first"):
        _apply(cur, MIGRATION)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_431_index_is_the_name_key(cur):
    cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'geo' "
                "AND indexname = 'tour_place_source_day_name_norm_idx'")
    assert cur.fetchone()[0].endswith("USING btree (source_id, day_index, geo.name_norm(name))")
    cur.execute("SELECT provolatile FROM pg_proc WHERE oid = 'geo.name_norm(text)'::regprocedure")
    assert cur.fetchone()[0] == "i"                  # an expression index needs IMMUTABLE


def test_reader_plan_keys_the_atoms_and_the_name_lookup(cur):
    """The reader's form (tour_id = ANY, lesson S238). Index paths forced on (the fixture tables are tiny, so the
    planner would rightly pick sequential scans): the atoms are read per tour (no Sort / Merge Join of tour_atoms),
    and the pipeline lookup matches the name key in the index, with no per-row name_norm filter left."""
    cur.execute("SET LOCAL enable_seqscan = off")
    cur.execute("SET LOCAL enable_bitmapscan = off")   # plain index scans: the plan text the asserts read
    cur.execute("SET LOCAL ROLE aa_app_user")
    cur.execute("EXPLAIN (COSTS OFF) " + READER_SQL, (cur.seeded["tours"],))
    plan = "\n".join(r[0] for r in cur.fetchall())
    cur.execute("RESET ROLE")
    atoms = r"Index Scan using idx_tour_atoms_tour_id on tour_atoms ta\n\s+Index Cond: \(tour_id = (rt|pt)\.tour_id\)"
    assert re.search(atoms, plan), plan
    assert "Merge Join" not in plan, plan
    m = re.search(r"Index Scan using tour_place_source_day_name_norm_idx on tour_place gp\n\s+Index Cond: ([^\n]+)\n"
                  r"(\s+Filter: ([^\n]+))?", plan)
    assert m, plan
    assert "(geo.name_norm(name) = geo.name_norm(" in m.group(1), plan     # the place name key: an index condition
    assert "name_norm" not in (m.group(3) or ""), plan


def test_day_text_and_day_views_unchanged(cur):
    text, days = _days(cur)
    assert (text, days) == cur.days_before
    a = [(r[4], r[5]) for r in text if str(r[2]) == cur.seeded["a"]]
    assert a == [(1, "Arrive in Velmora"), (2, "Velmora to Brask Harbour"), (3, "Kaldor Pass trek"),
                 (4, "Kaldor Pass trek"), (5, "Depart")]


def test_name_norm_same_keys_and_twin(cur):
    from services.places.names import name_norm as py_name_norm
    after = _norms(cur)
    assert after == cur.norm_before
    assert [k for _, k in after][:8] == ["velmora", "quillan ridge", "brask harbour north", "sorel falls",
                                         "sorel falls", "kaldors pass", "kaldors pass", "aeskel oy"]
    for s, key in after:                          # the fast path's inputs (ASCII, NULL): the twin agrees
        if s is None or s.isascii():
            assert py_name_norm(s) == key, s
    cur.execute("SELECT provolatile, proparallel FROM pg_proc WHERE oid = 'geo.name_norm(text)'::regprocedure")
    assert cur.fetchone() == ("i", "s")
