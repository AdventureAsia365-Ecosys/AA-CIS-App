"""AA-859 wave 3 — migration 262: shared.tenant_brand_rules and shared.tenant_seo_config on the 229 RLS template,
the X1 cis_app parity policy, and the grants of the tenant-pool brand identity routes.

conftest applies 262 as the superuser with every other migration (and swallows migration errors, lesson AA-741, so
test_applied_by_conftest checks the result). Each other test works in a transaction that is rolled back, mostly
re-applying the file body. Checked:
  * shape: RLS on without FORCE, exactly {tenant_isolation, <table>_cis_app_all}, the two policy shapes;
    aa_app_user's exact table / column privileges; no TRUNCATE / DELETE;
  * guard 5 runs the same invariant text as 241 guard 4 / NO_PRIVILEGE_WITHOUT_RLS_SQL / probe v3;
  * cis_app (the CIS runtime since X1 S18) still reads every row, without a GUC and under a forged one;
  * idempotent re-run, and a first apply from the pre-262 state (RLS off, no policy, no aa_app_user grant);
    a re-run puts back the exact grant set (an extra privilege is revoked);
  * the guards refuse: FORCE, an unknown policy, a LOGIN role or PUBLIC with a grant, a member of aa_app_user, a
    view over the table owned by a role RLS would filter, an aa_app_user privilege on a relation without RLS.
Guard 6 (cis_app parity by behaviour) runs only the DB Migrate way (current_user cis_owner); here it is skipped
with a NOTICE, which test_guard_6_is_skipped_outside_db_migrate pins. The tenant role's isolation by behaviour is in
test_aa817_rls_tenant_isolation.py (TENANT_TABLES / COLUMN_WRITE_TABLES) and the routes in
test_aa859_brand_identity_pool.py.
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import (  # noqa: E402
    NO_PRIVILEGE_WITHOUT_RLS_SQL, NO_PRIVILEGE_WITHOUT_RLS_SQL_V2, NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body,
    migration_path, revoke_platform_reads,
)

TENANT_ROLE = "aa_app_user"
BRAND = "shared.tenant_brand_rules"
SEO = "shared.tenant_seo_config"
TABLES = (BRAND, SEO)
BRAND_PRIVS = {"SELECT", "UPDATE(is_active)"} | {
    f"INSERT({c})" for c in ("tenant_id", "system_prompt", "style_guide", "forbidden_words", "version", "is_active",
                             "updated_at", "brand_name", "brand_type", "core_idea", "customer_segment",
                             "customer_mindset", "voice_examples", "good_examples", "target_markets", "default_cta")}
SEO_PRIVS = {"SELECT", "INSERT(tenant_id)", "INSERT(target_market)", "UPDATE(target_market)", "UPDATE(updated_at)"}
NULLIF_QUAL = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"


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


def _to_pre_262(c):
    """Dev's state before 262: no RLS, no policy, no aa_app_user privilege (241 revoked its SELECT)."""
    for t in TABLES:
        c.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        c.execute(f"DROP POLICY IF EXISTS {t.split('.')[1]}_cis_app_all ON {t}")
        c.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        c.execute(f"REVOKE ALL ON {t} FROM {TENANT_ROLE}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '262'")


def _apply(c):
    # AA-859 wave 4: guard 5 is invariant v1, which the 265 platform reads (applied later) break by design; a re-apply
    # here starts from the pre-265 state for them.
    revoke_platform_reads(c)
    c.execute(migration_body(262))


def _refused(c, match):
    with pytest.raises(psycopg2.errors.RaiseException, match=match):
        _apply(c)


# ── applied, shape ────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '262'")
        assert c.fetchone(), "conftest swallowed an error in 262 (see its log)"
        for t in TABLES:
            c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (t,))
            assert c.fetchone() == (True, False), t
    finally:
        conn.close()


@pytest.mark.parametrize("table", TABLES)
def test_policy_shape(cur, table):
    pols = _policies(cur, table)
    rel = table.split(".")[1]
    assert set(pols) == {"tenant_isolation", f"{rel}_cis_app_all"}
    assert pols["tenant_isolation"] == (True, "*", ["aa_app_user"], NULLIF_QUAL, NULLIF_QUAL)
    assert pols[f"{rel}_cis_app_all"] == (True, "*", ["cis_app"], "true", "true")


def test_tenant_role_privileges(cur):
    assert _privileges(cur, BRAND) == BRAND_PRIVS
    assert _privileges(cur, SEO) == SEO_PRIVS
    for t in TABLES:
        cur.execute("SELECT has_table_privilege(%s, %s, 'DELETE, TRUNCATE')", (TENANT_ROLE, t))
        assert not cur.fetchone()[0], t


def test_invariant_query_is_the_migration_guard():
    """Guard 5 carries the AA-859 invariant byte for byte (241 guard 4, the helper, the probe v3 verdict)."""
    sql = open(migration_path(262)).read()
    assert NO_PRIVILEGE_WITHOUT_RLS_SQL in sql


def test_committed_db_meets_the_invariant(cur):
    # the committed DB is past 272: the invariant there is v3 (the platform read allowlist + v_tour_liveness)
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3)
    assert cur.fetchall() == []


