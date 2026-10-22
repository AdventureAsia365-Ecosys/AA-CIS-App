"""AA-859 wave 4 — migration 265: acp_shared.subject / quarter_plan / quarter_plan_version / year_plan and
acp_deliver.tenant_tour_pages under RLS for the tenant pool, the X1 cis_app parity policy, the exact grants of the moved
statements, and the platform read allowlist (7 relations, table-level SELECT) with USAGE on acp_contract.

conftest applies 265 as the superuser with every other migration (and swallows migration errors, lesson AA-741, so
test_applied_by_conftest checks the result). Each other test works in a transaction that is rolled back, mostly
re-applying the file body. Checked:
  * shape: RLS on without FORCE, the exact policy set per table and each policy's shape; aa_app_user's exact table /
    column privileges on the five tables, exactly SELECT on the 7 platform relations, USAGE on acp_contract;
  * guard 5 runs invariant v2 (NO_PRIVILEGE_WITHOUT_RLS_SQL_V2); the committed DB meets it; no privilege on the
    acp_contract views;
  * cis_app (the CIS runtime since X1 S18) still reads every row, without a GUC and under a forged one;
  * idempotent re-run, a first apply from the pre-265 state, and a re-run puts back the exact grant set;
  * the guards refuse: FORCE, an unknown policy, a LOGIN role or PUBLIC with a grant, a member of aa_app_user, a view
    owned by a role RLS would filter, a non-canonical tenant_tour_pages id, a platform relation with RLS or a tenant
    column, a table or column privilege on a relation without RLS, one on an acp_contract view.
Guard 6 (cis_app parity by behaviour) runs only the DB Migrate way; here it is skipped with a NOTICE. The tenant role's
isolation by behaviour is in test_aa817_rls_tenant_isolation.py and the routes in test_aa859_wave4_pool_routes.py.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import (  # noqa: E402
    NO_PRIVILEGE_WITHOUT_RLS_SQL, NO_PRIVILEGE_WITHOUT_RLS_SQL_V2, PLATFORM_READ_ALLOWLIST, migration_body,
    platform_read_privileges, platform_read_v1_rows,
    migration_path, revoke_platform_reads,
)

TENANT_ROLE = "aa_app_user"
SUBJECT, QP, QPV, YP, PAGES = ("acp_shared.subject", "acp_shared.quarter_plan", "acp_shared.quarter_plan_version",
                               "acp_shared.year_plan", "acp_deliver.tenant_tour_pages")
TABLES = (SUBJECT, QP, QPV, YP, PAGES)
PRIVS = {
    SUBJECT: {"SELECT", "UPDATE(state)"},
    YP: {"SELECT", "INSERT(tenant_id)", "INSERT(year)"},
    QP: {"SELECT", "INSERT(tenant_id)", "INSERT(year)", "INSERT(quarter)", "INSERT(year_plan_id)",
         "UPDATE(year_plan_id)", "UPDATE(current_version_id)"},
    QPV: {"SELECT", "INSERT(plan_id)", "INSERT(version_no)", "INSERT(payload)", "INSERT(source)",
          "INSERT(approval_status)", "UPDATE(approval_status)", "UPDATE(approved_by)", "UPDATE(approved_at)"},
    PAGES: {"SELECT"},
}
UUID_QUAL = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
TEXT_QUAL = "(tenant_id = NULLIF(current_setting('app.tenant_id'::text, true), ''::text))"
MASTER_QUAL = "(tenant_id = '00000000-0000-0000-0000-000000000001'::text)"


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _privileges(c, table):
    c.execute("""
        SELECT a.privilege_type FROM pg_class cl, aclexplode(cl.relacl) a
         WHERE cl.oid = %s::regclass AND a.grantee = %s::regrole
        UNION ALL
        SELECT a.privilege_type || '(' || att.attname || ')'
          FROM pg_attribute att, aclexplode(att.attacl) a
         WHERE att.attrelid = %s::regclass AND att.attacl IS NOT NULL AND NOT att.attisdropped
           AND a.grantee = %s::regrole""", (table, TENANT_ROLE, table, TENANT_ROLE))
    return {r[0] for r in c.fetchall()}


def _policies(c, table):
    c.execute("""SELECT polname, polpermissive, polcmd, polroles::regrole[]::text[],
                        pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid)
                   FROM pg_policy WHERE polrelid = %s::regclass ORDER BY polname""", (table,))
    return {r[0]: r[1:] for r in c.fetchall()}


def _to_pre_265(c):
    """Dev's state before 265: no RLS, no policy, no aa_app_user privilege on the five (241 revoked it), none on the 7
    platform relations, no USAGE on acp_contract (266 needs it too: its table loses the grant first)."""
    c.execute(f"REVOKE ALL ON acp_contract.tour_atoms FROM {TENANT_ROLE}")
    for t in TABLES:
        c.execute(f"REVOKE ALL ON {t} FROM {TENANT_ROLE}")
        for pol in ("tenant_isolation", "aa_app_user_aa_master_read", f"{t.split('.')[1]}_cis_app_all"):
            c.execute(f"DROP POLICY IF EXISTS {pol} ON {t}")
        c.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    revoke_platform_reads(c)
    c.execute(f"REVOKE USAGE ON SCHEMA acp_contract FROM {TENANT_ROLE}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '265'")


def _apply(c):
    c.execute(migration_body(265))


def _refused(c, match):
    with pytest.raises(psycopg2.errors.RaiseException, match=match):
        _apply(c)


# ── applied, shape ────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '265'")
        assert c.fetchone(), "conftest swallowed an error in 265 (see its log)"
        for t in TABLES:
            c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (t,))
            assert c.fetchone() == (True, False), t
    finally:
        conn.close()


@pytest.mark.parametrize("table", (SUBJECT, QP, YP))
def test_uuid_tables_policy_shape(cur, table):
    pols = _policies(cur, table)
    rel = table.split(".")[1]
    assert set(pols) == {"tenant_isolation", f"{rel}_cis_app_all"}
    assert pols["tenant_isolation"] == (True, "*", ["aa_app_user"], UUID_QUAL, UUID_QUAL)
    assert pols[f"{rel}_cis_app_all"] == (True, "*", ["cis_app"], "true", "true")


def test_version_policy_goes_through_the_plan(cur):
    pols = _policies(cur, QPV)
    assert set(pols) == {"tenant_isolation", "quarter_plan_version_cis_app_all"}
    permissive, cmd, roles, qual, with_check = pols["tenant_isolation"]
    assert (permissive, cmd, roles) == (True, "*", ["aa_app_user"])
    assert "acp_shared.quarter_plan qp" in qual and "qp.plan_id = quarter_plan_version.plan_id" in qual
    assert "qp.tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid" in qual
    assert with_check == qual


def test_pages_policy_shape(cur):
    pols = _policies(cur, PAGES)
    assert set(pols) == {"tenant_isolation", "aa_app_user_aa_master_read", "tenant_tour_pages_cis_app_all"}
    assert pols["tenant_isolation"] == (True, "*", ["aa_app_user"], TEXT_QUAL, TEXT_QUAL)
    assert pols["aa_app_user_aa_master_read"] == (True, "r", ["aa_app_user"], MASTER_QUAL, None)


def test_tenant_role_privileges(cur):
    for t in TABLES:
        assert _privileges(cur, t) == PRIVS[t], t
        cur.execute("SELECT has_table_privilege(%s, %s, 'DELETE, TRUNCATE')", (TENANT_ROLE, t))
        assert not cur.fetchone()[0], t
    for rel in PLATFORM_READ_ALLOWLIST:
        assert _privileges(cur, rel) == platform_read_privileges(rel), rel
    cur.execute("SELECT has_schema_privilege(%s, 'acp_contract', 'USAGE')", (TENANT_ROLE,))
    assert cur.fetchone()[0]


def test_tenant_role_has_nothing_on_the_acp_contract_views(cur):
    cur.execute("""SELECT c.oid::regclass::text FROM pg_class c
                    WHERE c.relnamespace = 'acp_contract'::regnamespace AND c.relkind IN ('v', 'm')
                      AND (has_table_privilege(%s, c.oid, 'SELECT, INSERT, UPDATE, DELETE')
                           OR has_any_column_privilege(%s, c.oid, 'SELECT'))""", (TENANT_ROLE, TENANT_ROLE))
    assert cur.fetchall() == []


def test_invariant_v2_is_the_migration_guard():
    sql = open(migration_path(265)).read()
    assert NO_PRIVILEGE_WITHOUT_RLS_SQL_V2 in sql


def test_committed_db_meets_invariant_v2_and_v1_only_misses_the_allowlist(cur):
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL_V2)
    assert cur.fetchall() == []
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert {(r[0], r[1], r[2]) for r in cur.fetchall()} <= platform_read_v1_rows()


# ── cis_app parity ────────────────────────────────────────────────────────────

def test_cis_app_reads_every_row_without_and_under_a_forged_guc(cur):
    """The CIS runtime is not the owner: without its parity policy it would read 0 rows the moment RLS is on. The CI
    replay skips 244, so its SELECT is granted here, inside the rolled-back transaction."""
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
                (tid, f"AA-859 265 {tid[:8]}", f"aa859-265-{tid[:8]}"))
    cur.execute("INSERT INTO acp_shared.year_plan (tenant_id, year) VALUES (%s, 2031)", (tid,))
    cur.execute("INSERT INTO acp_shared.quarter_plan (tenant_id, year, quarter) VALUES (%s, 2031, 1) RETURNING plan_id",
                (tid,))
    cur.execute(f"INSERT INTO {QPV} (plan_id, version_no) VALUES (%s, 1)", (cur.fetchone()[0],))
    cur.execute(f"INSERT INTO {PAGES} (tenant_id, tour_id, url) VALUES (%s, gen_random_uuid(), 'https://x')", (tid,))
    cur.execute("GRANT USAGE ON SCHEMA acp_shared, acp_deliver TO cis_app")
    for t in TABLES:
        cur.execute(f"GRANT SELECT ON {t} TO cis_app")
        cur.execute(f"SELECT count(*) FROM {t}")
        total = cur.fetchone()[0]
        for guc in ("", str(uuid.uuid4())):
            cur.execute("SAVEPOINT p")
            cur.execute("SET LOCAL ROLE cis_app")
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
            cur.execute(f"SELECT count(*) FROM {t}")
            assert cur.fetchone()[0] == total, (t, guc)
            cur.execute("ROLLBACK TO SAVEPOINT p")


# ── idempotent, first apply ───────────────────────────────────────────────────

def test_rerun_is_idempotent(cur):
    _apply(cur)
    _apply(cur)
    for t in TABLES:
        assert _privileges(cur, t) == PRIVS[t], t
    assert len(_policies(cur, PAGES)) == 3
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '265'")
    assert cur.fetchone()[0] == 1


def test_first_apply_from_the_pre_265_state(cur):
    _to_pre_265(cur)
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert cur.fetchall() == [], "the pre-265 state meets invariant v1 (241)"
    _apply(cur)
    for t in TABLES:
        assert _privileges(cur, t) == PRIVS[t], t
    cur.execute("SELECT description FROM shared.schema_versions WHERE version = '265'")
    assert "AA-859 wave 4" in cur.fetchone()[0]


def test_rerun_puts_back_the_exact_grant_set(cur):
    """Extra privileges (table or column level) on the five tables and the 7 platform relations are revoked by a
    re-run: REVOKE ALL on a table takes its column grants with it."""
    cur.execute(f"GRANT DELETE ON {SUBJECT} TO {TENANT_ROLE}")
    cur.execute(f"GRANT UPDATE (tenant_id) ON {QP} TO {TENANT_ROLE}")
    cur.execute(f"GRANT INSERT ON {PAGES} TO {TENANT_ROLE}")
    cur.execute(f"GRANT INSERT, UPDATE ON acp_contract.route TO {TENANT_ROLE}")
    cur.execute(f"GRANT UPDATE (canonical_place) ON acp_contract.atom_segment TO {TENANT_ROLE}")
    _apply(cur)
    for t in TABLES:
        assert _privileges(cur, t) == PRIVS[t], t
    assert _privileges(cur, "acp_contract.route") == {"SELECT"}
    assert _privileges(cur, "acp_contract.atom_segment") == {"SELECT"}


def test_guard_6_is_skipped_outside_db_migrate(cur):
    notices = cur.connection.notices
    notices.clear()
    _apply(cur)
    assert any("guard 6" in n and "skipped" in n for n in notices), notices


# ── guards ────────────────────────────────────────────────────────────────────

def test_guard_refuses_force(cur):
    cur.execute(f"ALTER TABLE {QPV} FORCE ROW LEVEL SECURITY")
    _refused(cur, "FORCE ROW LEVEL SECURITY")


def test_guard_refuses_an_unknown_policy(cur):
    cur.execute(f"CREATE POLICY zz265_extra ON {SUBJECT} USING (true)")
    _refused(cur, "zz265_extra")


@pytest.mark.parametrize("grantee", ["zz265_login", "PUBLIC"])
def test_guard_refuses_a_grantee_that_would_read_zero_rows(cur, grantee):
    if grantee != "PUBLIC":
        cur.execute(f"CREATE ROLE {grantee} LOGIN")
    cur.execute(f"GRANT SELECT ON {YP} TO {grantee}")
    _refused(cur, "would\\s+read 0 rows")


def test_guard_refuses_a_member_of_the_tenant_role(cur):
    cur.execute("CREATE ROLE zz265_member NOLOGIN")
    cur.execute(f"GRANT {TENANT_ROLE} TO zz265_member")
    _refused(cur, "members of aa_app_user")


def test_guard_refuses_a_view_owned_by_a_filtered_role(cur):
    cur.execute("CREATE ROLE zz265_view_owner NOLOGIN")
    cur.execute("GRANT USAGE, CREATE ON SCHEMA acp_shared TO zz265_view_owner")
    cur.execute(f"GRANT SELECT ON {QP} TO zz265_view_owner")
    cur.execute(f"CREATE VIEW acp_shared.zz265_v AS SELECT tenant_id FROM {QP}")
    cur.execute("ALTER VIEW acp_shared.zz265_v OWNER TO zz265_view_owner")
    _refused(cur, "zz265_v")


def test_guard_refuses_a_non_canonical_page_tenant_id(cur):
    cur.execute(f"INSERT INTO {PAGES} (tenant_id, tour_id, url) VALUES ('AA_INTERNAL', gen_random_uuid(), 'x')")
    _refused(cur, "non-canonical tenant ids")


def test_guard_refuses_a_platform_relation_with_rls(cur):
    cur.execute("ALTER TABLE acp_shared.place_card_alias ENABLE ROW LEVEL SECURITY")
    _refused(cur, "platform relation acp_shared.place_card_alias has ROW LEVEL SECURITY")


def test_guard_refuses_a_platform_relation_with_a_tenant_column(cur):
    cur.execute("ALTER TABLE acp_contract.atom_segment_alias ADD COLUMN owner_scope text")
    _refused(cur, "tenant_id / owner_scope column")


def test_guard_refuses_a_privilege_on_a_relation_without_rls(cur):
    cur.execute(f"GRANT SELECT ON shared.job TO {TENANT_ROLE}")
    _refused(cur, "relations without RLS: shared.job SELECT")


def test_guard_refuses_a_column_grant_on_a_relation_without_rls(cur):
    """Invariant v2 keeps v1's column branch whole: the allowlist covers table-level SELECT only."""
    cur.execute("SELECT attname FROM pg_attribute WHERE attrelid = 'shared.job'::regclass AND attnum = 1")
    col = cur.fetchone()[0]
    cur.execute(f"GRANT UPDATE ({col}) ON shared.job TO {TENANT_ROLE}")
    _refused(cur, f"shared.job UPDATE\\({col}\\)")


