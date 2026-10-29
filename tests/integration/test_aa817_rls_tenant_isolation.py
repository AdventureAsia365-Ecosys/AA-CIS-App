"""AA-817 — tenant isolation by RLS for the role the tenant pool connects as (aa_app_user).

The guard the issue asks for: a non-owner, non-BYPASSRLS role with a forged app.tenant_id and a query
that FORGETS its `WHERE tenant_id = ...` reads 0 rows of another tenant. Covers the acp_shared tables
behind the tenant routes on the tenant pool (AA-544 stages 1/5, AA-815, AA-817 latest-piece):
angle_gate_request (113), content_piece (115), publish_log (116), facts (145, platform rows shared by
design), gold_aa_internal.tenant_tour_versions (the stage-2 T2 table), all on the policy template of
migration 229, and shared.tenant_integrations (the AA-815 disconnect route, same template, migration 237),
shared.tenant_brand_rules and shared.tenant_seo_config (the AA-859 wave-3 brand identity routes, same template,
migration 262), acp_shared.subject / quarter_plan / year_plan and acp_deliver.tenant_tour_pages (the AA-859 wave-4
Slate / planning / review routes, migration 265; tenant_tour_pages compares text and shows the AA master's pages).
acp_shared.quarter_plan_version goes through its parent quarter_plan (CHILD_RLS_TABLES), and acp_contract.tour_atoms
(266) shows every platform atom plus the tenant's own, read only (ATOMS). shared.writing_signal (277, AA-916, FORCE
RLS) shows the own tenant rows only, never a platform (A1) row; the tenant role may only read and insert there
(INSERT_ONLY_TABLES).
acp_shared.angle_gate_option has no tenant column: migration 241 puts it under RLS through its parent
angle_gate_request (CHILD_RLS_TABLES, checked by the key expression, not by tenant_id).
The CI DB does not create tenant_tour_versions (migration 001 is skipped), so each test creates
conftest's stand-in and re-applies 229, 237, 239, 241, 262, 265 and 266 in its own rolled-back transaction (239 after
undo_241_for_239_reapply: its guard refuses RLS on angle_gate_option and year_plan).

Write side: a WHERE-less UPDATE touches only the own tenant's rows, a row can be neither handed to nor
inserted for another tenant (or under ''), and platform facts can be read but never written or
re-scoped (229's WITH CHECK; before 229 this was a strict xfail). tenant_integrations is granted SELECT
and DELETE (237) and the columns the connect UPSERT and the test-connection UPDATE write (241): a
WHERE-less DELETE / UPDATE reaches only the own tenant's row (0 under a forged uuid or ''), a row cannot
be inserted for another tenant, and tenant_id / integration_type are not updatable. angle_gate_option is
granted SELECT and UPDATE (chosen) only (241). tenant_brand_rules is granted SELECT, INSERT on the brand POST's
columns and UPDATE (is_active); tenant_seo_config SELECT, INSERT (tenant_id, target_market) and UPDATE
(target_market, updated_at) (262): the real brand POST statements work for the own tenant, a WHERE-less
deactivate reaches only its rows, nothing can be written for another tenant, tenant_id is not updatable, no
DELETE / TRUNCATE.

The legacy FORCE-RLS tables (004/006/008 policies TO public; migration 239): published_tours, raw_tours
and seo_context show the tenant its own rows plus the AA master rows (aa_app_user_aa_master_read, the
T2 read path) and never another tenant's; generated_content, quality_scores, content_exports,
webhook_deliveries and pipeline_runs show only the own tenant's rows; '' gives no rows instead of
22P02 on all eight. Master rows stay read-only even with a write grant. acp_shared.acp_output_rules
(RLS on, no policy) is deny-all for the tenant role. ALL_RLS_TABLES lists the 24 RLS tables of Dev
after 266 (AA-817 plan §0 + angle_gate_option + the two wave-3 tables + the five 265 tables + tour_atoms); each is
checked here. On a relation without RLS the tenant role holds no privilege at all, the catalog contract and the
table-level SELECT on the 265 platform read allowlist excepted (241, AA-859 plan D1, wave 4:
NO_PRIVILEGE_WITHOUT_RLS_SQL_V2).

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
from _rls_fixture_helpers import (  # noqa: E402
    NO_PRIVILEGE_WITHOUT_RLS_SQL_V2, NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, PLATFORM_READ_ALLOWLIST, PLATFORM_READ_COLUMNS,
    migration_body,
    undo_241_for_239_reapply,
)

TENANT_ROLE = "aa_app_user"
# table -> True when rows with tenant_id NULL (platform scope) are visible to every tenant by design
TENANT_TABLES = {
    "acp_shared.angle_gate_request": False,
    "acp_shared.content_piece": False,
    "acp_shared.publish_log": False,
    "acp_shared.facts": True,
    "gold_aa_internal.tenant_tour_versions": False,
    "shared.tenant_integrations": False,
    "shared.tenant_brand_rules": False,     # 262 (AA-859 wave 3)
    "shared.tenant_seo_config": False,      # 262 (AA-859 wave 3)
    "acp_shared.subject": False,            # 265 (AA-859 wave 4)
    "acp_shared.quarter_plan": False,       # 265
    "acp_shared.year_plan": False,          # 265
    "acp_deliver.tenant_tour_pages": False,  # 265, text tenant_id (+ master read, own test below)
    "shared.notifications": False,          # 274 (AA-986 L3): tenant-facing rows only (restrictive audience policy)
    "shared.writing_signal": False,         # 277 (AA-916): tenant rows only, platform rows hidden; FORCE RLS
}
# tables the tenant role writes through column grants only (241: connect / test-connection columns; 237:
# DELETE; 262: the brand POST's columns): the generic `SET tenant_id = tenant_id` write tests do not apply, the
# dedicated ones below do
# 265: the pick / cut / trip-reallocation columns; tenant_tour_pages is read only for the tenant role
COLUMN_WRITE_TABLES = {"shared.tenant_integrations", "shared.tenant_brand_rules", "shared.tenant_seo_config",
                       "acp_shared.subject", "acp_shared.quarter_plan", "acp_shared.year_plan",
                       "acp_deliver.tenant_tour_pages", "shared.notifications"}
# tables the tenant role may DELETE from (237: the disconnect route; 302, AA-814: DELETE /v1/facts/{id} of an own
# tenant fact); the others have no DELETE grant
DELETE_TABLES = {"shared.tenant_integrations", "acp_shared.facts"}
# tables the tenant role may only read and INSERT into (277: the T9 edit signal); no UPDATE grant, so the generic
# `SET tenant_id = tenant_id` write test does not apply; the insert tests (_INSERT_FOR) and the platform-row test do
INSERT_ONLY_TABLES = {"shared.writing_signal"}
# RLS tables without a tenant column: table -> (primary key, the row's tenant through its parent). The
# key is evaluated by the (unfiltered) superuser, never by the tenant role, whose RLS on the parent
# would turn a leaked row's tenant into NULL.
CHILD_RLS_TABLES = {
    "acp_shared.angle_gate_option": (
        "option_id",
        "(SELECT r.tenant_id FROM acp_shared.angle_gate_request r WHERE r.request_id = t.request_id)"),
    "acp_shared.quarter_plan_version": (    # 265 (AA-859 wave 4)
        "version_id",
        "(SELECT qp.tenant_id FROM acp_shared.quarter_plan qp WHERE qp.plan_id = t.plan_id)"),
}

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
ATOMS = "acp_contract.tour_atoms"              # 266: platform atoms + own, FOR SELECT only
ALL_RLS_TABLES = set(TENANT_TABLES) | set(CHILD_RLS_TABLES) | set(LEGACY_TABLES) | {OUTPUT_RULES, ATOMS}
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


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(TTV_STANDIN_DDL)
        c.execute(OUTPUT_RULES_STANDIN_DDL)
        c.execute(migration_body(229))
        c.execute(migration_body(237))
        undo_241_for_239_reapply(c)            # 239's guard refuses RLS on angle_gate_option; grants Dev's
        c.execute(migration_body(239))         # legacy-table grants
        c.execute(migration_body(241))
        c.execute(migration_body(262))         # idempotent re-apply (its guards run on the replayed state)
        c.execute(migration_body(265))         # 241's loop revoked the platform reads, 239's REVOKE year_plan's
        c.execute(migration_body(266))         # INSERT: 265 puts them back; 266 re-applies on the replayed state
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-817 {tid[:8]}", f"aa817-{tid[:8]}"))
    return tid


_SIGNAL_INSERT = ("INSERT INTO shared.writing_signal (writer, channel, scope, tenant_id, field, before_text, "
                  "after_text, before_sha256, after_sha256, action, actor_type, source_table, source_id) "
                  "VALUES ('t9_blog', 'blog', %s, %s, 'content_text', 'b', 'a', repeat('a', 64), repeat('b', 64), "
                  "'tenant_edit', 'tenant', 'content_piece', 'aa817-test')")


def _seed(c, tid):
    """One row per tenant table (and one angle_gate_option under the request) for this tenant."""
    c.execute("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status) "
              "VALUES (%s, 'aa817-atom', 'approved') RETURNING request_id", (tid,))
    req = c.fetchone()[0]
    c.execute("INSERT INTO acp_shared.angle_gate_option (request_id, idx, name, why_it_works, formula_fit, "
              "best_final_style) VALUES (%s, 0, 'aa817', 'x', 'x', 'x')", (req,))
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
    c.execute("INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name, version, is_active, system_prompt) "
              "VALUES (%s, 'default', 1, true, 'aa817 brand')", (tid,))
    c.execute("INSERT INTO shared.tenant_seo_config (tenant_id, target_market) VALUES (%s, %s::jsonb)",
              (tid, json.dumps({"countries": ["UK"]})))
    # 265 (AA-859 wave 4): a Subject on a platform Segment, a year / quarter plan with one version, a trip page
    segment = f"aa817-seg-{uuid.uuid4().hex[:10]}"
    c.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
              "VALUES (%s, 'Hanoi', 'walk')", (segment,))
    c.execute("INSERT INTO acp_shared.subject (tenant_id, segment_id, channel, state, cleared_bar_reason) "
              "VALUES (%s, %s, 'blog', 'proposed', '{}'::jsonb)", (tid, segment))
    c.execute("INSERT INTO acp_shared.year_plan (tenant_id, year) VALUES (%s, 2031) RETURNING year_plan_id", (tid,))
    year_plan = c.fetchone()[0]
    c.execute("INSERT INTO acp_shared.quarter_plan (tenant_id, year, quarter, year_plan_id) VALUES (%s, 2031, 1, %s) "
              "RETURNING plan_id", (tid, year_plan))
    plan = c.fetchone()[0]
    c.execute("INSERT INTO acp_shared.quarter_plan_version (plan_id, version_no, payload, source, approval_status) "
              "VALUES (%s, 1, '{}'::jsonb, 'standard', 'pending')", (plan,))
    c.execute("INSERT INTO acp_deliver.tenant_tour_pages (tenant_id, tour_id, url) VALUES (%s, %s, %s)",
              (tid, str(uuid.uuid4()), f"https://example.invalid/{tid[:8]}"))
    # 274 (AA-986 L3): a tenant-facing notification (the admin-only rows of a tenant are hidden: own test in
    # test_aa986_migration_274.py)
    c.execute("INSERT INTO shared.notifications (tenant_id, event_type, entity_type, entity_id, target_roles) "
              "VALUES (%s, 'tour.discontinued', 'tour', 'x', ARRAY['tenant'])", (tid,))
    # 277 (AA-916): a tenant edit signal of a T9 piece
    c.execute(_SIGNAL_INSERT, ("tenant", tid))


@pytest.fixture
def two_tenants(cur):
    a, b = _tenant(cur), _tenant(cur)
    _seed(cur, a)
    _seed(cur, b)
    cur.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                "VALUES (%s, 'platform', NULL, 't', 'b', 'aa817-test')", (f"fact_{uuid.uuid4().hex[:10]}",))
    # 277: an A1 (platform) signal, which no tenant may read
    cur.execute(_SIGNAL_INSERT.replace("'t9_blog', 'blog'", "'a1', 'master'"), ("platform", None))
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
    for table in sorted(set(TENANT_TABLES) | set(CHILD_RLS_TABLES)):
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
WRITE_TABLES = sorted(set(TENANT_TABLES) - COLUMN_WRITE_TABLES - INSERT_ONLY_TABLES)
_INSERT_FOR = {
    "acp_shared.angle_gate_request": ("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status) "
                                      "VALUES (%s, 'aa817-atom', 'approved')"),
    "acp_shared.facts": ("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                         "VALUES ('fact_' || substr(md5(random()::text), 1, 10), 'tenant', %s, 't', 'b', 'p')"),
    "gold_aa_internal.tenant_tour_versions": ("INSERT INTO gold_aa_internal.tenant_tour_versions "
                                              "(tenant_id, edit_source) VALUES (%s, 'ai_generated')"),
    "shared.writing_signal": _SIGNAL_INSERT.replace("VALUES ('t9_blog', 'blog', %s, %s,",
                                                    "VALUES ('t9_blog', 'blog', 'tenant', %s,"),
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


@pytest.mark.parametrize("table", sorted(INSERT_ONLY_TABLES))
def test_insert_only_tables_refuse_update_and_delete(cur, two_tenants, table):
    """277: the tenant role reads and inserts writing signals, nothing else (no grant: 42501, not an RLS refusal)."""
    a, _ = two_tenants
    for sql in (f"UPDATE {table} SET tenant_id = tenant_id", f"DELETE FROM {table}"):
        with _as_tenant(cur, a) as c:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
                c.execute(sql)


def test_tenant_role_cannot_write_platform_signals(cur, two_tenants):
    """277: a platform (A1) signal cannot be written by the tenant pool, with or without a tenant id."""
    a, _ = two_tenants
    with _as_tenant(cur, a) as c:
        _rls_violation(c, _SIGNAL_INSERT, ("platform", None))
    with _as_tenant(cur, a) as c:              # a platform row naming the own tenant: the policy refuses it first
        _rls_violation(c, _SIGNAL_INSERT, ("platform", a))


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
    with _as_tenant(cur, a) as c:              # 302 grants DELETE: still never a platform fact
        c.execute("DELETE FROM acp_shared.facts WHERE scope = 'platform'")
        assert c.rowcount == 0, "tenant role deleted platform facts"


# shared.tenant_integrations (237 + 241): the tenant pool may read, DELETE (DELETE /v1/integrations/{type},
# AA-815) and write the columns of the connect UPSERT and the test-connection UPDATE (W9, AA-859). The
# routes filter by tenant_id; these checks drop the filter on purpose.

# api/routers/v1_integrations.py save_wordpress / save_facebook (connect) and test_* (verify), verbatim
# but for the placeholders
_CONNECT_UPSERT = """
    INSERT INTO shared.tenant_integrations
        (tenant_id, integration_type, config, secret_key, connected_at)
    VALUES (%s::uuid, 'wordpress', %s::jsonb, %s, now())
    ON CONFLICT (tenant_id, integration_type) DO UPDATE SET
        config = EXCLUDED.config,
        secret_key = EXCLUDED.secret_key,
        updated_at = now()
    RETURNING config, connected_at, last_verified_at, last_verify_error
