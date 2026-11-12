"""AA-1091 item 5 — migration 402: catalog.v_tour_price_reference_v2, AA-Booking's read of the structured reference
price (CIS 401) of the live published tours, next to the source price text.

conftest skips 402 (catalog recipe over 253's _live_tour), as it skips 365. Each test runs in ONE transaction on the
RDS-like state of test_aa860_migration_245 (module fixture), the way test_aa1029_migration_365 builds it: 273's
replayed column is dropped first, 253 / 256 / 257 / 258 / 273 / 289 and 402 are applied as cis_owner (DB Migrate's
live-owner switch), the checks run, and the transaction is rolled back. 401 is replayed by conftest (expand).

Checks: registered once, re-runnable; catalog_owner's with security_barrier, SELECT to booking only, catalog_owner
loses CREATE again; the exact columns; one row per live tour (with or without a text or a saved price), a tour that is
not live gives none; the status rule (confirmed / parsed / needs_review, text changed = needs_review, none = NULL).
Invented tour names only.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa712_migration_228 as t228  # noqa: E402  (seed helpers only)
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa990_migration_289 as t289  # noqa: E402  (helpers: _apply, _cols, migration_255)
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa990_migration_289 import migration_255  # noqa: E402,F401  (module autouse fixture)

BEFORE = t289.BEFORE + (t289.MIGRATION,)
MIGRATION = "402_tour_price_reference_view_v2.sql"
VIEW = "catalog.v_tour_price_reference_v2"
COLUMNS = ["source_system", "source_id", "price_raw", "reference_amount", "reference_currency", "reference_basis",
           "reference_status", "reference_origin", "reference_reviewed_at", "reference_updated_at"]
READ_SQL = (f"SELECT source_id, price_raw, reference_amount::text, reference_currency, reference_basis, "
            f"reference_status, reference_origin FROM {VIEW} WHERE source_system = 'cis'")


@pytest.fixture
def cur(rds_like_245):  # noqa: F811
    conn = t245._connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE gold_aa_internal.published_tours DROP COLUMN IF EXISTS updated_at")   # Dev before 273
        c.execute("SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding'")
        if not c.fetchone():
            c.execute("ALTER TABLE geo.tour_place ADD COLUMN IF NOT EXISTS embedding float8[]")
        for name in BEFORE:
            t289._apply(c, name)
        t289._apply(c, MIGRATION)
        t289._apply(c, MIGRATION)                                     # idempotent re-run
        yield c
    finally:
        conn.rollback()
        conn.close()


def _as(c, role, sql, *args):
    c.execute(f"SET LOCAL ROLE {role}")
    c.execute(sql, args)
    out = c.fetchall()
    c.execute("RESET ROLE")
    return out


def _price(c, key, *, amount="1450.00", currency="USD", basis="per_person", origin="staff", needs_review=False,
           seen=None, reviewed=True):
    c.execute(
        "INSERT INTO silver_aa_internal.tour_reference_price (source_key, amount, currency, basis, origin, "
        "needs_review, price_raw_seen, reviewed_by, reviewed_at, updated_by) VALUES (%s, %s::numeric, %s, %s, %s, %s, "
        "%s, %s, CASE WHEN %s THEN now() END, 'admin:pytest')",
        (key, amount, currency, basis, origin, needs_review, seen, "admin:pytest" if reviewed else None, reviewed))


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '402'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    assert [n for n, _ in t289._cols(cur, VIEW)] == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (VIEW,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('booking', %s, 'SELECT') AS ok", (VIEW,))
    assert cur.fetchone()[0] is True
    for role in ("tripplanner", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS ok", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE') AS ok")
    assert cur.fetchone()[0] is False


def test_one_row_per_live_tour_and_the_status_rule(cur):
    tag = uuid.uuid4().hex[:8]
    confirmed, _ = t228._tour(cur, name=f"Invented Ridge Trek {tag}", country="Laos")
    parsed, _ = t228._tour(cur, name=f"Invented Lake Loop {tag}", country="Laos")
    doubtful, _ = t228._tour(cur, name=f"Invented Valley Walk {tag}", country="Laos")
    moved, _ = t228._tour(cur, name=f"Invented River Day {tag}", country="Laos")
    staff_only, _ = t228._tour(cur, name=f"Invented Hill Camp {tag}", country="Laos")
    none, _ = t228._tour(cur, name=f"Invented Garden Stroll {tag}", country="Laos")
    inactive, _ = t228._tour(cur, name=f"Invented Cave Day {tag}", country="Laos", master_status="inactive")
    texts = {confirmed: "from USD 1,450 per person", parsed: "USD 900 per person", doubtful: "USD 900 or EUR 850",
             moved: "USD 1,200 per person", none: "   ", inactive: "USD 700"}
    for tid, text in texts.items():
        cur.execute("UPDATE silver_aa_internal.raw_tours SET price_raw = %s WHERE tour_id = %s", (text, tid))
    keys = {r[1]: r[0] for r in _as(cur, "tripplanner", "SELECT source_id, tour_id::text FROM catalog.v_tour")}
    _price(cur, keys[confirmed], basis="from", seen=texts[confirmed])
    _price(cur, keys[parsed], amount="900", origin="parsed", seen=texts[parsed], reviewed=False)
    _price(cur, keys[doubtful], amount="900", origin="parsed", needs_review=True, seen=texts[doubtful], reviewed=False)
    _price(cur, keys[moved], amount="1100", seen="USD 1,100 per person")          # the text changed since
    _price(cur, keys[staff_only], amount="2300", currency="EUR", basis="per_group", origin="import", seen=None)
    rows = {r[0]: r[1:] for r in _as(cur, "booking", READ_SQL)}
    assert rows[keys[confirmed]] == ("from USD 1,450 per person", "1450.00", "USD", "from", "confirmed", "staff")
    assert rows[keys[parsed]][4:] == ("parsed", "parsed")
    assert rows[keys[doubtful]][4] == "needs_review"
    assert rows[keys[moved]][4] == "needs_review"
    assert rows[keys[staff_only]] == (None, "2300.00", "EUR", "per_group", "confirmed", "import")
    assert rows[keys[none]] == (None, None, None, None, None, None)                # live, no text, no price: a row
    assert inactive not in keys                                                  # not live: no row
    assert "USD 700" not in {v[0] for v in rows.values()}
