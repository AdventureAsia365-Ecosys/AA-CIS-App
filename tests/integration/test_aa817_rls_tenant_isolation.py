"""AA-817 — tenant isolation by RLS for the role the tenant pool connects as (aa_app_user).

The guard the issue asks for: a non-owner, non-BYPASSRLS role with a forged app.tenant_id and a query
that FORGETS its `WHERE tenant_id = ...` reads 0 rows of another tenant. Covers the acp_shared tables
behind the tenant routes on the tenant pool (AA-544 stages 1/5, AA-815, AA-817 latest-piece):
angle_gate_request (113), content_piece (115), publish_log (116), facts (145, platform rows shared by
design), gold_aa_internal.tenant_tour_versions (the stage-2 T2 table), all on the policy template of
migration 229, and shared.tenant_integrations (the AA-815 disconnect route, same template, migration 237).
The CI DB does not create tenant_tour_versions (migration 001 is skipped), so each test creates
conftest's stand-in and re-applies 229 and 237 in its own rolled-back transaction.

Write side: a WHERE-less UPDATE touches only the own tenant's rows, a row can be neither handed to nor
inserted for another tenant (or under ''), and platform facts can be read but never written or
re-scoped (229's WITH CHECK; before 229 this was a strict xfail). tenant_integrations is granted only
SELECT and DELETE (237): a WHERE-less DELETE removes only the own tenant's row (0 under a forged uuid or
''), and INSERT/UPDATE are refused by the missing grant.

The legacy FORCE-RLS tables (004/006/008 policies TO public; migration 239): published_tours, raw_tours
and seo_context show the tenant its own rows plus the AA master rows (aa_app_user_aa_master_read, the
T2 read path) and never another tenant's; generated_content, quality_scores, content_exports,
webhook_deliveries and pipeline_runs show only the own tenant's rows; '' gives no rows instead of
22P02 on all eight. Master rows stay read-only even with a write grant. acp_shared.acp_output_rules
(RLS on, no policy) is deny-all for the tenant role. ALL_RLS_TABLES lists the 15 RLS tables of Dev
(AA-817 plan §0); each is checked here. Tables without RLS lose the tenant role's writes (239).

The CI DB skips 007b/008, so aa_app_user has none of Dev's SELECT grants on the five non-master legacy
tables (nor SELECT/INSERT/UPDATE on pipeline_runs): the fixture grants them in its transaction, as on
Dev, before re-applying 239 (whose guard then runs its behavioural checks with them).

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
MIGRATION_237 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                             "237_tenant_integrations_rls.sql")
MIGRATION_239 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                             "239_tenant_master_read_nullif.sql")

TENANT_ROLE = "aa_app_user"
# table -> True when rows with tenant_id NULL (platform scope) are visible to every tenant by design
TENANT_TABLES = {
    "acp_shared.angle_gate_request": False,
    "acp_shared.content_piece": False,
    "acp_shared.publish_log": False,
    "acp_shared.facts": True,
    "gold_aa_internal.tenant_tour_versions": False,
    "shared.tenant_integrations": False,
}
# tables the tenant role may read and DELETE but not INSERT/UPDATE (237: the disconnect route only)
DELETE_ONLY_TABLES = {"shared.tenant_integrations"}

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
# legacy FORCE-RLS tables (004/006/008, tenant_isolation TO public, NULLIF since 225/239) -> True when the
# tenant role also reads the AA master rows (239: aa_app_user_aa_master_read)
LEGACY_TABLES = {
    "gold_aa_internal.published_tours": True,
    "silver_aa_internal.raw_tours": True,
    "silver_aa_internal.seo_context": True,
    "silver_aa_internal.generated_content": False,
    "silver_aa_internal.quality_scores": False,
    "gold_aa_internal.content_exports": False,
    "gold_aa_internal.webhook_deliveries": False,
    "shared.pipeline_runs": False,
}
OUTPUT_RULES = "acp_shared.acp_output_rules"   # RLS on, no policy: deny-all for every non-owner (229)
ALL_RLS_TABLES = set(TENANT_TABLES) | set(LEGACY_TABLES) | {OUTPUT_RULES}
# tables WITHOUT RLS on which the tenant role may no longer write (239, plan critique C1)
NO_RLS_NO_WRITE_TABLES = ("acp_shared.angle_gate_option", "acp_shared.tenant_atom_state", "acp_shared.year_plan")
# Dev's tenant-role grants on the legacy tables that the CI DB lacks (007b/008 are skipped in CI)
DEV_LEGACY_GRANTS = (
    "GRANT USAGE ON SCHEMA gold_aa_internal, silver_aa_internal, shared TO aa_app_user",
    "GRANT SELECT ON silver_aa_internal.generated_content, silver_aa_internal.quality_scores, "
    "gold_aa_internal.content_exports, gold_aa_internal.webhook_deliveries TO aa_app_user",
    "GRANT SELECT, INSERT, UPDATE ON shared.pipeline_runs TO aa_app_user",
)
# stand-in for the live-only acp_output_rules (no CIS migration creates it; Dev: RLS on, no policy)
OUTPUT_RULES_STANDIN_DDL = """
    CREATE TABLE IF NOT EXISTS acp_shared.acp_output_rules (
        rule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(50),
        rule_type VARCHAR(20) NOT NULL DEFAULT 'block',
        pattern   VARCHAR(500) NOT NULL DEFAULT 'x'
    );
    ALTER TABLE acp_shared.acp_output_rules ENABLE ROW LEVEL SECURITY;
