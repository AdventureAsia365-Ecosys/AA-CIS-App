"""Shared helpers for the tenant-role RLS tests (AA-817 / AA-859).

* `migration_body(n)`: a migration file without its own BEGIN/COMMIT, so a test can run it inside a
  transaction it rolls back.
* `DEV_LEGACY_GRANTS`: Dev's tenant-role grants on the legacy FORCE-RLS tables that the CI DB lacks
  (007b/008 are skipped in CI).
* `NO_PRIVILEGE_WITHOUT_RLS_SQL`: the AA-859 invariant (plan D1). Rows returned = violations: a privilege
  of aa_app_user (table or column level) on a relation without RLS, except the cross-app catalog
  contract (SELECT on the `catalog` relations owned by catalog_owner, 225 / 228). The same text is guard
  4 of migration 241 and the verdict of the AA-859 probe v3; test_aa859_migration_241 checks that the
  migration still carries it.
* `NO_PRIVILEGE_WITHOUT_RLS_SQL_V2` (AA-859 wave 4, migrations 265 / 266): the invariant once the platform
  read allowlist exists. V1 plus two exceptions: table-level SELECT on the 6 relations of
  PLATFORM_READ_TABLE_LEVEL, and column-level SELECT on PLATFORM_READ_COLUMNS (acp_shared.place_card: card_id
  and status only, the one tenant-pool statement's columns; its staff `verified_by`, `cost_usd`, `naming_note`
  and `facts` stay unreadable, review S247). Any other grant on them is still a violation. V1 stays byte for
  byte as it is: 241 and 262 carry it (their tests check that), and a DB that has 265 applied fails V1 by those
  8 rows by design.
* `undo_241_for_239_reapply(cur)`: 239's guard 1 refuses RLS on angle_gate_option and year_plan (both were 239
  REVOKE tables, "with RLS a later wave owns its grants"). A fixture that re-applies 239 on the fully migrated CI
  DB first takes angle_gate_option back to its pre-241 state and year_plan to its pre-265 state
  (undo_265_year_plan), then may re-apply 241, 262, 265 and 266.
* `undo_265_year_plan(cur)`: year_plan without RLS / its two policies / aa_app_user's 265 grants, the state the
  241 tests use it in (a relation without RLS the loop revokes on).
* `revoke_platform_reads(cur)`: aa_app_user's 265 SELECT (table and column level) on the 7 platform relations
  revoked, the pre-265 state that 241 guard 4 / 262 guard 5 (V1) expect when a test re-applies them on the fully
  migrated DB.
* `restore_266_catalog_owner_read(cur)`: the catalog tests drop and re-create catalog_owner (DROP OWNED also drops
  a policy whose only role it is); this puts 266's keep-alive policy on tour_atoms back.
"""
import glob
import os

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")

# Dev's tenant-role grants on the legacy tables that the CI DB lacks (007b/008 are skipped in CI)
DEV_LEGACY_GRANTS = (
    "GRANT USAGE ON SCHEMA gold_aa_internal, silver_aa_internal, shared TO aa_app_user",
    "GRANT SELECT ON silver_aa_internal.generated_content, silver_aa_internal.quality_scores, "
    "gold_aa_internal.content_exports, gold_aa_internal.webhook_deliveries TO aa_app_user",
    "GRANT SELECT, INSERT, UPDATE ON shared.pipeline_runs TO aa_app_user",
)

NO_PRIVILEGE_WITHOUT_RLS_SQL = """\
SELECT c.oid::regclass::text AS rel, a.privilege_type, NULL::text AS column_name
  FROM pg_class c
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
 CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND NOT c.relrowsecurity
   AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
   AND ns.nspname NOT LIKE 'pg_toast%'
   AND a.grantee = 'aa_app_user'::regrole
   AND NOT (ns.nspname = 'catalog'
            AND c.relowner IS NOT DISTINCT FROM to_regrole('catalog_owner')
            AND a.privilege_type = 'SELECT')
UNION ALL
SELECT c.oid::regclass::text, a.privilege_type, att.attname::text
  FROM pg_class c
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
  JOIN pg_attribute att ON att.attrelid = c.oid AND att.attacl IS NOT NULL AND NOT att.attisdropped
 CROSS JOIN LATERAL aclexplode(att.attacl) a
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND NOT c.relrowsecurity
   AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
   AND ns.nspname NOT LIKE 'pg_toast%'
   AND a.grantee = 'aa_app_user'::regrole
ORDER BY 1, 3, 2"""

# AA-859 wave 4 (265): relations with no tenant column the tenant role reads (SELECT only): table level on
# PLATFORM_READ_TABLE_LEVEL, column level on PLATFORM_READ_COLUMNS.
PLATFORM_READ_ALLOWLIST = (
    "acp_contract.route", "acp_contract.atom_segment", "acp_contract.atom_segment_member",
    "acp_contract.atom_segment_alias", "acp_shared.place_card", "acp_shared.place_card_alias",
    "acp_shared.atom_place_card",
)
PLATFORM_READ_COLUMNS = {"acp_shared.place_card": ("card_id", "status")}
PLATFORM_READ_TABLE_LEVEL = tuple(r for r in PLATFORM_READ_ALLOWLIST if r not in PLATFORM_READ_COLUMNS)


