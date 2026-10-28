"""AA-996 follow-up (S256) — migration 298: catalog.v_tour_place_name, the places of a tour day by name only, for the
AA-Booking trip-plans list (Booking read catalog.v_tour_place: 380 ms for 25 tours, 994 ms for 100 on Dev).

conftest skips 298 (it needs 253 / 256 / 289 and the catalog recipe). Each test runs in ONE transaction on the RDS-like
state of test_aa860_migration_245 (module fixture): 253 / 256 / 257 / 258 / 273 / 289 are applied as cis_owner (DB
Migrate's live-owner switch), the tours are seeded, 298 is applied, the checks run, and the transaction is rolled back.

Checks: registered once, re-runnable, the view catalog_owner's with security_barrier and catalog_owner loses CREATE
again; the six columns with v_tour_place's types; row for row the same as v_tour_place's projection (spelling, order,
a day without text, a place card and a pipeline link that must not multiply rows, a tour that is not live); SELECT to
booking only; the consumer read (tour_id = ANY, lesson S238) is planned below the barrier and touches none of the
lookup tables v_tour_place reads (the point of the view).
"""
import os
import re
import sys
import uuid

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
MIGRATION = "298_catalog_v_tour_place_name.sql"
NAME_COLS = ("source_system", "source_id", "tour_id", "day_number", "position", "place_name")
# Tables only v_tour_place's lookups read (place card, pipeline link, legacy / chosen destination, line stretch).
LOOKUP_SCAN = re.compile(r"\bon (atom_place_card|place_card|tour_place|destinations|tour_segment|graph_build)\b")
# The AA-Booking destinations read (app/catalog/trip_plans.py DESTINATIONS_SQL) on the new view.
BOOKING_DESTINATIONS = """
SELECT x.tour_id::text AS key, (array_agg(x.place ORDER BY x.first_seen, x.place))[1:8] AS places
FROM (
    SELECT p.tour_id, btrim(p.place_name) AS place, min(p.day_number * 1000 + p.position) AS first_seen
    FROM catalog.v_tour_place_name p
    WHERE p.tour_id = ANY(%s::uuid[]) AND p.day_number IS NOT NULL AND p.place_name IS NOT NULL
    GROUP BY p.tour_id, btrim(p.place_name)
) x
WHERE x.place <> ''
GROUP BY x.tour_id
"""


@pytest.fixture(scope="module", autouse=True)
def migration_255():
    """test_aa712_migration_226 drops schema geo and re-applies 226 only: re-apply 255 (idempotent)."""
    c = t245._connect()
    c.autocommit = True
    with open(os.path.join(MIG, "255_geo_place_shape.sql")) as f:
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


def _as_booking(c, sql, *args):
    c.execute("SET LOCAL ROLE booking")
    c.execute(sql, args)
    out = c.fetchall()
    c.execute("RESET ROLE")
    return out


def _plan(c, sql, *args):
    return "\n".join(r[0] for r in _as_booking(c, "EXPLAIN (COSTS OFF) " + sql, *args))