"""
_VERIFY_OK = """
    UPDATE shared.tenant_integrations
    SET last_verified_at = now(), last_verify_error = NULL, updated_at = now()
    WHERE tenant_id = %s::uuid AND integration_type = 'wordpress'
    RETURNING config, connected_at, last_verified_at, last_verify_error
"""
_VERIFY_FAILED = """
    UPDATE shared.tenant_integrations
    SET last_verify_error = %s, updated_at = now()
    WHERE tenant_id = %s::uuid AND integration_type = 'wordpress'
    RETURNING config, connected_at, last_verified_at, last_verify_error
"""


@pytest.mark.parametrize("table", sorted(DELETE_TABLES))
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


def test_tenant_integrations_connect_and_verify_run_on_the_tenant_role(cur, two_tenants):
    """The real connect UPSERT (new row and conflict path) and both verify UPDATEs work for the own
    tenant under the 241 column grants (a missing column grant would be 42501 -> 500 after the flip)."""
    a, b = two_tenants
    with _as_tenant(cur, a) as c:
        c.execute(_CONNECT_UPSERT.replace("'wordpress'", "'facebook'"), (a, "{}", f"acp/social/{a}"))
        assert c.rowcount == 1                                  # new row
        c.execute(_CONNECT_UPSERT, (a, '{"site_url": "https://x.invalid"}', f"acp/cms/{a}"))
        assert c.rowcount == 1                                  # ON CONFLICT DO UPDATE of the seeded row
        c.execute(_VERIFY_OK, (a,))
        assert c.rowcount == 1
        c.execute(_VERIFY_FAILED, ("401 from site", a))
        assert c.rowcount == 1
        c.execute("RESET ROLE")
        c.execute("SELECT count(*) FROM shared.tenant_integrations WHERE tenant_id = %s", (b,))
        assert c.fetchone()[0] == 1, "the other tenant's row must be untouched"


def test_tenant_integrations_writes_stay_in_the_own_tenant(cur, two_tenants):
    a, b = two_tenants
    table = "shared.tenant_integrations"
    for guc, expected in ((a, 1), (str(uuid.uuid4()), 0), ("", 0)):
        with _as_tenant(cur, guc) as c:
            c.execute(f"UPDATE {table} SET updated_at = now()")         # no WHERE on purpose
            assert c.rowcount == expected, guc
    with _as_tenant(cur, a) as c:                                       # a row for another tenant (WITH CHECK)
        _rls_violation(c, _CONNECT_UPSERT.replace("'wordpress'", "'facebook'"), (b, "{}", "x"))
    with _as_tenant(cur, "") as c:                                      # '' is no tenant
        _rls_violation(c, _CONNECT_UPSERT.replace("'wordpress'", "'facebook'"), (a, "{}", "x"))
    # No case for "b's existing row through ON CONFLICT": Postgres checks the INSERT WITH CHECK before
    # conflict arbitration, so it is the first case again, and tenant_id is part of the conflict key and
    # not updatable, so the DO UPDATE path only ever reaches the own tenant's row.


def test_tenant_integrations_columns_outside_the_grant_are_refused(cur, two_tenants):
    """tenant_id and integration_type are not updatable (a row cannot be moved to another tenant or
    channel), no other column is insertable, and no TRUNCATE, which RLS does not filter."""
    a, b = two_tenants
    table = "shared.tenant_integrations"
    for sql, params in ((f"UPDATE {table} SET integration_type = 'facebook'", ()),
                        (f"UPDATE {table} SET tenant_id = %s", (b,)),
                        (f"UPDATE {table} SET created_at = now()", ()),
                        (f"INSERT INTO {table} (id, tenant_id, integration_type, config) "
                         "VALUES (gen_random_uuid(), %s, 'facebook', '{}'::jsonb)", (a,)),
                        (f"TRUNCATE {table}", ())):
        with _as_tenant(cur, a) as c:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
                c.execute(sql, params)


# ── shared.tenant_brand_rules / tenant_seo_config (262): the brand identity POST on the tenant pool ─────
# api/routers/admin_pipeline.py _save_brand_rows, verbatim but for the placeholders (and the
# WHERE-less variants that drop the route's own tenant filter on purpose).
_BRAND_DEACTIVATE_ALL = "UPDATE shared.tenant_brand_rules SET is_active = false"
_BRAND_INSERT = """
    INSERT INTO shared.tenant_brand_rules
        (tenant_id, system_prompt, style_guide, forbidden_words, version, is_active,
         updated_at, brand_name, brand_type, core_idea, customer_segment,
         customer_mindset, voice_examples, good_examples, target_markets, default_cta)
    VALUES (%s, 'p', 's', '[]'::jsonb, %s, true, NOW(), 'default', '', '', '', '', '[]'::jsonb, '', %s, '')