def test_guard_refuses_a_grant_on_an_acp_contract_view(cur):
    cur.execute(f"GRANT SELECT ON acp_contract.v_active_tour_atoms TO {TENANT_ROLE}")
    _refused(cur, "v_active_tour_atoms")


# ── review S247 ───────────────────────────────────────────────────────────────

def test_guard_refuses_a_column_only_grantee_that_would_read_zero_rows(cur):
    """A login role holding only a column grant on one of the five would silently read 0 rows once RLS is on."""
    cur.execute("CREATE ROLE zz265_col_login LOGIN")
    cur.execute(f"GRANT SELECT (tenant_id) ON {YP} TO zz265_col_login")
    _refused(cur, "zz265_col_login hold privileges")


@pytest.mark.parametrize("grant", ["GRANT SELECT ON acp_contract.hub TO PUBLIC",
                                   "GRANT UPDATE (route_id) ON acp_contract.route TO PUBLIC"])
def test_guard_refuses_a_public_grant_in_the_newly_reachable_schema(cur, grant):
    """265 gives the tenant role USAGE on acp_contract for the first time: a PUBLIC grant there (table or column, SELECT
    included) on a relation without RLS would reach the tenant pool, and V2 only looks at aa_app_user's own grants."""
    cur.execute(grant)
    _refused(cur, "PUBLIC holds privileges on acp_contract relations without RLS")


def test_guard_refuses_a_security_definer_function_in_acp_contract(cur):
    cur.execute("CREATE FUNCTION acp_contract.zz265_secdef() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'")
    _refused(cur, "SECURITY DEFINER functions in acp_contract: acp_contract.zz265_secdef")


def test_tenant_role_reads_only_card_id_and_status_of_place_card(cur):
    """The one tenant-pool statement (subject_seed._ALIAS_SQL) reads card_id and status; the staff columns stay out."""
    cur.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
    cur.execute("SELECT count(*) FROM (SELECT card_id, status FROM acp_shared.place_card) x")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("SELECT verified_by FROM acp_shared.place_card")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("SELECT * FROM acp_shared.place_card")
