"""AA-645 step 0 (d06) — migration 273: published_tours.updated_at; catalog._live_tour.master_updated_at; v_tour's
updated_at follows the Master row's last change while published_at stays the first publish.

conftest replays only 273's column part (it skips 253-258, which the catalog part needs). Each test runs in ONE
transaction on the RDS-like state of test_aa860_migration_245 (module fixture): the replayed column is dropped first
(the Dev state before 273), 253 / 256 / 257 / 258 and 273 are applied as cis_owner (DB Migrate's live-owner switch),
the checks run, and the transaction is rolled back.

Checks: registered once, re-runnable; the column is timestamptz NOT NULL DEFAULT now() and every existing row is
backfilled to its published_at; the views stay catalog_owner's and catalog_owner loses CREATE again; v_tour keeps its
columns and, read as tripplanner, its rows apart from updated_at; _live_tour gains master_updated_at as its last
column; a re-publish (the code's upsert: updated_at = now(), published_at untouched) moves v_tour.updated_at and
leaves v_tour.published_at.
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
          "258_catalog_live_source_key.sql")
MIGRATION = "273_published_tours_updated_at.sql"
V_TOUR_NO_UPDATED_AT = """
    SELECT count(*), md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), ''))
    FROM (SELECT source_system, source_id, tour_id, slug, title, published_at, content_hash FROM catalog.v_tour) r
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


def _as_tripplanner(c, sql, *args):
    c.execute("SET LOCAL ROLE tripplanner")
    c.execute(sql, args)
    out = c.fetchall()
    c.execute("RESET ROLE")
    return out


class _Cur(psycopg2.extensions.cursor):
    """Carries the pre-273 state."""


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
        tag = uuid.uuid4().hex[:8]
        old, _ = t228._tour(c, name=f"Old {tag}", country="Laos")
        new, _ = t228._tour(c, name=f"New {tag}", country="Laos")
        c.execute("UPDATE gold_aa_internal.published_tours SET published_at = '2026-01-15T00:00:00Z' "
                  "WHERE tour_id = %s", (old,))
        c.seeded = {"old": old, "new": new}
        c.before = {"v_tour_cols": _cols(c, "catalog.v_tour"), "live_cols": _cols(c, "catalog._live_tour"),
                    "v_tour_rows": _as_tripplanner(c, V_TOUR_NO_UPDATED_AT)}
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_and_owned(cur):
    _apply(cur, MIGRATION)                                  # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '273'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT pg_get_userbyid(relowner) FROM pg_class
                    WHERE oid IN ('catalog._live_tour'::regclass, 'catalog.v_tour'::regclass)""")
    assert {r[0] for r in cur.fetchall()} == {"catalog_owner"}
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False
    cur.execute("SELECT coalesce(reloptions, '{}') FROM pg_class WHERE oid = 'catalog.v_tour'::regclass")
    assert "security_barrier=true" in cur.fetchone()[0]


def test_column_shape_and_backfill(cur):
    cur.execute("""SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
                     FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = 'gold_aa_internal.published_tours'::regclass AND a.attname = 'updated_at'""")
    assert cur.fetchone() == ("timestamp with time zone", True, "now()")
    cur.execute("SELECT count(*) FROM gold_aa_internal.published_tours WHERE updated_at IS DISTINCT FROM published_at")
    assert cur.fetchone()[0] == 0


def test_views_keep_their_columns_and_rows(cur):
    assert _cols(cur, "catalog.v_tour") == cur.before["v_tour_cols"]
    assert _cols(cur, "catalog._live_tour") == cur.before["live_cols"] + [("master_updated_at",
                                                                           "timestamp with time zone")]
    assert _as_tripplanner(cur, V_TOUR_NO_UPDATED_AT) == cur.before["v_tour_rows"]


def test_a_republish_moves_updated_at_and_keeps_published_at(cur):
    old = cur.seeded["old"]
    q = "SELECT published_at, updated_at FROM catalog.v_tour WHERE tour_id = %s::uuid"
    (pub0, upd0), = _as_tripplanner(cur, q, old)
    assert pub0.isoformat().startswith("2026-01-15") and upd0 == pub0
    # what PublishedCatalogRepository.insert's ON CONFLICT branch now does (published_at untouched)
    cur.execute("UPDATE gold_aa_internal.published_tours SET aa_name = 'Rewritten', updated_at = now() + interval "
                "'1 day' WHERE tour_id = %s", (old,))
    (pub1, upd1), = _as_tripplanner(cur, q, old)
    assert pub1 == pub0 and upd1 > pub0


def test_a_first_publish_gets_both_dates_from_the_defaults(cur):
    fresh, _ = t228._tour(cur, name=f"Fresh {uuid.uuid4().hex[:8]}", country="Laos")   # inserted after 273
    cur.execute("SELECT published_at, updated_at FROM gold_aa_internal.published_tours WHERE tour_id = %s",
                (fresh,))
    pub, upd = cur.fetchone()
    assert pub is not None and upd == pub