"""
_MARKETS_UPSERT = """
    INSERT INTO shared.tenant_seo_config (tenant_id, target_market)
    VALUES (%s::uuid, %s::jsonb)
    ON CONFLICT (tenant_id) DO UPDATE
        SET target_market = EXCLUDED.target_market, updated_at = now()
"""


def test_brand_save_statements_run_on_the_tenant_role(cur, two_tenants):
    """The whole POST (lock, MAX + 1, deactivate, insert, markets upsert) works for the own tenant under the 262
    grants (a missing column grant would be 42501 -> 500 after the flip) and leaves the other tenant alone."""
    a, b = two_tenants
    with _as_tenant(cur, a) as c:
        c.execute("SELECT pg_advisory_xact_lock(859, hashtext(%s || ':brand_identity'))", (a,))
        c.execute("SELECT COALESCE(MAX(version), 0) FROM shared.tenant_brand_rules WHERE tenant_id = %s", (a,))
        current = c.fetchone()[0]
        assert current == 1
        c.execute(_BRAND_DEACTIVATE_ALL)                     # no WHERE on purpose: own rows only
        assert c.rowcount == 1
        c.execute(_BRAND_INSERT, (a, current + 1, ["UK"]))
        c.execute(_MARKETS_UPSERT, (a, json.dumps({"countries": ["AU"]})))   # conflict path (seeded row)
        assert c.rowcount == 1
        c.execute("SELECT version, is_active FROM shared.tenant_brand_rules ORDER BY version")
        assert c.fetchall() == [(1, False), (2, True)]
        c.execute("RESET ROLE")
        c.execute("SELECT count(*) FROM shared.tenant_brand_rules WHERE tenant_id = %s AND is_active", (b,))
        assert c.fetchone()[0] == 1, "the other tenant's active brand row is untouched"
        c.execute("SELECT target_market->'countries' FROM shared.tenant_seo_config WHERE tenant_id = %s", (b,))
        assert c.fetchone()[0] == ["UK"], "the other tenant's markets are untouched"


def test_brand_writes_stay_in_the_own_tenant(cur, two_tenants):
    a, b = two_tenants
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute(_BRAND_DEACTIVATE_ALL)
            assert c.rowcount == 0, guc
            c.execute("UPDATE shared.tenant_seo_config SET updated_at = now()")
            assert c.rowcount == 0, guc
    with _as_tenant(cur, a) as c:                            # a brand row for another tenant (WITH CHECK)
        _rls_violation(c, _BRAND_INSERT, (b, 9, ["UK"]))
    with _as_tenant(cur, "") as c:                           # '' is no tenant
        _rls_violation(c, _BRAND_INSERT, (a, 9, ["UK"]))
    with _as_tenant(cur, a) as c:                            # markets for another tenant
        _rls_violation(c, _MARKETS_UPSERT, (b, "{}"))


@pytest.mark.parametrize("sql,params", [
    ("UPDATE shared.tenant_brand_rules SET tenant_id = %s", "b"),
    ("UPDATE shared.tenant_brand_rules SET system_prompt = 'x'", None),
    ("INSERT INTO shared.tenant_brand_rules (id, tenant_id, brand_name) VALUES (gen_random_uuid(), %s, 'x')", "a"),
    ("DELETE FROM shared.tenant_brand_rules", None),
    ("TRUNCATE shared.tenant_brand_rules", None),
    ("UPDATE shared.tenant_seo_config SET tenant_id = %s", "b"),
    ("UPDATE shared.tenant_seo_config SET seo_provider = seo_provider", None),
    ("UPDATE shared.tenant_seo_config SET custom_keywords = '[]'::jsonb", None),
    ("DELETE FROM shared.tenant_seo_config", None),
    ("TRUNCATE shared.tenant_seo_config", None),
])
def test_brand_columns_outside_the_grant_are_refused(cur, two_tenants, sql, params):
    """tenant_id is never updatable (a row cannot be handed to another tenant), the admin-only columns
    (custom_validators, seo_provider, custom_keywords, overrides, ...) are not writable, no DELETE, no TRUNCATE
    (which RLS does not filter)."""
    a, b = two_tenants
    args = () if params is None else ({"a": a, "b": b}[params],)
    with _as_tenant(cur, a) as c:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
            c.execute(sql, args)


# ── RLS tables without a tenant column (241: angle_gate_option through its parent request) ────────────

def _visible_child_tenants(c, table, guc):
    """As the tenant role with app.tenant_id = guc, the rows a WHERE-less query returns, mapped to their
    tenant by the superuser (see CHILD_RLS_TABLES)."""
    pk, key = CHILD_RLS_TABLES[table]
    with _as_tenant(c, guc):
        c.execute(f"SELECT {pk}::text FROM {table}")            # no WHERE on purpose
        ids = [r[0] for r in c.fetchall()]
    c.execute(f"SELECT DISTINCT {key}::text FROM {table} t WHERE t.{pk}::text = ANY(%s)", (ids,))
    return {r[0] for r in c.fetchall()}


@pytest.mark.parametrize("table", sorted(CHILD_RLS_TABLES))
def test_child_forged_tenant_reads_no_foreign_rows(cur, two_tenants, table):
    a, b = two_tenants
    assert _visible_child_tenants(cur, table, a) == {a}
    assert _visible_child_tenants(cur, table, b) == {b}
    assert _visible_child_tenants(cur, table, str(uuid.uuid4())) == set()
    assert _visible_child_tenants(cur, table, "") == set()


@pytest.mark.parametrize("table", sorted(CHILD_RLS_TABLES))
def test_owner_still_sees_every_tenant_of_child_tables(cur, two_tenants, table):
    """Sanity check of the fixture: the rows exist, so the empty sets above come from RLS."""
    a, b = two_tenants
    _, key = CHILD_RLS_TABLES[table]
    cur.execute(f"SELECT count(DISTINCT {key}) FROM {table} t WHERE {key}::text = ANY(%s)", ([a, b],))
    assert cur.fetchone()[0] == 2, table


def test_angle_gate_option_writes(cur, two_tenants):
    """241: SELECT + UPDATE (chosen) only. A WHERE-less UPDATE reaches only the own tenant's options;
    INSERT (the T8 worker's, on the main pool) and every other column are refused by the grant."""
    a, b = two_tenants
    table = "acp_shared.angle_gate_option"
    for guc, expected in ((a, 1), (b, 1), (str(uuid.uuid4()), 0), ("", 0)):
        with _as_tenant(cur, guc) as c:
            c.execute(f"UPDATE {table} SET chosen = chosen")    # no WHERE on purpose
            assert c.rowcount == expected, guc
    cur.execute("SELECT request_id FROM acp_shared.angle_gate_request WHERE tenant_id = %s", (a,))
    own_request = cur.fetchone()[0]
    cur.execute("SELECT request_id FROM acp_shared.angle_gate_request WHERE tenant_id = %s", (b,))
    other_request = cur.fetchone()[0]
    for sql, params in ((f"INSERT INTO {table} (request_id, idx, name, why_it_works, formula_fit, best_final_style) "
                         "VALUES (%s, 1, 'n', 'x', 'x', 'x')", (own_request,)),
                        (f"UPDATE {table} SET request_id = %s", (other_request,)),
                        (f"UPDATE {table} SET name = name", ()),
                        (f"DELETE FROM {table}", ()),
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
    """The 26 RLS tables of Dev after 277 (AA-817 plan §0 + angle_gate_option + tenant_brand_rules /
    tenant_seo_config + the five 265 tables + tour_atoms + notifications + writing_signal) all have RLS here, and none
    is owned by the tenant role."""
    assert len(ALL_RLS_TABLES) == 26
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


def test_tenant_role_has_no_privilege_on_any_relation_without_rls(cur):
    """AA-859 plan D1 (241): without RLS a grant to the tenant role is a grant on every tenant's rows, so
    it holds none (table or column level), the catalog contract (SELECT on catalog_owner's catalog.*
    views, 225 / 228) and (wave 4, 265) the table-level SELECT on the platform read allowlist excepted. Also holds
    after test_aa679 / test_aa712_228 re-apply 225 / 228 with commit: they grant only that contract."""
    cur.execute(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3)   # AA-986 L2: v2 + 272's v_tour_liveness read
    assert cur.fetchall() == []
    for rel in PLATFORM_READ_ALLOWLIST:        # the allowlist is used, and nothing beyond SELECT
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT'), "
                    "has_table_privilege(%s, %s, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'), "
                    "has_any_column_privilege(%s, %s, 'INSERT, UPDATE, REFERENCES')",
                    (TENANT_ROLE, rel, TENANT_ROLE, rel, TENANT_ROLE, rel))
        # place_card: column-level SELECT (card_id, status) only, not the staff columns (review S247)
        assert cur.fetchone() == (rel not in PLATFORM_READ_COLUMNS, False, False), rel
    for rel, cols in PLATFORM_READ_COLUMNS.items():
        cur.execute("SELECT att.attname FROM pg_attribute att WHERE att.attrelid = %s::regclass AND att.attnum > 0 "
                    "AND NOT att.attisdropped AND has_column_privilege(%s, att.attrelid, att.attnum, 'SELECT')",
                    (rel, TENANT_ROLE))
        assert sorted(r[0] for r in cur.fetchall()) == sorted(cols), rel


def test_tenant_pool_still_reads_angle_gate_option(cur):
    """The publish-log list and the latest-piece read join angle_gate_option on the tenant pool."""
    cur.execute("SELECT has_table_privilege(%s, 'acp_shared.angle_gate_option', 'SELECT')", (TENANT_ROLE,))
    assert cur.fetchone()[0]


# ── 265 (AA-859 wave 4): the Slate / planning writes on the tenant role ─────────────────────────────────
# The statements of services/acp_shared/slate.py (pick / cut) and services/acp_planning/quarter.py (save / approve),
# verbatim but for the placeholders, plus WHERE-less variants that drop the route's own tenant filter on purpose.

def _own(c, table, col, tid):
    c.execute(f"SELECT {col} FROM {table} WHERE tenant_id = %s", (tid,))
    return c.fetchone()[0]


def test_subject_pick_and_cut_statements_run_on_the_tenant_role(cur, two_tenants):
    from services.acp_shared import slate
    a, b = two_tenants
    sid = _own(cur, "acp_shared.subject", "subject_id", a)
    with _as_tenant(cur, a) as c:
        c.execute(slate._SUBJECT_LOCK_SQL.replace("$1::uuid", "%s::uuid").replace("$2::uuid", "%s::uuid"), (sid, a))
        assert c.fetchone()[2] == "proposed"
        c.execute(slate._SUBJECT_PICKED_SQL.replace("$1::uuid", "%s::uuid").replace("$2::uuid", "%s::uuid"), (sid, a))
        assert c.rowcount == 1
    for guc in (b, str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute("UPDATE acp_shared.subject SET state = 'cut'")        # no WHERE on purpose
            assert c.rowcount == (1 if guc == b else 0), guc
            c.execute("SELECT subject_id FROM acp_shared.subject WHERE subject_id = %s FOR UPDATE", (sid,))
            assert c.fetchall() == [], "another tenant can neither see nor lock a's Subject"


def test_planning_save_and_approve_statements_run_on_the_tenant_role(cur, two_tenants):
    from services.acp_planning import quarter
    a, b = two_tenants

    def _run(c, sql, *args):   # asyncpg $n placeholders -> psycopg2 named ones, so their order does not matter
        for i in range(9, 0, -1):
            sql = sql.replace(f"${i}", f"%(p{i})s")
        c.execute(sql, {f"p{i}": v for i, v in enumerate(args, 1)})
    with _as_tenant(cur, a) as c:
        _run(c, quarter._YEAR_PLAN_UPSERT_SQL, a, 2031)                     # conflict path (seeded row)
        _run(c, quarter._YEAR_PLAN_ID_SQL, a, 2031)
        year_plan = c.fetchone()[0]
        _run(c, quarter._QUARTER_PLAN_UPSERT_SQL, a, 2031, 1, year_plan)
        _run(c, quarter._QUARTER_PLAN_UPSERT_SQL, a, 2031, 2, year_plan)    # new row
        _run(c, quarter._QUARTER_PLAN_ID_SQL, a, 2031, 1)
        plan = c.fetchone()[0]
        _run(c, quarter._NEXT_VERSION_NO_SQL, plan)
        assert c.fetchone()[0] == 2
        _run(c, quarter._VERSION_INSERT_SQL, plan, 2, "{}", "override")
        version = c.fetchone()[0]
        _run(c, quarter._VERSION_LOCK_SQL, version)
        assert c.fetchone()[1] == "pending"
        _run(c, quarter._VERSION_APPROVE_SQL, version, f"tenant:{a}")
        assert c.rowcount == 1
        _run(c, quarter._CURRENT_VERSION_SQL, plan, version)
        assert c.rowcount == 1
        _run(c, quarter._APPROVED_PLAN_SQL, a, 2031, 1)
        assert c.fetchone() is not None
        c.execute("UPDATE acp_shared.quarter_plan_version SET approval_status = approval_status")   # no WHERE
        assert c.rowcount == 2, "only a's two versions"
    with _as_tenant(cur, b) as c:                                          # b sees none of a's plan rows
        c.execute("SELECT count(*) FROM acp_shared.quarter_plan_version v JOIN acp_shared.quarter_plan qp "
                  "USING (plan_id) WHERE qp.tenant_id = %s", (a,))
        assert c.fetchone()[0] == 0
    plan_a = _own(cur, "acp_shared.quarter_plan", "plan_id", a)
    with _as_tenant(cur, b) as c:                                          # a version under a's plan (WITH CHECK)
        _rls_violation(c, "INSERT INTO acp_shared.quarter_plan_version (plan_id, version_no, payload, source, "
                          "approval_status) VALUES (%s, 9, '{}'::jsonb, 'standard', 'pending')", (plan_a,))
    with _as_tenant(cur, a) as c:                                          # a plan for another tenant
        _rls_violation(c, "INSERT INTO acp_shared.quarter_plan (tenant_id, year, quarter) VALUES (%s, 2032, 1)", (b,))
    with _as_tenant(cur, "") as c:
        _rls_violation(c, "INSERT INTO acp_shared.year_plan (tenant_id, year) VALUES (%s, 2033)", (a,))


@pytest.mark.parametrize("sql,params", [
    ("UPDATE acp_shared.subject SET tenant_id = %s", "b"),
    ("UPDATE acp_shared.subject SET score = 1", None),
    ("INSERT INTO acp_shared.subject (tenant_id, route_id, channel, cleared_bar_reason) VALUES (%s, 'r', 'blog', "
     "'{}'::jsonb)", "a"),
    ("DELETE FROM acp_shared.subject", None),
    ("TRUNCATE acp_shared.subject", None),
    ("UPDATE acp_shared.year_plan SET tenant_id = %s", "b"),
    ("DELETE FROM acp_shared.year_plan", None),
    ("UPDATE acp_shared.quarter_plan SET tenant_id = %s", "b"),
    ("UPDATE acp_shared.quarter_plan SET year = 2040", None),
    ("DELETE FROM acp_shared.quarter_plan", None),
    ("UPDATE acp_shared.quarter_plan_version SET payload = '{}'::jsonb", None),
    ("UPDATE acp_shared.quarter_plan_version SET plan_id = plan_id", None),
    ("DELETE FROM acp_shared.quarter_plan_version", None),
    ("UPDATE acp_deliver.tenant_tour_pages SET url_alive = false", None),
    ("INSERT INTO acp_deliver.tenant_tour_pages (tenant_id, tour_id, url) VALUES (%s, gen_random_uuid(), 'x')", "a"),
    ("DELETE FROM acp_deliver.tenant_tour_pages", None),
    ("TRUNCATE acp_deliver.tenant_tour_pages", None),
])
def test_planning_columns_outside_the_grant_are_refused(cur, two_tenants, sql, params):
    """265 grants exactly the moved statements' columns: tenant_id is never updatable, no DELETE / TRUNCATE (which RLS
    does not filter), tenant_tour_pages is read only for the tenant role (the trip page / publish write it on MAIN)."""
    a, b = two_tenants
    args = () if params is None else ({"a": a, "b": b}[params],)
    with _as_tenant(cur, a) as c:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
            c.execute(sql, args)


def test_tenant_tour_pages_show_own_and_master_pages_only(cur, two_tenants):
    a, b = two_tenants
    cur.execute("INSERT INTO acp_deliver.tenant_tour_pages (tenant_id, tour_id, url) VALUES (%s, %s, 'https://m')",
                (MASTER_TENANT, str(uuid.uuid4())))
    ours = {a, b, MASTER_TENANT}
    assert _visible_tenants(cur, "acp_deliver.tenant_tour_pages", a) & ours == {a, MASTER_TENANT}
    assert _visible_tenants(cur, "acp_deliver.tenant_tour_pages", b) & ours == {b, MASTER_TENANT}
    assert _visible_tenants(cur, "acp_deliver.tenant_tour_pages", str(uuid.uuid4())) & ours == {MASTER_TENANT}
    assert _visible_tenants(cur, "acp_deliver.tenant_tour_pages", "") & ours == {MASTER_TENANT}


# ── 266 (AA-859 wave 4): tour_atoms, every platform atom + the tenant's own, read only ──────────────────

@pytest.fixture
def atoms(cur, two_tenants):
    a, b = two_tenants
    cur.execute("INSERT INTO silver_aa_internal.raw_tours (tenant_id, batch_id, src_name) "
                "VALUES (%s, gen_random_uuid(), 'aa859-266') RETURNING tour_id", (MASTER_TENANT,))
    tour = cur.fetchone()[0]
    ids = {}
    for scope in ("platform", a, b):
        atom = f"aa859-266-{uuid.uuid4().hex[:10]}"
        cur.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text) "
                    "VALUES (%s, %s, %s, 'x')", (atom, tour, scope))
        ids[scope] = atom
    return a, b, ids


