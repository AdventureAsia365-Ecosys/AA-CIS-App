"""AA-814 — migration 302: acp_shared.facts kind / valid_until / updated_by, and DELETE to aa_app_user. Real Postgres.

conftest applies every migration, 302 included (acp_shared.facts comes from CIS 145, its policies from 229). These
tests check the replayed shape, run the file again directly (errors NOT swallowed) from the pre-302 state and twice
(idempotent), check both CHECKs, and check by behaviour that the tenant role deletes only its own tenant facts (never a
platform fact, never another tenant's). Every test leaves the post-302 state behind.
"""
import os
import sys
import uuid
from contextlib import contextmanager

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "302_facts_entry_kind_valid_until.sql")
PROBE = "aa814-migration-302-probe"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM acp_shared.facts WHERE provenance = %s", (PROBE,))
        c.execute("DELETE FROM shared.tenants WHERE slug LIKE 'aa814-%%'")
        _run(c)                     # the post-302 state other tests see (idempotent)
        conn.close()


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _columns(c):
    c.execute("""SELECT column_name, data_type, is_nullable, coalesce(column_default, '-')
                   FROM information_schema.columns
                  WHERE table_schema = 'acp_shared' AND table_name = 'facts'
                    AND column_name IN ('kind', 'valid_until', 'updated_by') ORDER BY column_name""")
    return c.fetchall()


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '302'")
    return c.fetchone()[0]


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-814 {tid[:8]}", f"aa814-{tid[:8]}"))
    return tid


def _fact(c, scope, tenant_id=None, **cols):
    fid = f"fact_{uuid.uuid4().hex[:10]}"
    names = ["fact_id", "scope", "tenant_id", "title", "body", "provenance", *cols]
    values = [fid, scope, tenant_id, "t", "b", PROBE, *cols.values()]
    c.execute(f"INSERT INTO acp_shared.facts ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(values))})",
              values)
    return fid


@contextmanager
def _as_tenant(c, guc):
    c.execute("BEGIN")
    try:
        c.execute("SET LOCAL ROLE aa_app_user")
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        yield c
    finally:
        c.execute("ROLLBACK")


EXPECTED_COLUMNS = [("kind", "text", "NO", "'other'::text"), ("updated_by", "text", "YES", "-"),
                    ("valid_until", "date", "YES", "-")]


def test_replay_applied_302(cur):
    assert _columns(cur) == EXPECTED_COLUMNS
    cur.execute("SELECT has_table_privilege('aa_app_user', 'acp_shared.facts', 'DELETE')")
    assert cur.fetchone()[0]
    assert _registered(cur) == 1


def test_302_from_the_pre_302_state_and_again(cur):
    fid = _fact(cur, "platform")
    cur.execute("""ALTER TABLE acp_shared.facts DROP CONSTRAINT facts_valid_until_ck, DROP CONSTRAINT facts_kind_check,
                   DROP COLUMN updated_by, DROP COLUMN valid_until, DROP COLUMN kind""")
    cur.execute("REVOKE DELETE ON acp_shared.facts FROM aa_app_user")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '302'")
    assert _columns(cur) == []
    _run(cur)
    _run(cur)                       # idempotent
    assert _columns(cur) == EXPECTED_COLUMNS
    assert _registered(cur) == 1
    cur.execute("SELECT kind, valid_until, updated_by FROM acp_shared.facts WHERE fact_id = %s", (fid,))
    assert cur.fetchone() == ("other", None, None)       # an existing row becomes 'other'


@pytest.mark.parametrize("cols", [{"kind": "rumour"}, {"stated_on": "2026-10-10", "valid_until": "2026-10-01"}])
def test_checks_refuse_an_unknown_kind_and_an_end_before_the_statement(cur, cols):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _fact(cur, "platform", **cols)


def test_known_kinds_and_dates_are_accepted(cur):
    for kind in ("price", "season", "visa_entry", "transfer_time", "booking_terms", "other"):
        _fact(cur, "platform", kind=kind, stated_on="2026-10-01", valid_until="2026-10-01")


def test_tenant_role_deletes_only_its_own_tenant_facts(cur):
    a, b = _tenant(cur), _tenant(cur)
    own, other, plat = _fact(cur, "tenant", a), _fact(cur, "tenant", b), _fact(cur, "platform")
    with _as_tenant(cur, a) as c:
        c.execute("DELETE FROM acp_shared.facts WHERE fact_id = ANY(%s)", ([own, other, plat],))
        assert c.rowcount == 1                           # the own fact only
        c.execute("SELECT fact_id FROM acp_shared.facts WHERE fact_id = ANY(%s)", ([own, other, plat],))
        assert {r[0] for r in c.fetchall()} == {plat}    # platform still readable, b's never visible
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute("DELETE FROM acp_shared.facts WHERE fact_id = ANY(%s)", ([own, other, plat],))
            assert c.rowcount == 0, guc
    with _as_tenant(cur, a) as c:                        # the new columns are writable on an own fact
        c.execute("UPDATE acp_shared.facts SET kind = 'price', valid_until = current_date, updated_by = 'tenant:x' "
                  "WHERE fact_id = %s", (own,))
        assert c.rowcount == 1
        c.execute("UPDATE acp_shared.facts SET kind = 'price' WHERE fact_id = %s", (plat,))
        assert c.rowcount == 0                           # platform facts stay read only
