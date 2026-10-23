"""AA-847 — migration 254: tripplanner loses the CIS 179 (raw_tours) and 180 (tour_atoms) column reads.

conftest replays 254 like every file (as the test superuser, the owner here, so its REVOKE removes the grants like
cis_owner's does on Dev). Because conftest swallows migration errors, the module re-applies the file itself (errors
not swallowed; the re-run must be a no-op) and checks the effect by privilege, not by the registry row:
  * tripplanner can read no column of silver_aa_internal.raw_tours or acp_contract.tour_atoms;
  * USAGE on schema silver_aa_internal is gone (nothing else of the schema is granted to tripplanner);
  * kept by 254: USAGE on acp_contract and the CIS 216 column grants on acp_contract.v_active_tour_atoms, and the
    catalog reads (USAGE on schema catalog). CIS 270 (AA-712 X4) revokes the first two later; conftest replays every
    file, so those checks expect 270's result when 270 is registered (test_aa712_migration_270.py covers it);
  * registered once.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import psycopg2  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "254_tripplanner_raw_reads_revoked.sql")
TOUR_ATOM_COLS = ("atom_id", "tour_id", "owner_scope", "itinerary_day", "place", "activity_type", "action", "text",
                  "season_note", "deleted", "is_empty_marker")


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    k.execute("SELECT to_regrole('tripplanner') IS NOT NULL")
    if not k.fetchone()[0]:
        pytest.skip("role tripplanner missing in this database (conftest creates it in CI)")
    c.notices.clear()
    k.execute(open(MIGRATION, encoding="utf-8").read())      # errors raise here (conftest swallows its own run)
    yield k, list(c.notices)
    c.close()


def _readable(k, table):
    k.execute("""SELECT a.attname FROM pg_attribute a
                  WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                    AND has_column_privilege('tripplanner', a.attrelid, a.attnum, 'SELECT')
                  ORDER BY 1""", (table,))
    return [r[0] for r in k.fetchall()]


def test_no_column_of_raw_tours_or_tour_atoms_is_readable(cur):
    k, notices = cur
    assert _readable(k, "silver_aa_internal.raw_tours") == []
    assert _readable(k, "acp_contract.tour_atoms") == []
    assert any("revoked" in n or "nothing to revoke" in n for n in notices), notices


def _after_270(k) -> bool:
    k.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '270'")
    return k.fetchone()[0] > 0


def test_silver_usage_is_gone_and_acp_contract_usage_is_kept(cur):
    k, _ = cur
    k.execute("SELECT has_schema_privilege('tripplanner', 'silver_aa_internal', 'USAGE'), "
              "has_schema_privilege('tripplanner', 'acp_contract', 'USAGE'), "
              "has_schema_privilege('tripplanner', 'catalog', 'USAGE')")
    silver, acp, catalog = k.fetchone()
    assert silver is False and acp is (not _after_270(k))
    k.execute("SELECT to_regnamespace('catalog') IS NOT NULL")
    if k.fetchone()[0]:
        assert catalog is True


def test_the_216_view_grants_are_kept(cur):
    k, _ = cur
    k.execute("SELECT to_regclass('acp_contract.v_active_tour_atoms') IS NOT NULL")
    if not k.fetchone()[0]:
        pytest.skip("acp_contract.v_active_tour_atoms (CIS 203) not in this database")
    k.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '216'")
    if not k.fetchone()[0]:
        pytest.skip("CIS 216 not applied in this database")
    readable = set(_readable(k, "acp_contract.v_active_tour_atoms"))
    if _after_270(k):
        assert readable == set()                              # AA-712 X4: CIS 270 revokes the 216 grants
    else:
        assert {"atom_id", "tour_id", "place", "activity_type"} <= readable <= set(TOUR_ATOM_COLS)


def test_registered_once(cur):
    k, _ = cur
    k.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '254'")
    assert k.fetchone()[0] == 1