def _visible_atoms(c, guc, ids):
    with _as_tenant(c, guc):
        c.execute(f"SELECT atom_id FROM {ATOMS}")          # no WHERE on purpose
        return {r[0] for r in c.fetchall()} & set(ids.values())


def test_tour_atoms_platform_plus_own_only(cur, atoms):
    a, b, ids = atoms
    assert _visible_atoms(cur, a, ids) == {ids["platform"], ids[a]}
    assert _visible_atoms(cur, b, ids) == {ids["platform"], ids[b]}
    assert _visible_atoms(cur, str(uuid.uuid4()), ids) == {ids["platform"]}
    assert _visible_atoms(cur, "", ids) == {ids["platform"]}, "'' is no tenant: platform atoms only, no 22P02"


@pytest.mark.parametrize("sql", [
    f"UPDATE {ATOMS} SET weight = weight",
    f"DELETE FROM {ATOMS}",
    f"INSERT INTO {ATOMS} (atom_id, tour_id, text) SELECT 'zz', tour_id, 'x' FROM {ATOMS} LIMIT 1",
    f"TRUNCATE {ATOMS}",
])
def test_tour_atoms_are_read_only_for_the_tenant_role(cur, atoms, sql):
    a, _, _ = atoms
    with _as_tenant(cur, a) as c:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="permission denied"):
            c.execute(sql)