def platform_read_privileges(rel) -> set:
    """aa_app_user's exact privileges on a platform relation after 265, in the 'PRIV' / 'PRIV(column)' form."""
    if rel in PLATFORM_READ_COLUMNS:
        return {f"SELECT({col})" for col in PLATFORM_READ_COLUMNS[rel]}
    return {"SELECT"}


def platform_read_v1_rows() -> set:
    """The (rel, privilege, column) rows V1 reports on a DB with 265 applied, by design."""
    return ({(rel, "SELECT", None) for rel in PLATFORM_READ_TABLE_LEVEL}
            | {(rel, "SELECT", col) for rel, cols in PLATFORM_READ_COLUMNS.items() for col in cols})

# The same text is guard 5 of migrations 265 and 266.
NO_PRIVILEGE_WITHOUT_RLS_SQL_V2 = """\
SELECT c.oid::regclass::text AS rel, a.privilege_type, NULL::text AS column_name
  FROM pg_class c
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
 CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND NOT c.relrowsecurity
   AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
   AND ns.nspname NOT LIKE 'pg_toast%'
   AND a.grantee = 'aa_app_user'::regrole
   AND NOT (ns.nspname = 'catalog'
            AND c.relowner IS NOT DISTINCT FROM to_regrole('catalog_owner')
            AND a.privilege_type = 'SELECT')
   AND NOT (ns.nspname || '.' || c.relname = ANY (ARRAY['acp_contract.route', 'acp_contract.atom_segment',
                                                        'acp_contract.atom_segment_member',
                                                        'acp_contract.atom_segment_alias',
                                                        'acp_shared.place_card_alias', 'acp_shared.atom_place_card'])
            AND a.privilege_type = 'SELECT')
UNION ALL
SELECT c.oid::regclass::text, a.privilege_type, att.attname::text
  FROM pg_class c
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
  JOIN pg_attribute att ON att.attrelid = c.oid AND att.attacl IS NOT NULL AND NOT att.attisdropped
 CROSS JOIN LATERAL aclexplode(att.attacl) a
 WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND NOT c.relrowsecurity
   AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
   AND ns.nspname NOT LIKE 'pg_toast%'
   AND a.grantee = 'aa_app_user'::regrole
   AND NOT (ns.nspname || '.' || c.relname = 'acp_shared.place_card'
            AND att.attname IN ('card_id', 'status')
            AND a.privilege_type = 'SELECT')
ORDER BY 1, 3, 2"""


def migration_path(n) -> str:
    """The one file `api/migrations/<n>_*.sql`."""
    matches = glob.glob(os.path.join(MIGRATIONS_DIR, f"{n}_*.sql"))
    assert len(matches) == 1, f"expected one migration {n}_*.sql, found {matches}"
    return matches[0]


def migration_body(n) -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    with open(migration_path(n)) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


def undo_241_for_239_reapply(cur):
    """Take angle_gate_option back to the pre-241 state 239's guard 1 expects (no RLS, no policy, no
    column UPDATE), year_plan to its pre-265 state (undo_265_year_plan), and grant Dev's legacy-table
    grants 239's guard 4 counts with. Run inside the test's transaction (rolled back with it)."""
    cur.execute("DROP POLICY IF EXISTS tenant_isolation ON acp_shared.angle_gate_option")
    cur.execute("ALTER TABLE acp_shared.angle_gate_option DISABLE ROW LEVEL SECURITY")
    cur.execute("REVOKE UPDATE (chosen) ON acp_shared.angle_gate_option FROM aa_app_user")
    undo_265_year_plan(cur)
    for grant in DEV_LEGACY_GRANTS:
        cur.execute(grant)


def undo_265_year_plan(cur):
    """acp_shared.year_plan as before 265: no RLS, neither of its policies, no aa_app_user privilege."""
    cur.execute("REVOKE ALL ON acp_shared.year_plan FROM aa_app_user")
    cur.execute("DROP POLICY IF EXISTS tenant_isolation ON acp_shared.year_plan")
    cur.execute("DROP POLICY IF EXISTS year_plan_cis_app_all ON acp_shared.year_plan")
    cur.execute("ALTER TABLE acp_shared.year_plan DISABLE ROW LEVEL SECURITY")


def revoke_platform_reads(cur):
    """aa_app_user's 265 SELECT on the platform read allowlist revoked, table and column level (REVOKE ALL on a
    table also drops its column grants): the pre-265 state."""
    cur.execute(f"REVOKE ALL ON {', '.join(PLATFORM_READ_ALLOWLIST)} FROM aa_app_user")


def restore_266_catalog_owner_read(cur):
    """266's keep-alive policy on tour_atoms, when tour_atoms has RLS and the policy is missing (DROP OWNED BY
    catalog_owner removes a policy whose only role it is). A no-op before 266 or when it is in place."""
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'acp_contract.tour_atoms'::regclass")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT 1 FROM pg_policy WHERE polrelid = 'acp_contract.tour_atoms'::regclass "
                "AND polname = 'catalog_owner_read'")
    if cur.fetchone():
        return
    cur.execute("CREATE POLICY catalog_owner_read ON acp_contract.tour_atoms AS PERMISSIVE FOR SELECT "
                "TO catalog_owner USING (true)")
