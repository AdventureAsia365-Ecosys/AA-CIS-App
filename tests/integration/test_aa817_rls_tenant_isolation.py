"""AA-817 — tenant isolation by RLS for the role the tenant pool connects as (aa_app_user).

The guard the issue asks for: a non-owner, non-BYPASSRLS role with a forged app.tenant_id and a query
that FORGETS its `WHERE tenant_id = ...` reads 0 rows of another tenant. Covers the acp_shared tables
behind the tenant routes on the tenant pool (AA-544 stages 1/5, AA-815, AA-817 latest-piece):
angle_gate_request (113), content_piece (115), publish_log (116), facts (145, platform rows shared by
design) and gold_aa_internal.tenant_tour_versions (the stage-2 T2 table), all on the policy template of
migration 229. The CI DB does not create tenant_tour_versions (migration 001 is skipped), so each test
creates conftest's stand-in and re-applies 229 in its own rolled-back transaction.

Write side: a WHERE-less UPDATE touches only the own tenant's rows, a row can be neither handed to nor
inserted for another tenant (or under ''), and platform facts can be read but never written or
re-scoped (229's WITH CHECK; before 229 this was a strict xfail).

Lesson S236: on RDS, check RLS by behaviour (row counts under a foreign tenant), not by role
attributes. Seeding runs as the CI superuser; reads run under SET ROLE aa_app_user in the same
transaction, which is rolled back (nothing to clean up).
"""
import json
import os
import sys
import uuid
from contextlib import contextmanager

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TTV_STANDIN_DDL  # noqa: E402

MIGRATION_229 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                             "229_tenant_rls_policies.sql")

TENANT_ROLE = "aa_app_user"
# table -> True when rows with tenant_id NULL (platform scope) are visible to every tenant by design
TENANT_TABLES = {
    "acp_shared.angle_gate_request": False,
    "acp_shared.content_piece": False,
    "acp_shared.publish_log": False,
    "acp_shared.facts": True,
    "gold_aa_internal.tenant_tour_versions": False,
}


def _migration_229_body() -> str:
    with open(MIGRATION_229) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(TTV_STANDIN_DDL)
        c.execute(_migration_229_body())
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-817 {tid[:8]}", f"aa817-{tid[:8]}"))
    return tid


def _seed(c, tid):
    """One row per tenant table for this tenant."""
    c.execute("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status) "
              "VALUES (%s, 'aa817-atom', 'approved') RETURNING request_id", (tid,))
    req = c.fetchone()[0]
    c.execute("INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, "
              "content_text, status, gate_ledger, repair_log) "
              "VALUES (%s, %s, 1, 'x', 'approved', %s::jsonb, '[]'::jsonb) RETURNING piece_id",
              (tid, req, json.dumps([])))
    piece = c.fetchone()[0]
    c.execute("INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status) "
              "VALUES (%s, %s, 'blog', 'published')", (piece, tid))
    c.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
              "VALUES (%s, 'tenant', %s, 't', 'b', 'aa817-test')", (f"fact_{uuid.uuid4().hex[:10]}", tid))
    c.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, edit_source) "
              "VALUES (%s, %s, 'ai_generated')", (tid, str(uuid.uuid4())))


@pytest.fixture
def two_tenants(cur):
    a, b = _tenant(cur), _tenant(cur)
    _seed(cur, a)
    _seed(cur, b)
    cur.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                "VALUES (%s, 'platform', NULL, 't', 'b', 'aa817-test')", (f"fact_{uuid.uuid4().hex[:10]}",))
    return a, b


@contextmanager
def _as_tenant(c, guc):
    """Run the block as the tenant role with app.tenant_id = guc, inside a savepoint that is rolled
    back afterwards (resets the role, the GUC and any write, also after an error)."""
    c.execute("SAVEPOINT aa817")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        yield c
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa817")


def _visible_tenants(c, table, guc):
    """As the tenant role, with app.tenant_id = guc, the tenant_ids a WHERE-less query returns."""
    with _as_tenant(c, guc):
        c.execute(f"SELECT DISTINCT tenant_id::text FROM {table}")   # no WHERE on purpose
        return {r[0] for r in c.fetchall()}


def _rls_violation(c, sql, params=()):
    """The statement is refused by the policy's (implicit) WITH CHECK, not by a missing grant."""
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="row-level security"):
        c.execute(sql, params)


def test_tenant_role_is_not_privileged(cur):
    """The premise of every check below: the tenant role is filtered by RLS, owns none of the tables."""
    cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (TENANT_ROLE,))
    assert cur.fetchone() == (False, False)
    for table in TENANT_TABLES:
        cur.execute("SELECT c.relrowsecurity, pg_has_role(%s, c.relowner, 'USAGE') FROM pg_class c "
                    "WHERE c.oid = %s::regclass", (TENANT_ROLE, table))
        rls, owns = cur.fetchone()
        assert rls, f"{table}: ROW LEVEL SECURITY is not enabled"
        assert not owns, f"{table}: {TENANT_ROLE} is (or inherits) the owner, so RLS does not apply to it"
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (TENANT_ROLE, table))
        assert cur.fetchone()[0], f"{table}: {TENANT_ROLE} has no SELECT (tenant pool would 500)"