"""


def _migration_body(path: str) -> str:
    with open(path) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(TTV_STANDIN_DDL)
        c.execute(OUTPUT_RULES_STANDIN_DDL)
        c.execute(_migration_body(MIGRATION_229))
        c.execute(_migration_body(MIGRATION_237))
        for grant in DEV_LEGACY_GRANTS:
            c.execute(grant)
        c.execute(_migration_body(MIGRATION_239))
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
    c.execute("INSERT INTO shared.tenant_integrations (tenant_id, integration_type, config, secret_key) "
              "VALUES (%s, 'wordpress', '{}'::jsonb, %s)", (tid, f"acp/cms/{tid}"))


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
WRITE_TABLES = sorted(set(TENANT_TABLES) - DELETE_ONLY_TABLES)
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


# shared.tenant_integrations (237): the tenant pool may read and DELETE (DELETE /v1/integrations/{type},
# AA-815), nothing else. The route filters by tenant_id; these checks drop the filter on purpose.

@pytest.mark.parametrize("table", sorted(DELETE_ONLY_TABLES))
def test_forged_tenant_deletes_no_foreign_rows(cur, two_tenants, table):
    a, b = two_tenants
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute(f"DELETE FROM {table}")                   # no WHERE on purpose
            assert c.rowcount == 0, (table, guc)
    with _as_tenant(cur, a) as c:
        c.execute(f"DELETE FROM {table}")                       # no WHERE: only the own row goes
        assert c.rowcount == 1, table
        c.execute("RESET ROLE")                                 # read back as the seeding superuser
        c.execute(f"SELECT DISTINCT tenant_id::text FROM {table} WHERE tenant_id::text = ANY(%s)", ([a, b],))
        assert {r[0] for r in c.fetchall()} == {b}, "the other tenant's row must survive"


@pytest.mark.parametrize("table", sorted(DELETE_ONLY_TABLES))
def test_tenant_role_cannot_insert_or_update_delete_only_tables(cur, two_tenants, table):
    """Connect / test-connection stay on the main pool: no INSERT or UPDATE grant (237), and no
    TRUNCATE, which RLS does not filter."""
    a, _ = two_tenants
    for sql, params in ((f"UPDATE {table} SET updated_at = now()", ()),
                        (f"INSERT INTO {table} (tenant_id, integration_type) VALUES (%s, 'facebook')", (a,)),
                        (f"TRUNCATE {table}", ())):
        with _as_tenant(cur, a) as c:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
                c.execute(sql, params)


# ── Legacy FORCE-RLS tables (004/006/008 policies, migration 239) ────────────────────────────────────

def _seed_legacy(c, tid):
    """One row per legacy table for this tenant, linked the way the pipeline links them."""
    c.execute("INSERT INTO silver_aa_internal.raw_tours (tenant_id, batch_id, src_name) "
              "VALUES (%s, gen_random_uuid(), 'aa817') RETURNING tour_id", (tid,))
    tour = c.fetchone()[0]
    c.execute("INSERT INTO silver_aa_internal.seo_context (tenant_id, tour_id) VALUES (%s, %s)", (tid, tour))
    c.execute("INSERT INTO silver_aa_internal.generated_content (tenant_id, tour_id) VALUES (%s, %s) "
              "RETURNING id", (tid, tour))
    gc = c.fetchone()[0]
    c.execute("INSERT INTO silver_aa_internal.quality_scores (tenant_id, tour_id, generated_content_id, "
              "score_overall) VALUES (%s, %s, %s, 9)", (tid, tour, gc))
    c.execute("INSERT INTO gold_aa_internal.published_tours (tenant_id, tour_id, generated_content_id, aa_name) "
              "VALUES (%s, %s, %s, 'aa817')", (tid, tour, gc))
    c.execute("INSERT INTO gold_aa_internal.content_exports (tenant_id) VALUES (%s)", (tid,))
    c.execute("INSERT INTO gold_aa_internal.webhook_deliveries (tenant_id, tour_id, webhook_url) "
              "VALUES (%s, %s, 'https://example.invalid/aa817')", (tid, tour))
    c.execute("INSERT INTO shared.pipeline_runs (tenant_id, batch_id, status) "
              "VALUES (%s, gen_random_uuid(), 'completed')", (tid,))


@pytest.fixture
def legacy_rows(cur, two_tenants):
    """Rows of tenants a, b and of the AA master tenant in every legacy table."""
    a, b = two_tenants
    for tid in (a, b, MASTER_TENANT):
        _seed_legacy(cur, tid)
    return a, b


def _ours(tenants, a, b):
    """The visible tenant set restricted to this test's tenants (the CI DB may hold other rows)."""
    return tenants & {a, b, MASTER_TENANT}


