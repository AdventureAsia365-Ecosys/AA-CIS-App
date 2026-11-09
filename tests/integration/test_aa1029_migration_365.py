"""AA-1029 R4 (S267) — migration 365: catalog.v_tour_price_reference_v1, AA-Booking's read of the source price text
of the live published tours (the Booking trip plan keeps it as a reviewed reference price, root ADR 0005 d7).

conftest skips 365 (catalog recipe over 253's _live_tour). Each test runs in ONE transaction on the RDS-like state of
test_aa860_migration_245 (module fixture), the way test_aa1040_migration_359 builds it: 273's replayed column is
dropped first, 253 / 256 / 257 / 258 / 273 / 289 and 365 are applied as cis_owner (DB Migrate's live-owner switch),
the checks run, and the transaction is rolled back.

Checks: registered once, re-runnable; catalog_owner's with security_barrier, SELECT to booking only, catalog_owner
loses CREATE again; the exact columns; one row per live tour with a non-blank price_raw (a blank or missing text and a
tour that is not live give none); the SELECT AA-Booking runs (backend/app/catalog/trip_plan_sync.py REFERENCES_SQL)
answers as booking. Invented tour names only.
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
MIGRATION = "365_tour_price_reference_view.sql"
VIEW = "catalog.v_tour_price_reference_v1"
# The SELECT of AA-Booking backend/app/catalog/trip_plan_sync.py (REFERENCES_SQL).
BK_REFERENCES_SQL = f"SELECT source_id, price_raw FROM {VIEW} WHERE source_system = 'cis'"


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


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '365'")
    assert cur.fetchone()[0] == 1


def test_columns_owner_barrier_and_consumers(cur):
    assert [n for n, _ in t289._cols(cur, VIEW)] == ["source_system", "source_id", "price_raw"]
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


def test_one_row_per_live_tour_with_a_source_price(cur):
    tag = uuid.uuid4().hex[:8]
    priced, _ = t228._tour(cur, name=f"Invented Ridge Trek {tag}", country="Laos")
    blank, _ = t228._tour(cur, name=f"Invented Lake Loop {tag}", country="Laos")
    none, _ = t228._tour(cur, name=f"Invented Valley Walk {tag}", country="Laos")
    inactive, _ = t228._tour(cur, name=f"Invented Cave Day {tag}", country="Laos", master_status="inactive")
    cur.execute("UPDATE silver_aa_internal.raw_tours SET price_raw = 'from USD 1,450 per person' WHERE tour_id = %s",
                (priced,))
    cur.execute("UPDATE silver_aa_internal.raw_tours SET price_raw = '   ' WHERE tour_id = %s", (blank,))
    cur.execute("UPDATE silver_aa_internal.raw_tours SET price_raw = 'USD 900' WHERE tour_id = %s", (inactive,))
    keys = {r[1]: r[0] for r in _as(cur, "tripplanner", "SELECT source_id, tour_id::text FROM catalog.v_tour")}
    rows = dict(_as(cur, "booking", BK_REFERENCES_SQL))
    assert rows.get(keys[priced]) == "from USD 1,450 per person"
    assert keys[blank] not in rows and keys[none] not in rows
    assert inactive not in keys  # not live: not in v_tour, so not in the reference view either
    assert "USD 900" not in rows.values()
    assert all(v.strip() for v in rows.values())
