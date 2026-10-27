"""AA-990 (S253) — migration 289: catalog._tour_day_text and catalog.v_tour.duration_days read a ranged day heading
("Day 12-15 — Beach") as every day it covers, the rule of services/content_generation/day_parse.py.

conftest skips 289 (it needs 253 / 273's catalog part and the catalog recipe). Each test runs in ONE transaction on the
RDS-like state of test_aa860_migration_245 (module fixture): 273's replayed column is dropped first (as in
test_aa645_migration_273), 253 / 256 / 257 / 258 / 273 and 289 are applied as cis_owner (DB Migrate's live-owner
switch), the checks run, and the transaction is rolled back.

Checks: registered once, re-runnable, views still catalog_owner's with security_barrier and catalog_owner loses CREATE
again; same columns; v_tour_day equals the Python parser (parse_canonical_itinerary_days) on plain, ranged (hyphen,
en dash, "Days … to …", at the end of the tour), overlapping and invalid headings; a tour without a ranged heading
keeps exactly its rows; v_tour.duration_days' fallback counts a ranged heading's last day.
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
          "258_catalog_live_source_key.sql", "273_published_tours_updated_at.sql")
MIGRATION = "289_catalog_ranged_day_heading.sql"

PLAIN = "Day 1 — Arrive in Colombo\nLand and transfer.\n\nDay 2 — Kandy\nDrive to Kandy.\n\nDay 3 — Depart\nFly."
TEXTS = {
    "plain": PLAIN,
    "hyphen": ("Day 11 — Galle\nWalk the fort.\n\nDay 12-15 — Beach leisure in Bentota\nFree days on the beach."
               "\n\nDay 16 — Departure\nTransfer to the airport."),
    "en_dash": "Day 1 — A\nx\n\nDay 2–3 — B\ny\n\nDay 4 — C\nz",
    "days_to": "Day 1 — A\nx\n\nDays 2 to 4 — B\ny\n\nDay 5 — C\nz",
    "end": "Day 1 — A\nx\n\nDay 2 — B\ny\n\nDay 3-5 — Beach\nFree days.",
    "overlap": "Day 3-5 — Trek\ntrek text\n\nDay 4 — Rest\nrest text",
    "invalid": "Day 14 — A\nx\n\nDay 15-12 — B\ny\n\nDay 16-40 — C\nz",
    "numeric_title": "Day 1 — 15 km ride\nx\n\nDay 2 — B\ny",
}


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


def _days(c, tour_id):
    rows = _as_tripplanner(c, """SELECT day_number, title, body FROM catalog.v_tour_day
                                  WHERE tour_id = %s::uuid ORDER BY day_number""", tour_id)
    return {d: {"title": t, "body": b} for d, t, b in rows}


class _Cur(psycopg2.extensions.cursor):
    """Carries the seeded tours and the pre-289 state."""


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
        c.seeded = {}
        for key, text in TEXTS.items():
            tid, _ = t228._tour(c, name=f"Ranged {key} {tag}", country="Laos")
            c.execute("UPDATE gold_aa_internal.published_tours SET aa_itineraries = %s WHERE tour_id = %s",
                      (text, tid))
            c.seeded[key] = tid
        c.before = {"day_text_cols": _cols(c, "catalog._tour_day_text"), "v_tour_cols": _cols(c, "catalog.v_tour"),
                    "plain_days": _days(c, c.seeded["plain"]),
                    "hyphen_days": sorted(_days(c, c.seeded["hyphen"]))}
        _apply(c, MIGRATION)
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registered_rerunnable_and_owned(cur):
    _apply(cur, MIGRATION)                                  # idempotent re-run
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '289'")
    assert cur.fetchone()[0] == 1
    cur.execute("""SELECT pg_get_userbyid(relowner), coalesce(reloptions, '{}') FROM pg_class
                    WHERE oid IN ('catalog._tour_day_text'::regclass, 'catalog.v_tour'::regclass)""")
    rows = cur.fetchall()
    assert {r[0] for r in rows} == {"catalog_owner"}
    assert all("security_barrier=true" in r[1] for r in rows)
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_views_keep_their_columns(cur):
    assert _cols(cur, "catalog._tour_day_text") == cur.before["day_text_cols"]
    assert _cols(cur, "catalog.v_tour") == cur.before["v_tour_cols"]


def test_before_289_the_range_was_folded(cur):
    """The Sri Lanka shape before this file: Day 11, then nothing until Day 16."""
    assert cur.before["hyphen_days"] == [11, 16]


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_v_tour_day_matches_the_python_parser(cur, key):
    from services.content_generation.itinerary_utils import parse_canonical_itinerary_days

    expected = {d: {"title": v["title"], "body": v["body"]}
                for d, v in parse_canonical_itinerary_days(TEXTS[key]).items()}
    assert _days(cur, cur.seeded[key]) == expected


def test_ranged_days_and_plain_tours(cur):
    hyphen = _days(cur, cur.seeded["hyphen"])
    assert sorted(hyphen) == [11, 12, 13, 14, 15, 16]
    assert hyphen[13]["title"] == "Beach leisure in Bentota" and "Beach" not in hyphen[11]["body"]
    assert sorted(_days(cur, cur.seeded["invalid"])) == [14, 15, 16]      # backwards / too long: single days
    assert _days(cur, cur.seeded["plain"]) == cur.before["plain_days"]


def test_duration_fallback_counts_the_last_ranged_day(cur):
    q = "SELECT duration_days FROM catalog.v_tour WHERE tour_id = %s::uuid"
    assert _as_tripplanner(cur, q, cur.seeded["end"]) == [(5,)]
    assert _as_tripplanner(cur, q, cur.seeded["hyphen"]) == [(16,)]
    assert _as_tripplanner(cur, q, cur.seeded["plain"]) == [(3,)]
    assert _as_tripplanner(cur, q, cur.seeded["invalid"]) == [(16,)]