def test_every_rls_table_is_covered(cur):
    """The 15 RLS tables of Dev (AA-817 plan §0) all have RLS here, and none is owned by the tenant role."""
    assert len(ALL_RLS_TABLES) == 15
    for table in sorted(ALL_RLS_TABLES):
        cur.execute("SELECT c.relrowsecurity, pg_has_role(%s, c.relowner, 'USAGE') FROM pg_class c "
                    "WHERE c.oid = %s::regclass", (TENANT_ROLE, table))
        rls, owns = cur.fetchone()
        assert rls and not owns, table


@pytest.mark.parametrize("table", sorted(LEGACY_TABLES))
def test_legacy_table_forged_tenant_reads_no_foreign_rows(cur, legacy_rows, table):
    a, b = legacy_rows
    master = {MASTER_TENANT} if LEGACY_TABLES[table] else set()

    assert _ours(_visible_tenants(cur, table, a), a, b) == {a} | master
    assert _ours(_visible_tenants(cur, table, b), a, b) == {b} | master
    assert _ours(_visible_tenants(cur, table, str(uuid.uuid4())), a, b) == master
    # '' (a pooled connection after any set_config(..., true)): no rows of any tenant, and no 22P02
    assert _ours(_visible_tenants(cur, table, ""), a, b) == master
    # the master GUC sees the master rows (tenant_isolation TO public), never a or b
    assert _ours(_visible_tenants(cur, table, MASTER_TENANT), a, b) == {MASTER_TENANT}


@pytest.mark.parametrize("table", sorted(t for t, master in LEGACY_TABLES.items() if master))
def test_master_rows_stay_read_only_for_the_tenant_role(cur, legacy_rows, table):
    """aa_app_user_aa_master_read is FOR SELECT: even with a write grant (none on Dev), a WHERE-less
    UPDATE / DELETE under tenant a reaches only a's row, never a master row or b's row."""
    a, b = legacy_rows
    cur.execute(f"GRANT UPDATE, DELETE ON {table} TO {TENANT_ROLE}")
    with _as_tenant(cur, a) as c:
        c.execute(f"UPDATE {table} SET tenant_id = tenant_id")        # no WHERE on purpose
        assert c.rowcount == 1, table
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute(f"UPDATE {table} SET tenant_id = tenant_id")    # master rows are visible, not writable
            assert c.rowcount == 0, (table, guc)
    with _as_tenant(cur, a) as c:
        _rls_violation(c, f"UPDATE {table} SET tenant_id = %s", (MASTER_TENANT,))   # hand own row to master


def test_pipeline_runs_forged_tenant_writes_no_foreign_rows(cur, legacy_rows):
    """pipeline_runs is the one legacy table the tenant role may write on Dev (SELECT/INSERT/UPDATE)."""
    a, b = legacy_rows
    table = "shared.pipeline_runs"
    for guc, expected in ((a, 1), (str(uuid.uuid4()), 0), ("", 0)):
        with _as_tenant(cur, guc) as c:
            c.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id::text = ANY(%s)",
                      ([a, b, MASTER_TENANT],))                       # the filter names every tenant
            assert c.rowcount == expected, guc
    insert = f"INSERT INTO {table} (tenant_id, batch_id, status) VALUES (%s, gen_random_uuid(), 'completed')"
    with _as_tenant(cur, a) as c:
        _rls_violation(c, insert, (b,))
    with _as_tenant(cur, "") as c:
        _rls_violation(c, insert, (a,))
    with _as_tenant(cur, a) as c:
        _rls_violation(c, f"UPDATE {table} SET tenant_id = %s WHERE tenant_id = %s", (b, a))


def test_output_rules_is_deny_all_for_the_tenant_role(cur):
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'acp_shared' AND tablename = 'acp_output_rules'")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE')",
                (TENANT_ROLE, OUTPUT_RULES))
    assert not cur.fetchone()[0]
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
                c.execute(f"SELECT count(*) FROM {OUTPUT_RULES}")


@pytest.mark.parametrize("table", NO_RLS_NO_WRITE_TABLES)
def test_tenant_role_cannot_write_tables_without_rls(cur, table):
    """No RLS there, so a tenant-pool write could reach any tenant's rows: 239 revokes them."""
    cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT, UPDATE, DELETE, TRUNCATE')", (TENANT_ROLE, table))
    assert not cur.fetchone()[0], table
    with _as_tenant(cur, str(uuid.uuid4())) as c:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
            c.execute(f"UPDATE {table} SET tenant_id = tenant_id" if table != "acp_shared.angle_gate_option"
                      else f"UPDATE {table} SET chosen = chosen")


def test_tenant_pool_still_reads_angle_gate_option(cur):
    """The publish-log list and the latest-piece read join angle_gate_option on the tenant pool."""
    cur.execute("SELECT has_table_privilege(%s, 'acp_shared.angle_gate_option', 'SELECT')", (TENANT_ROLE,))
    assert cur.fetchone()[0]