# ── cis_app parity ────────────────────────────────────────────────────────────

def test_cis_app_reads_every_row_without_and_under_a_forged_guc(cur):
    """The CIS runtime (api main pool, worker) is not the owner: without its parity policy it would read 0 rows the
    moment RLS is on. The CI replay skips 244, so its SELECT is granted here, inside the rolled-back transaction."""
    tids = []
    for _ in range(2):
        tid = str(uuid.uuid4())
        tids.append(tid)
        cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
                    (tid, f"AA-859 262 {tid[:8]}", f"aa859-262-{tid[:8]}"))
        cur.execute(f"INSERT INTO {BRAND} (tenant_id, brand_name, version) VALUES (%s, 'default', 1)", (tid,))
        cur.execute(f"INSERT INTO {SEO} (tenant_id, target_market) VALUES (%s, %s::jsonb)", (tid, json.dumps({})))
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    for t in TABLES:
        cur.execute(f"GRANT SELECT ON {t} TO cis_app")
        cur.execute(f"SELECT count(*) FROM {t}")
        total = cur.fetchone()[0]
        assert total >= 2
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
    assert _privileges(cur, BRAND) == BRAND_PRIVS and _privileges(cur, SEO) == SEO_PRIVS
    for t in TABLES:
        assert len(_policies(cur, t)) == 2
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '262'")
    assert cur.fetchone()[0] == 1


def test_first_apply_from_the_pre_262_state(cur):
    _to_pre_262(cur)
    revoke_platform_reads(cur)
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL)
    assert cur.fetchall() == [], "pre-262 state meets the invariant (241)"
    _apply(cur)
    assert _privileges(cur, BRAND) == BRAND_PRIVS and _privileges(cur, SEO) == SEO_PRIVS
    cur.execute("SELECT description FROM shared.schema_versions WHERE version = '262'")
    assert "AA-859 wave 3" in cur.fetchone()[0]


def test_rerun_puts_back_the_exact_grant_set(cur):
    cur.execute(f"GRANT DELETE ON {BRAND} TO {TENANT_ROLE}")
    cur.execute(f"GRANT UPDATE (system_prompt) ON {BRAND} TO {TENANT_ROLE}")
    cur.execute(f"GRANT UPDATE (seo_provider) ON {SEO} TO {TENANT_ROLE}")
    _apply(cur)
    assert _privileges(cur, BRAND) == BRAND_PRIVS and _privileges(cur, SEO) == SEO_PRIVS


def test_guard_6_is_skipped_outside_db_migrate(cur):
    notices = cur.connection.notices
    notices.clear()
    _apply(cur)
    assert any("guard 6" in n and "skipped" in n for n in notices), notices


# ── guards ────────────────────────────────────────────────────────────────────

def test_guard_refuses_force(cur):
    cur.execute(f"ALTER TABLE {SEO} FORCE ROW LEVEL SECURITY")
    _refused(cur, "FORCE ROW LEVEL SECURITY")


def test_guard_refuses_an_unknown_policy(cur):
    cur.execute(f"CREATE POLICY zz262_extra ON {BRAND} USING (true)")
    _refused(cur, "zz262_extra")


@pytest.mark.parametrize("grantee", ["zz262_login", "PUBLIC"])
def test_guard_refuses_a_grantee_that_would_read_zero_rows(cur, grantee):
    if grantee != "PUBLIC":
        cur.execute(f"CREATE ROLE {grantee} LOGIN")
    cur.execute(f"GRANT SELECT ON {SEO} TO {grantee}")
    _refused(cur, "would\\s+read 0 rows")


def test_guard_accepts_a_nologin_role_without_members(cur):
    """CI's legacy app_user (005) holds grants on these tables but can open no session."""
    cur.execute("CREATE ROLE zz262_nologin NOLOGIN")
    cur.execute(f"GRANT SELECT ON {BRAND} TO zz262_nologin")
    _apply(cur)


def test_guard_refuses_a_member_of_the_tenant_role(cur):
    cur.execute("CREATE ROLE zz262_member NOLOGIN")
    cur.execute(f"GRANT {TENANT_ROLE} TO zz262_member")
    _refused(cur, "members of aa_app_user")


def test_guard_refuses_a_view_owned_by_a_filtered_role(cur):
    cur.execute("CREATE ROLE zz262_view_owner NOLOGIN")
    cur.execute("GRANT USAGE, CREATE ON SCHEMA shared TO zz262_view_owner")
    cur.execute(f"GRANT SELECT ON {BRAND} TO zz262_view_owner")
    cur.execute(f"CREATE VIEW shared.zz262_v AS SELECT tenant_id FROM {BRAND}")
    cur.execute("ALTER VIEW shared.zz262_v OWNER TO zz262_view_owner")
    _refused(cur, "zz262_v")


def test_guard_refuses_a_privilege_on_a_relation_without_rls(cur):
    cur.execute(f"GRANT SELECT ON shared.job TO {TENANT_ROLE}")
    _refused(cur, "relations without RLS: shared.job SELECT")
