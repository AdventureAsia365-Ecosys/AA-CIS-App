"""AA-859 wave 4 — migration 266: acp_contract.tour_atoms under RLS (aa_app_user: platform atoms + its own, read only;
cis_app parity; catalog_owner keep-alive for the catalog views).

conftest applies 266 as the superuser with every other migration (and swallows migration errors, so
test_applied_by_conftest checks the result). Each other test works in a rolled-back transaction. Checked:
  * shape: RLS on without FORCE, exactly the four policies and their shapes; aa_app_user holds exactly SELECT;
  * guard 5 runs invariant v2 (NO_PRIVILEGE_WITHOUT_RLS_SQL_V2);
  * keep-alive: catalog_owner reads every atom and the same catalog.v_tour_place rows as the owner sees; cis_app reads
    every atom without a GUC and under a forged one;
  * idempotent re-run, a first apply from the pre-266 state;
  * the guards refuse: FORCE, an unknown policy, a tripplanner grant ("apply CIS 254 first"), another LOGIN grantee,
    a view owned by a filtered role, a non-canonical owner_scope, D9 > 0 (a live tenant atom on a live master tour).
Guard 6 runs only the DB Migrate way; here it is skipped with a NOTICE. The tenant role by behaviour: test_aa817
(test_tour_atoms_*) and test_aa859_wave4_pool_routes.py.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import (  # noqa: E402
    NO_PRIVILEGE_WITHOUT_RLS_SQL_V2, migration_body, migration_path, revoke_tour_liveness_read,
)

TENANT_ROLE = "aa_app_user"
ATOMS = "acp_contract.tour_atoms"
MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
OWN_QUAL = "(owner_scope = NULLIF(current_setting('app.tenant_id'::text, true), ''::text))"


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


def _policies(c):
    c.execute("""SELECT polname, polpermissive, polcmd, polroles::regrole[]::text[],
                        pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid)
                   FROM pg_policy WHERE polrelid = %s::regclass ORDER BY polname""", (ATOMS,))
    return {r[0]: r[1:] for r in c.fetchall()}


def _privileges(c):
    c.execute("""
        SELECT a.privilege_type FROM pg_class cl, aclexplode(cl.relacl) a
         WHERE cl.oid = %s::regclass AND a.grantee = %s::regrole
        UNION ALL
        SELECT a.privilege_type || '(' || att.attname || ')'
          FROM pg_attribute att, aclexplode(att.attacl) a
         WHERE att.attrelid = %s::regclass AND att.attacl IS NOT NULL AND NOT att.attisdropped
           AND a.grantee = %s::regrole""", (ATOMS, TENANT_ROLE, ATOMS, TENANT_ROLE))
    return {r[0] for r in c.fetchall()}


def _apply(c):
    # AA-986 L2: guard 5 is invariant v2, which 272's v_tour_liveness read (applied later) breaks by design.
    revoke_tour_liveness_read(c)
    c.execute(migration_body(266))


def _refused(c, match):
    with pytest.raises(psycopg2.errors.RaiseException, match=match):
        _apply(c)


def _tour(c, tenant=MASTER_TENANT, live=True):
    """A raw tour and (live=True) its active, not deleted published_tours row."""
    c.execute("INSERT INTO silver_aa_internal.raw_tours (tenant_id, batch_id, src_name) "
              "VALUES (%s, gen_random_uuid(), 'aa859-266') RETURNING tour_id", (tenant,))
    tour = c.fetchone()[0]
    if live:
        c.execute("INSERT INTO silver_aa_internal.generated_content (tenant_id, tour_id) VALUES (%s, %s) RETURNING id",
                  (tenant, tour))
        gc = c.fetchone()[0]
        c.execute("INSERT INTO gold_aa_internal.published_tours (tenant_id, tour_id, generated_content_id, aa_name, "
                  "master_status) VALUES (%s, %s, %s, 'aa859-266', 'active')", (tenant, tour, gc))
    return tour


def _atom(c, tour, scope="platform"):
    atom = f"aa859-266-{uuid.uuid4().hex[:10]}"
    c.execute(f"INSERT INTO {ATOMS} (atom_id, tour_id, owner_scope, text) VALUES (%s, %s, %s, 'x')",
              (atom, tour, scope))
    return atom


# ── applied, shape ────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '266'")
        assert c.fetchone(), "conftest swallowed an error in 266 (see its log; CIS 254 must be applied first)"
        c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (ATOMS,))
        assert c.fetchone() == (True, False)
    finally:
        conn.close()


def test_policy_shape(cur):
    pols = _policies(cur)
    assert pols == {
        "atoms_platform_read": (True, "r", ["aa_app_user"], "(owner_scope = 'platform'::text)", None),
        "catalog_owner_read": (True, "r", ["catalog_owner"], "true", None),
        "tenant_isolation": (True, "r", ["aa_app_user"], OWN_QUAL, None),
        "tour_atoms_cis_app_all": (True, "*", ["cis_app"], "true", "true"),
    }


def test_tenant_role_privileges(cur):
    assert _privileges(cur) == {"SELECT"}


def test_invariant_v2_is_the_migration_guard():
    assert NO_PRIVILEGE_WITHOUT_RLS_SQL_V2 in open(migration_path(266)).read()


# ── keep-alive ────────────────────────────────────────────────────────────────

def test_catalog_owner_and_cis_app_read_every_atom(cur):
    tour = _tour(cur)
    tenant = str(uuid.uuid4())
    _atom(cur, tour)
    _atom(cur, tour, tenant)
    cur.execute(f"SELECT count(*) FROM {ATOMS}")
    total = cur.fetchone()[0]
    cur.execute("GRANT USAGE ON SCHEMA acp_contract TO cis_app")
    cur.execute(f"GRANT SELECT ON {ATOMS} TO cis_app")
    for role, gucs in (("catalog_owner", ("",)), ("cis_app", ("", str(uuid.uuid4())))):
        for guc in gucs:
            cur.execute("SAVEPOINT p")
            cur.execute(f"SET LOCAL ROLE {role}")
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
            cur.execute(f"SELECT count(*) FROM {ATOMS}")
            assert cur.fetchone()[0] == total, (role, guc)
            cur.execute("ROLLBACK TO SAVEPOINT p")


def test_catalog_views_see_the_atoms_as_before(cur):
    """catalog.v_tour_place reads tour_atoms as catalog_owner: the rows it shows are the ones it shows with RLS off."""
    cur.execute("SELECT to_regclass('catalog.v_tour_place') IS NOT NULL")
    if not cur.fetchone()[0]:
        pytest.skip("catalog.v_tour_place not in this DB")

    def _count():
        cur.execute("SAVEPOINT p")
        cur.execute("SET LOCAL ROLE catalog_owner")
        cur.execute("SELECT set_config('app.tenant_id', '', true)")
        cur.execute("SELECT count(*) FROM catalog.v_tour_place")
        n = cur.fetchone()[0]
        cur.execute("ROLLBACK TO SAVEPOINT p")
        return n
    with_rls = _count()
    cur.execute(f"ALTER TABLE {ATOMS} DISABLE ROW LEVEL SECURITY")
    assert _count() == with_rls


# ── idempotent, first apply ───────────────────────────────────────────────────

def test_rerun_is_idempotent(cur):
    _apply(cur)
    _apply(cur)
    assert len(_policies(cur)) == 4 and _privileges(cur) == {"SELECT"}
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '266'")
    assert cur.fetchone()[0] == 1


def test_first_apply_from_the_pre_266_state(cur):
    cur.execute(f"REVOKE ALL ON {ATOMS} FROM {TENANT_ROLE}")
    for pol in ("atoms_platform_read", "tenant_isolation", "tour_atoms_cis_app_all", "catalog_owner_read"):
        cur.execute(f"DROP POLICY IF EXISTS {pol} ON {ATOMS}")
    cur.execute(f"ALTER TABLE {ATOMS} DISABLE ROW LEVEL SECURITY")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '266'")
    _apply(cur)
    assert len(_policies(cur)) == 4 and _privileges(cur) == {"SELECT"}


def test_guard_6_is_skipped_outside_db_migrate(cur):
    notices = cur.connection.notices
    notices.clear()
    _apply(cur)
    assert any("guard 6" in n and "skipped" in n for n in notices), notices


# ── guards ────────────────────────────────────────────────────────────────────

def test_guard_refuses_force(cur):
    cur.execute(f"ALTER TABLE {ATOMS} FORCE ROW LEVEL SECURITY")
    _refused(cur, "FORCE ROW LEVEL SECURITY")


def test_guard_refuses_an_unknown_policy(cur):
    cur.execute(f"CREATE POLICY zz266_extra ON {ATOMS} FOR SELECT TO aa_app_user USING (true)")
    _refused(cur, "zz266_extra")


def test_guard_refuses_a_tripplanner_grant(cur):
    cur.execute(f"GRANT SELECT (atom_id, place) ON {ATOMS} TO tripplanner")
    _refused(cur, "apply CIS 254")


def test_guard_refuses_another_login_grantee(cur):
    cur.execute("CREATE ROLE zz266_login LOGIN")
    cur.execute(f"GRANT SELECT ON {ATOMS} TO zz266_login")
    _refused(cur, "would read 0 rows")


def test_guard_refuses_a_view_owned_by_a_filtered_role(cur):
    cur.execute("CREATE ROLE zz266_view_owner NOLOGIN")
    cur.execute("GRANT USAGE, CREATE ON SCHEMA acp_contract TO zz266_view_owner")
    cur.execute(f"GRANT SELECT ON {ATOMS} TO zz266_view_owner")
    cur.execute(f"CREATE VIEW acp_contract.zz266_v AS SELECT atom_id FROM {ATOMS}")
    cur.execute("ALTER VIEW acp_contract.zz266_v OWNER TO zz266_view_owner")
    _refused(cur, "zz266_v")


def test_guard_refuses_a_non_canonical_owner_scope(cur):
    _atom(cur, _tour(cur, live=False), "Tenant-Upper")
    _refused(cur, "Tenant-Upper")


def test_guard_refuses_d9(cur):
    """A live tenant-owned atom on a live master tour reaches every catalog consumer already: Q4, not this file."""
    _atom(cur, _tour(cur), str(uuid.uuid4()))
    _refused(cur, "D9")


def test_guard_accepts_a_tenant_atom_off_the_live_catalog(cur):
    """D9 counts live atoms of live master tours only: a deleted one, or one on an unpublished tour, is fine."""
    tenant = str(uuid.uuid4())
    atom = _atom(cur, _tour(cur), tenant)
    cur.execute(f"UPDATE {ATOMS} SET deleted = true WHERE atom_id = %s", (atom,))
    _atom(cur, _tour(cur, live=False), tenant)
    _apply(cur)