def _barrier_filter(plan: str) -> bool:
    """True when the security-barrier subquery has its OWN Filter (the qual was not pushed into it), as 256's test."""
    lines = plan.splitlines()
    return any("Subquery Scan" in ln and i + 1 < len(lines) and lines[i + 1].strip().startswith("Filter:")
               for i, ln in enumerate(lines))


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded tours."""


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
        c.seeded = _seed(c)
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _seed(c):
    tag = uuid.uuid4().hex[:8]
    # t228.ITINERARY: "Day 1 — Arrive in Paro …", "Day 2 — Paro to Thimphu …", "Day 3 — Depart …"
    order, src = t228._tour(c, name=f"Order {tag}")
    other, _ = t228._tour(c, name=f"Other {tag}")
    retired, _ = t228._tour(c, name=f"Retired {tag}", master_status="trashed")
    t228._atom(c, order, day=2, place="Zhemgang")            # not in the day text: last
    t228._atom(c, order, day=2, place="thimphu")
    t228._atom(c, order, day=2, place="thimphu")
    thimphu = t228._atom(c, order, day=2, place="Thimphu")   # verbatim spelling wins over the more frequent one
    t228._atom(c, order, day=2, place="Paro")                # named first in "Paro to Thimphu"
    t228._atom(c, order, day=2, place="  ")                  # blank: no row
    t228._atom(c, order, day=7, place="Bumthang")            # no "Day 7" block: the row stays
    t228._atom(c, order, day=1, place=f"Paro {tag}")
    t228._atom(c, other, day=1, place="Paro")
    t228._atom(c, retired, day=1, place="Punakha")           # not a live tour: no row in either view
    # a place card and a pipeline link on the order tour: v_tour_place resolves them, the names view must not care
    paro = t228._dest(c, f"Paro {tag}")
    t228._component(c, src, order, day=1, name=f"Paro {tag}", destination_id=paro)
    card = str(uuid.uuid4())
    c.execute("""INSERT INTO acp_shared.place_card (card_id, place_key, display_name, country, destination_id)
                 VALUES (%s, %s, 'Thimphu', 'Bhutan', %s)""", (card, f"thimphu-{tag}", paro))
    c.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES (%s, %s)", (thimphu, card))
    return {"order": order, "other": other, "retired": retired, "tag": tag}


def _names(c, view, tours):
    return _as_booking(c, f"""SELECT {', '.join(NAME_COLS)} FROM catalog.{view}
                               WHERE tour_id = ANY(%s::uuid[]) ORDER BY tour_id, day_number, position""", tours)


def test_registered_rerunnable_and_owned(cur):
    _apply(cur, MIGRATION)                                  # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '298'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT pg_get_userbyid(relowner), coalesce(reloptions, '{}') FROM pg_class
                    WHERE oid = 'catalog.v_tour_place_name'::regclass""")
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in opts
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_six_columns_with_v_tour_place_types(cur):
    place = dict(_cols(cur, "catalog.v_tour_place"))
    assert _cols(cur, "catalog.v_tour_place_name") == [(n, place[n]) for n in NAME_COLS]


def test_rows_equal_v_tour_place_names(cur):
    s = cur.seeded
    tours = [s["order"], s["other"], s["retired"]]
    rows = _names(cur, "v_tour_place_name", tours)
    assert rows == _names(cur, "v_tour_place", tours)
    tag = s["tag"]
    assert [(str(r[2]), r[3], r[4], r[5]) for r in rows if str(r[2]) == s["order"]] == [
        (s["order"], 1, 1, f"Paro {tag}"),
        (s["order"], 2, 1, "Paro"),
        (s["order"], 2, 2, "Thimphu"),
        (s["order"], 2, 3, "Zhemgang"),
        (s["order"], 7, 1, "Bumthang"),
    ]
    assert not [r for r in rows if str(r[2]) == s["retired"]]


def test_whole_view_equals_v_tour_place_projection(cur):
    cols = ", ".join(NAME_COLS)
    diff = _as_booking(cur, f"""SELECT count(*) FROM (
        (SELECT {cols} FROM catalog.v_tour_place EXCEPT ALL SELECT {cols} FROM catalog.v_tour_place_name)
        UNION ALL
        (SELECT {cols} FROM catalog.v_tour_place_name EXCEPT ALL SELECT {cols} FROM catalog.v_tour_place)) d""")
    assert diff == [(0,)]


def test_booking_only(cur):
    for role, expected in (("booking", True), ("tripplanner", False), ("aa_app_user", False), ("cis_app", False)):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_tour_place_name', 'SELECT')", (role,))
        assert cur.fetchone()[0] is expected, role


def test_booking_destinations_read(cur):
    s = cur.seeded
    rows = dict(_as_booking(cur, BOOKING_DESTINATIONS, [s["order"], s["other"], s["retired"]]))
    assert rows == {s["order"]: [f"Paro {s['tag']}", "Paro", "Thimphu", "Zhemgang", "Bumthang"],
                    s["other"]: ["Paro"]}


def test_consumer_read_is_keyed_and_skips_the_lookups(cur):
    """Booking's form (tour_id = ANY, lesson S238): the qual reaches the tours inside the barrier and none of the
    tables of v_tour_place's lookups is in the plan; v_tour_place's own plan still has them (what the view saves)."""
    tours = [cur.seeded["order"], cur.seeded["other"]]
    plan = _plan(cur, "SELECT tour_id, day_number, position, place_name FROM catalog.v_tour_place_name "
                      "WHERE tour_id = ANY(%s::uuid[])", tours)
    assert not _barrier_filter(plan), plan
    assert "ANY" in plan, plan
    assert not LOOKUP_SCAN.search(plan), plan
    heavy = _plan(cur, "SELECT tour_id, day_number, position, place_name FROM catalog.v_tour_place "
                       "WHERE tour_id = ANY(%s::uuid[])", tours)
    assert LOOKUP_SCAN.search(heavy), heavy