@pytest.mark.parametrize("table", sorted(TENANT_TABLES))
def test_forged_tenant_reads_no_foreign_rows(cur, two_tenants, table):
    a, b = two_tenants
    platform = {None} if TENANT_TABLES[table] else set()

    # own tenant: its rows only, never the other tenant's (the query has no WHERE tenant_id)
    assert _visible_tenants(cur, table, a) == {a} | platform
    assert _visible_tenants(cur, table, b) == {b} | platform
    # forged tenant id that owns nothing
    assert _visible_tenants(cur, table, str(uuid.uuid4())) == platform
    # empty GUC (what a pooled connection holds after any set_config(..., true)): no rows, no error
    assert _visible_tenants(cur, table, "") == platform


def test_owner_still_sees_every_tenant(cur, two_tenants):
    """Sanity check of the fixture: the rows exist, so the zero counts above come from RLS."""
    a, b = two_tenants
    for table in TENANT_TABLES:
        cur.execute(f"SELECT count(DISTINCT tenant_id) FROM {table} WHERE tenant_id::text = ANY(%s)", ([a, b],))
        assert cur.fetchone()[0] == 2, table


# Write side. Every policy here is FOR ALL TO aa_app_user with an explicit WITH CHECK (229), so a
# WHERE-less UPDATE touches only the own tenant's rows and a row can neither be written for nor handed
# to another tenant. facts is split: tenant rows like the others, platform rows read-only.
WRITE_TABLES = sorted(TENANT_TABLES)
_INSERT_FOR = {
    "acp_shared.angle_gate_request": ("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status) "
                                      "VALUES (%s, 'aa817-atom', 'approved')"),
    "acp_shared.facts": ("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                         "VALUES ('fact_' || substr(md5(random()::text), 1, 10), 'tenant', %s, 't', 'b', 'p')"),
    "gold_aa_internal.tenant_tour_versions": ("INSERT INTO gold_aa_internal.tenant_tour_versions "
                                              "(tenant_id, edit_source) VALUES (%s, 'ai_generated')"),
}


@pytest.mark.parametrize("table", WRITE_TABLES)
def test_forged_tenant_writes_no_foreign_rows(cur, two_tenants, table):
    a, b = two_tenants
    tenant_only = " WHERE tenant_id IS NOT NULL" if TENANT_TABLES[table] else ""   # facts: tenant rows
    for guc, expected in ((a, 1), (str(uuid.uuid4()), 0), ("", 0)):
        with _as_tenant(cur, guc) as c:
            c.execute(f"UPDATE {table} SET tenant_id = tenant_id{tenant_only}")   # no tenant filter on purpose
            assert c.rowcount == expected, (table, guc)
    with _as_tenant(cur, a) as c:
        _rls_violation(c, f"UPDATE {table} SET tenant_id = %s{tenant_only}", (b,))   # hand own row to b


@pytest.mark.parametrize("table", sorted(_INSERT_FOR))
def test_forged_tenant_cannot_insert_for_another_tenant(cur, two_tenants, table):
    a, b = two_tenants
    with _as_tenant(cur, a) as c:
        _rls_violation(c, _INSERT_FOR[table], (b,))
    with _as_tenant(cur, "") as c:
        _rls_violation(c, _INSERT_FOR[table], (a,))
    with _as_tenant(cur, a) as c:              # the own tenant still can (the stage-2 rewrite INSERT)
        c.execute(_INSERT_FOR[table], (a,))
        assert c.rowcount == 1


def test_tenant_role_cannot_write_platform_facts(cur, two_tenants):
    """Was a strict xfail before 229 (policy 145 FOR ALL `scope = 'platform' OR own`, no WITH CHECK)."""
    a, _ = two_tenants
    with _as_tenant(cur, a) as c:
        c.execute("UPDATE acp_shared.facts SET body = body WHERE scope = 'platform'")
        assert c.rowcount == 0, "tenant role updated platform facts"
    with _as_tenant(cur, a) as c:
        _rls_violation(c, "INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                          "VALUES ('fact_' || substr(md5(random()::text), 1, 10), 'platform', NULL, 't', 'b', 'p')")
    with _as_tenant(cur, a) as c:              # an own fact cannot be turned into a platform fact
        _rls_violation(c, "UPDATE acp_shared.facts SET scope = 'platform', tenant_id = NULL "
                          "WHERE tenant_id IS NOT NULL")
