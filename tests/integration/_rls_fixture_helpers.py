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
* `undo_241_for_239_reapply(cur)`: 239's guard 1 refuses RLS on angle_gate_option (it was one of 239's
  REVOKE tables, "with RLS a later wave owns its grants"). A fixture that re-applies 239 on the fully
  migrated CI DB first takes angle_gate_option back to its pre-241 state, then may re-apply 241.
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
    column UPDATE), and grant Dev's legacy-table grants 239's guard 4 counts with. Run inside the
    test's transaction (rolled back with it)."""
    cur.execute("DROP POLICY IF EXISTS tenant_isolation ON acp_shared.angle_gate_option")
    cur.execute("ALTER TABLE acp_shared.angle_gate_option DISABLE ROW LEVEL SECURITY")
    cur.execute("REVOKE UPDATE (chosen) ON acp_shared.angle_gate_option FROM aa_app_user")
    for grant in DEV_LEGACY_GRANTS:
        cur.execute(grant)
