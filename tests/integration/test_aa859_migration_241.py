"""AA-859 — migration 241: no tenant-role privilege on CIS relations without RLS (catalog contract kept),
angle_gate_option under RLS through its parent request, tenant_integrations column grants for connect /
test-connection.

conftest applies 241 as the superuser with every other migration. Each test here works in a transaction
that is rolled back, mostly re-applying the file body. Checked:
  * conftest really applied it (it swallows migration errors, lesson AA-741), the committed DB meets the
    invariant, and guard 3's seed left nothing behind;
  * the invariant query of the helpers is the one the migration's guard 4 runs;
  * shape: RLS on without FORCE, exactly tenant_isolation FOR ALL TO aa_app_user through
    angle_gate_request; aa_app_user's exact privileges on angle_gate_option and tenant_integrations;
  * the loop revokes table and column privileges on relations without RLS, keeps the catalog contract,
    and the file is idempotent, also with rows of several tenants (guard 3 on real rows) and on an empty
    table (guard 3 seeds and rolls back);
  * the guards refuse: an unknown policy, FORCE, another grantee on angle_gate_option, a member of
    aa_app_user, a PUBLIC write grant, an aa_app_user grant this file cannot revoke (another app's role or
    schema, a catalog column grant), an aclitem from another grantor, an extra privilege on
    tenant_integrations, and a policy that lets a foreign tenant read (guard 3 by behaviour);
  * applied as an RDS-like runner (owner, NOSUPERUSER, NOBYPASSRLS, CREATEROLE, not a member of
    aa_app_user nor of catalog_owner, lesson S237: SET LOCAL SESSION AUTHORIZATION) from the pre-241
    state: the catalog_owner views it cannot act for are skipped without error, guard 3 runs through the
    transaction-scoped membership, which is gone afterwards; without that GRANT the apply fails
    (mutation check).
The per-table read/write isolation as aa_app_user is in test_aa817_rls_tenant_isolation.py
(CHILD_RLS_TABLES, COLUMN_WRITE_TABLES, test_tenant_role_has_no_privilege_on_any_relation_without_rls).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import (  # noqa: E402
    NO_PRIVILEGE_WITHOUT_RLS_SQL, migration_body, migration_path, undo_241_for_239_reapply,
)

TENANT_ROLE = "aa_app_user"
RUNNER = "zz241_rds_runner"   # stands in for aa_cis_admin on RDS
OPTION = "acp_shared.angle_gate_option"
INTEGRATIONS = "shared.tenant_integrations"
OPTION_PRIVS = {"SELECT", "UPDATE(chosen)"}
INTEGRATION_PRIVS = {
    "SELECT", "DELETE",
    "INSERT(tenant_id)", "INSERT(integration_type)", "INSERT(config)", "INSERT(secret_key)", "INSERT(connected_at)",
    "UPDATE(config)", "UPDATE(secret_key)", "UPDATE(updated_at)", "UPDATE(last_verified_at)",
    "UPDATE(last_verify_error)",
}
TEMP_GRANT_LINE = "EXECUTE format('GRANT aa_app_user TO %I', current_user);"


def _body() -> str:
    return migration_body(241)


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    """A rolled-back transaction with 241 re-applied."""
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(_body())
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-859 241 {tid[:8]}", f"aa859m-{tid[:8]}"))
    return tid


def _seed_options(c, tid, n):
    """One request of this tenant with n options (idx 0..n-1)."""
    c.execute("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status) "
              "VALUES (%s, 'aa859-atom', 'pending_choice') RETURNING request_id", (tid,))
    req = c.fetchone()[0]
    for idx in range(n):
        c.execute(f"INSERT INTO {OPTION} (request_id, idx, name, why_it_works, formula_fit, best_final_style) "
                  "VALUES (%s, %s, 'aa859', 'x', 'x', 'x')", (req, idx))
    return req


@pytest.fixture
def seeded(cur):
    """Tenant a with 2 options (the busiest tenant guard 3 checks), tenant b with 1."""
    a, b = _tenant(cur), _tenant(cur)
    _seed_options(cur, a, 2)
    _seed_options(cur, b, 1)
    return a, b


def _count_options(c, tenants):
    c.execute(f"SELECT count(*) FROM {OPTION} o JOIN acp_shared.angle_gate_request r USING (request_id) "
              "WHERE r.tenant_id::text = ANY(%s)", (list(tenants),))
    return c.fetchone()[0]


def _privileges(c, table):
    """aa_app_user's privileges on the table: 'PRIV' (table level) and 'PRIV(column)'."""
    c.execute("""
        SELECT a.privilege_type FROM pg_class cl, aclexplode(cl.relacl) a
         WHERE cl.oid = %s::regclass AND a.grantee = %s::regrole
        UNION ALL
        SELECT a.privilege_type || '(' || att.attname || ')'
          FROM pg_attribute att, aclexplode(att.attacl) a
         WHERE att.attrelid = %s::regclass AND att.attacl IS NOT NULL AND NOT att.attisdropped
           AND a.grantee = %s::regrole""", (table, TENANT_ROLE, table, TENANT_ROLE))
    return {r[0] for r in c.fetchall()}


def _catalog_contract(c):
    c.execute("SELECT c.oid::regclass::text FROM pg_class c, aclexplode(c.relacl) a "
              "WHERE c.relnamespace = 'catalog'::regnamespace AND a.grantee = %s::regrole "
              "AND a.privilege_type = 'SELECT' ORDER BY 1", (TENANT_ROLE,))
    return [r[0] for r in c.fetchall()]


def _first_column(c, table):
    c.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
              "ORDER BY attnum LIMIT 1", (table,))
    return c.fetchone()[0]


def _can_set_tenant_role(c) -> bool:
    c.execute("SAVEPOINT aa241set")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        return True
    except psycopg2.errors.InsufficientPrivilege:
        return False
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa241set")


def _to_pre_241(c):
    """Back to Dev's state before 241: angle_gate_option without RLS / policy and with SELECT (239),
    tenant_integrations with SELECT + DELETE only (237), version not registered."""
    undo_241_for_239_reapply(c)
    c.execute(f"GRANT SELECT ON {OPTION} TO {TENANT_ROLE}")
    c.execute(f"REVOKE INSERT, UPDATE ON {INTEGRATIONS} FROM {TENANT_ROLE}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '241'")


def _as_rds_like_runner(c):
    """Hand the schemas and tables 241 touches to a NOSUPERUSER, NOBYPASSRLS, CREATEROLE role that may
    not SET ROLE aa_app_user and is not a member of catalog_owner (as aa_cis_admin on RDS), then become
    it. SET LOCAL SESSION AUTHORIZATION, not SET ROLE: SET ROLE permission is checked against the session
    user (lesson S237). Owner exemption (no FORCE) stands in for its RLS exemption on these tables.
    Rolled back with the test transaction."""
    c.execute("SELECT current_setting('server_version_num')::int")
    pg16 = c.fetchone()[0] >= 160000
    c.execute(f"CREATE ROLE {RUNNER} NOSUPERUSER NOBYPASSRLS CREATEROLE INHERIT")
    if pg16:
        # PG16+: CREATEROLE grants only roles it holds ADMIN on (RDS PG15: any non-superuser role).
        # ADMIN without SET does not allow SET ROLE, so guard 3 still takes its GRANT path.
        c.execute(f"GRANT {TENANT_ROLE} TO {RUNNER} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
    for schema in ("acp_shared", "shared"):
        c.execute(f"ALTER SCHEMA {schema} OWNER TO {RUNNER}")
    for table in (OPTION, "acp_shared.angle_gate_request", "acp_shared.year_plan", "shared.tenants",
                  INTEGRATIONS, "shared.schema_versions"):
        c.execute(f"ALTER TABLE {table} OWNER TO {RUNNER}")
    c.execute(f"SET LOCAL SESSION AUTHORIZATION {RUNNER}")


# ── applied, and the invariant is the guard's ─────────────────────────────────────────────────────

def test_applied_by_conftest():
    """241 really applied to the shared test DB (conftest swallows migration errors), the committed DB
    meets the invariant, and guard 3's seed (CI: empty table) was rolled back."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '241'")
        assert c.fetchone(), "241 did not apply in the test DB"
        c.execute("SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass", (OPTION,))
        assert c.fetchone()[0]
        c.execute("SELECT 1 FROM pg_policies WHERE schemaname = 'acp_shared' AND tablename = 'angle_gate_option' "
                  "AND policyname = 'tenant_isolation'")
        assert c.fetchone()
        c.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
        assert c.fetchall() == []
        c.execute("SELECT count(*) FROM acp_shared.angle_gate_request WHERE atom_id = 'aa241-guard'")
        assert c.fetchone()[0] == 0
    finally:
        conn.rollback()
        conn.close()


def test_invariant_query_is_the_migration_guard():
    """One SQL text for guard 4, the CI test and the probe v3 verdict (AA-859 plan D1)."""
    with open(migration_path(241)) as f:
        migration = " ".join(f.read().split())
    assert " ".join(NO_PRIVILEGE_WITHOUT_RLS_SQL.split()) in migration


# ── shape ─────────────────────────────────────────────────────────────────────────────────────────

def test_policy_shape(cur):
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (OPTION,))
    assert cur.fetchone() == (True, False)
    cur.execute("SELECT policyname, permissive, cmd, roles::text[], qual, with_check FROM pg_policies "
                "WHERE schemaname = 'acp_shared' AND tablename = 'angle_gate_option'")
    rows = cur.fetchall()
    assert len(rows) == 1
    name, permissive, cmd, roles, qual, with_check = rows[0]
    assert (name, permissive, cmd, roles) == ("tenant_isolation", "PERMISSIVE", "ALL", [TENANT_ROLE])
    for expr in (qual, with_check):
        assert "angle_gate_request r" in expr and "r.request_id = angle_gate_option.request_id" in expr
        assert "r.tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid" in expr


def test_tenant_role_privileges(cur):
    assert _privileges(cur, OPTION) == OPTION_PRIVS
    assert _privileges(cur, INTEGRATIONS) == INTEGRATION_PRIVS


# ── the loop ──────────────────────────────────────────────────────────────────────────────────────

def test_loop_revokes_table_and_column_privileges(cur):
    """Grants of the kinds Dev has (and one column grant) on relations without RLS are all revoked."""
    col = _first_column(cur, "acp_shared.tenant_atom_state")
    cur.execute(f"GRANT SELECT, INSERT, UPDATE ON acp_shared.year_plan TO {TENANT_ROLE}")
    cur.execute(f"GRANT UPDATE ({col}) ON acp_shared.tenant_atom_state TO {TENANT_ROLE}")
    cur.execute(f"GRANT SELECT ON shared.tenants, shared.job TO {TENANT_ROLE}")
    cur.execute(f"GRANT SELECT ON acp_contract.v_active_tour_atoms TO {TENANT_ROLE}")
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert len(cur.fetchall()) == 7, "fixture: the grants must be violations before the re-run"
    cur.execute(_body())
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert cur.fetchall() == []
    for table in ("acp_shared.year_plan", "acp_shared.tenant_atom_state", "shared.tenants", "shared.job",
                  "acp_contract.v_active_tour_atoms"):
        cur.execute("SELECT has_any_column_privilege(%s, %s, 'SELECT, INSERT, UPDATE, REFERENCES')",
                    (TENANT_ROLE, table))
        assert not cur.fetchone()[0], table


def test_catalog_contract_is_kept(cur):
    """SELECT on catalog_owner's catalog.* views (225 / 228) is the cross-app contract: untouched."""
    before = _catalog_contract(cur)
    assert len(before) >= 13, before
    cur.execute(_body())
    assert _catalog_contract(cur) == before


def test_rerun_with_rows_is_idempotent(cur, seeded):
    """Guard 3 runs on real rows (busiest tenant a, 2 options) and writes nothing."""
    a, b = seeded
    cur.execute(_body())
    cur.execute(_body())
    assert _count_options(cur, (a, b)) == 3
    assert _privileges(cur, OPTION) == OPTION_PRIVS
    cur.execute("SELECT count(*) FROM acp_shared.angle_gate_request WHERE atom_id = 'aa241-guard'")
    assert cur.fetchone()[0] == 0


def test_empty_table_seed_is_rolled_back(cur):
    """CI case: guard 3 seeds one request and one option and rolls them back."""
    cur.execute(f"DELETE FROM {OPTION}")
    cur.execute(_body())
    cur.execute("SELECT count(*) FROM acp_shared.angle_gate_request WHERE atom_id = 'aa241-guard'")
    assert cur.fetchone()[0] == 0
    cur.execute(f"SELECT count(*) FROM {OPTION}")
    assert cur.fetchone()[0] == 0


def test_first_apply_from_the_pre_241_state(cur, seeded):
    """As on Dev: SELECT on angle_gate_option is revoked by the loop and granted back with RLS."""
    a, b = seeded
    _to_pre_241(cur)
    cur.execute(_body())
    assert _privileges(cur, OPTION) == OPTION_PRIVS
    assert _privileges(cur, INTEGRATIONS) == INTEGRATION_PRIVS
    assert _count_options(cur, (a, b)) == 3
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '241'")
    assert cur.fetchone()


# ── guards ────────────────────────────────────────────────────────────────────────────────────────

def test_guard_refuses_unknown_policy(cur):
    cur.execute(f"CREATE POLICY zz241_extra ON {OPTION} USING (true)")
    with pytest.raises(psycopg2.errors.RaiseException, match="has policies"):
        cur.execute(_body())


def test_guard_refuses_force(cur):
    cur.execute(f"ALTER TABLE {OPTION} FORCE ROW LEVEL SECURITY")
    with pytest.raises(psycopg2.errors.RaiseException, match="FORCE ROW LEVEL SECURITY"):
        cur.execute(_body())


def test_guard_refuses_another_grantee_on_angle_gate_option(cur):
    """With RLS on and a policy TO aa_app_user only, any other grantee would read 0 rows."""
    cur.execute(f"GRANT SELECT ON {OPTION} TO tripplanner")
    with pytest.raises(psycopg2.errors.RaiseException, match="hold privileges on acp_shared.angle_gate_option"):
        cur.execute(_body())


def test_guard_refuses_a_member_of_the_tenant_role(cur):
    cur.execute("CREATE ROLE zz241_member")
    cur.execute(f"GRANT {TENANT_ROLE} TO zz241_member")
    with pytest.raises(psycopg2.errors.RaiseException, match="members of aa_app_user"):
        cur.execute(_body())


def test_guard_refuses_a_public_write_grant(cur):
    cur.execute("GRANT INSERT ON acp_shared.year_plan TO PUBLIC")
    with pytest.raises(psycopg2.errors.RaiseException, match="PUBLIC holds write privileges"):
        cur.execute(_body())


def test_guard_refuses_a_grant_on_another_apps_role(cur):
    """Owned by a role the runner may not act for (CI superuser: the explicit exclusion)."""
    cur.execute("CREATE TABLE shared.zz241_tp (x int)")
    cur.execute("ALTER TABLE shared.zz241_tp OWNER TO tripplanner")
    cur.execute(f"GRANT SELECT ON shared.zz241_tp TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="cannot revoke.*zz241_tp"):
        cur.execute(_body())


def test_guard_refuses_a_grant_in_another_apps_schema(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS booking")
    cur.execute("CREATE TABLE booking.zz241_bk (x int)")
    cur.execute(f"GRANT SELECT ON booking.zz241_bk TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="cannot revoke.*zz241_bk"):
        cur.execute(_body())


def test_guard_refuses_a_catalog_column_grant(cur):
    """The contract is table-level SELECT only."""
    col = _first_column(cur, "catalog.v_tour")
    cur.execute(f"GRANT SELECT ({col}) ON catalog.v_tour TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="cannot revoke.*catalog.v_tour"):
        cur.execute(_body())


def test_guard_refuses_another_grantor(cur):
    """A REVOKE run as the owner would silently leave an aclitem granted by someone else."""
    cur.execute("CREATE ROLE zz241_grantor")
    cur.execute("GRANT USAGE ON SCHEMA acp_shared TO zz241_grantor")
    cur.execute("GRANT SELECT ON acp_shared.year_plan TO zz241_grantor WITH GRANT OPTION")
    cur.execute("SET LOCAL ROLE zz241_grantor")
    cur.execute(f"GRANT SELECT ON acp_shared.year_plan TO {TENANT_ROLE}")
    cur.execute("RESET ROLE")
    with pytest.raises(psycopg2.errors.RaiseException, match="granted by a role other than the owner"):
        cur.execute(_body())


def test_guard_refuses_extra_privilege_on_tenant_integrations(cur):
    cur.execute(f"GRANT TRUNCATE ON {INTEGRATIONS} TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="on shared.tenant_integrations"):
        cur.execute(_body())


def test_guard_3_catches_a_policy_that_leaks(cur, seeded):
    """Mutation: a policy `... OR true` still passes the shape check (ILIKE), so guard 3's counts under a
    foreign tenant are what refuses it."""
    body = _body()
    leaky = body.replace("'')::uuid))\n    WITH CHECK", "'')::uuid) OR true)\n    WITH CHECK", 1)
    assert leaky != body
    with pytest.raises(psycopg2.errors.RaiseException, match="aa_app_user reads 3 rows"):
        cur.execute(leaky)


# ── as an RDS-like runner (how DB Migrate applies it on Dev) ─────────────────────────────────────

def test_applies_as_rds_like_runner(cur, seeded):
    a, b = seeded
    catalog_before = _catalog_contract(cur)
    _to_pre_241(cur)
    cur.execute(f"GRANT SELECT ON acp_shared.year_plan TO {TENANT_ROLE}")      # in scope after the hand-over
    _as_rds_like_runner(cur)
    assert not _can_set_tenant_role(cur), "fixture: the runner must not be able to SET ROLE aa_app_user"
    cur.execute("SELECT pg_has_role(current_user, 'catalog_owner', 'USAGE')")
    assert not cur.fetchone()[0], "fixture: the runner must not act for catalog_owner"
    cur.execute(_body())                       # raises on any guard failure
    assert not _can_set_tenant_role(cur), "the transaction-scoped membership was not revoked"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '241'")
    assert cur.fetchone()
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(uuid.uuid4()),))
    assert _count_options(cur, (a, b)) == 3, "the runner (main pool) must still read every option"
    cur.execute("RESET SESSION AUTHORIZATION")
    assert _catalog_contract(cur) == catalog_before, "the catalog_owner views must be skipped, not revoked"
    assert _privileges(cur, OPTION) == OPTION_PRIVS
    assert _privileges(cur, INTEGRATIONS) == INTEGRATION_PRIVS
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert cur.fetchall() == []


def test_rds_like_runner_without_the_temporary_grant_fails(cur, seeded):
    """Mutation check (lesson S237): without the transaction-scoped GRANT the RDS-like apply cannot SET
    ROLE aa_app_user, so the test above really exercises that path."""
    body = _body()
    assert body.count(TEMP_GRANT_LINE) == 1
    _to_pre_241(cur)
    _as_rds_like_runner(cur)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied to set role"):
        cur.execute(body.replace(TEMP_GRANT_LINE, "NULL;"))


def test_rds_like_runner_refuses_a_grant_it_cannot_revoke(cur):
    """A relation owned by a role the runner is not a member of (as catalog_owner on Dev), outside the
    catalog contract: guard 0 raises instead of the REVOKE failing or revoking nothing."""
    cur.execute("CREATE ROLE zz241_other")
    cur.execute("CREATE TABLE shared.zz241_other_t (x int)")
    cur.execute("ALTER TABLE shared.zz241_other_t OWNER TO zz241_other")
    cur.execute(f"GRANT SELECT ON shared.zz241_other_t TO {TENANT_ROLE}")
    _as_rds_like_runner(cur)
    with pytest.raises(psycopg2.errors.RaiseException, match="cannot revoke.*zz241_other_t"):
        cur.execute(_body())
