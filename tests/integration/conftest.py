"""
Integration test fixtures for AA-CIS pipeline.
NO Docker required — connects to local PostgreSQL 16 + fakeredis.
Test DB: cis_integration_test / cistest

Setup (one-time):
    su -c "psql -c \"CREATE USER cistest WITH PASSWORD 'cistest' SUPERUSER;\"" postgres
    su -c "psql -c \"CREATE DATABASE cis_integration_test OWNER cistest;\"" postgres

cistest must be SUPERUSER, as in CI (.github/workflows: postgres:15 service). Without it the role
creation below and every migration that grants to / asserts on other roles (203, 225 ...) fail;
_apply_schema swallows migration errors, so the symptom is failing tests, not a setup error.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import uuid
import json
import pytest
import psycopg2
import fakeredis
from unittest.mock import MagicMock, AsyncMock, patch
from _constants import (  # noqa: F401 — re-exported for test imports
    SAMPLE_TOUR, SAMPLE_SEO, SAMPLE_GENERATED, TENANT_ID, BATCH_ID
)

# ── Connection params ─────────────────────────────────────────────
DB_HOST = os.environ.get("CIS_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("CIS_TEST_DB_PORT", "5432"))
DB_NAME = os.environ.get("CIS_TEST_DB_NAME", "cis_integration_test")
DB_USER = os.environ.get("CIS_TEST_DB_USER", "cistest")
DB_PASS = os.environ.get("CIS_TEST_DB_PASS", "cistest")

_SCHEMA_APPLIED = False

# AA-817: stand-in for gold_aa_internal.tenant_tour_versions (live table created by migration 001, which
# this DB skips). Columns used by the RLS tests, with defaults; no version_number, so test_aa766's
# _ttv_minimal() treats it as a stand-in too.
TTV_STANDIN_DDL = """
    CREATE TABLE IF NOT EXISTS gold_aa_internal.tenant_tour_versions (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         UUID NOT NULL,
        published_tour_id UUID,
        rewritten_content JSONB NOT NULL DEFAULT '{}'::jsonb,
        status            TEXT NOT NULL DEFAULT 'draft',
        edit_source       TEXT NOT NULL DEFAULT 'ai_generated',
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""


def _get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS,
    )


def _apply_schema(conn):
    global _SCHEMA_APPLIED
    if _SCHEMA_APPLIED:
        return
    conn.autocommit = True
    cur = conn.cursor()

    # Drop all schemas and recreate fresh
    cur.execute("""
        DROP SCHEMA IF EXISTS gold_aa_internal CASCADE;
        DROP SCHEMA IF EXISTS silver_aa_internal CASCADE;
        DROP SCHEMA IF EXISTS shared CASCADE;
        DROP SCHEMA IF EXISTS ops CASCADE;
        DROP TYPE IF EXISTS pipeline_status_enum CASCADE;
        DROP TYPE IF EXISTS content_status_enum CASCADE;
        DROP TYPE IF EXISTS review_status_enum CASCADE;
        DROP TYPE IF EXISTS webhook_status_enum CASCADE;
    """)

    # Apply real migrations in order
    migrations_dir = os.path.join(
        os.path.dirname(__file__), '..', '..', 'api', 'migrations'
    )
    # Create the roles required by GRANT statements in migrations. aa_app_user (AA-741, S231):
    # without it migration 203 (acp_contract.v_active_tour_atoms) and the other ~50 GRANTs to it
    # failed silently, so no integration test ever ran against that view.
    cur.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aa_app_user') THEN
                CREATE ROLE aa_app_user;
            END IF;
            -- AA-679: the consumer roles of the cross-owner grants (migration 225 grants catalog.*
            -- to them unguarded, as schema-ownership rule 5 requires; 173/180/201/216 too, guarded).
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tripplanner') THEN
                CREATE ROLE tripplanner;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'booking') THEN
                CREATE ROLE booking;
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            NULL;  -- a local non-superuser test DB; CI's postgres service user is a superuser
        END $$;
    """)
    # AA-412: pre-create acp_deliver/acp_contract (real owner: migration 078) so migrations that
    # depend on them (094/096/102/105 — acp_deliver.packets/pieces + this issue's review columns)
    # can apply even though 078 itself fails locally (its v_trip_registry view selects
    # pt.content_embedding, a pgvector column this local test DB was never given — unrelated to
    # AA-412, not fixed here). 078's own CREATE SCHEMA statements never survive because that
    # whole file runs in one transaction that rolls back on its later error; pre-creating the
    # schemas here (idempotent, same "PATCHING" convention as the 004/005/006 special-cases
    # below) unblocks every acp_deliver/acp_contract migration without touching 078 itself.
    cur.execute("CREATE SCHEMA IF NOT EXISTS acp_deliver;")
    cur.execute("CREATE SCHEMA IF NOT EXISTS acp_contract;")
    # AA-412 (2nd gap, same root cause): migration 094's acp_deliver.pieces FK originally pointed
    # at acp_shared.acp_runs (re-pointed to acp_shared.acp_v2_runs only later, migration 096 — see
    # that file's own "Re-point..." comment) — but acp_shared.acp_runs itself was created as
    # `shared.acp_runs` by migration 009 (wrong schema name, a pre-existing bug independent of
    # AA-412: acpcore's ACPv1 lineage, migrations 019/025/026/028/031/055/064/065, has referenced
    # the never-created `acp_shared.acp_runs` throughout). This never surfaced before because
    # nothing under acp_deliver/acp_contract had ever successfully applied locally (see the
    # schema-creation patch above) to reach the point of needing this FK target to exist. A
    # minimal compatibility stub (just the PK column the FK constraint needs) is enough — 094's
    # real FK gets re-pointed away from this table 2 migrations later (096) and never touches it
    # again.
    cur.execute("CREATE SCHEMA IF NOT EXISTS acp_shared;")
    cur.execute("CREATE TABLE IF NOT EXISTS acp_shared.acp_runs (run_id UUID PRIMARY KEY);")

    migration_files = sorted([
        f for f in os.listdir(migrations_dir)
        if f.endswith('.sql')
        and not f.startswith('001')       # old ops/silver schema — superseded by 003
        and not f.startswith('002')       # depends on 001 ops schema
        and not f.startswith('007_seed')
        and not f.startswith('007b')
        and not f.startswith('008')
    ])

    for mf in migration_files:
        fpath = os.path.join(migrations_dir, mf)
        sql = open(fpath).read()
        # Strip shell redirects that accidentally ended up in SQL
        sql = sql.replace("2>/dev/null", "")
        if mf.startswith("198_"):
            # AA-679: shared.destinations is CIS-owned since ADR 0002, but the file that created it is
            # TripPlanner's migrations/002_shared_destinations.sql (+ 005 located_by/located_at), which
            # this DB never runs. 198 (place_photo FK), 201 (revoke) and 225 (catalog views) need it,
            # so create it here in its live shape (db-schema-reference.md).
            sql = """
                CREATE TABLE IF NOT EXISTS shared.destinations (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name            TEXT NOT NULL,
                    country         TEXT NOT NULL,
                    lat             DOUBLE PRECISION NOT NULL,
                    lng             DOUBLE PRECISION NOT NULL,
                    cover_image_url TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    located_by      TEXT,
                    located_at      TIMESTAMPTZ
                );
                CREATE UNIQUE INDEX IF NOT EXISTS destinations_lower_name_uidx
                    ON shared.destinations (lower(name));
            """ + sql
        if mf.startswith("229_"):
            # AA-817: 229 sets RLS + grants on gold_aa_internal.tenant_tour_versions, which only
            # migration 001 creates (skipped here), and its guard refuses a missing table. Apply it
            # against a stand-in, dropped again right after (below), so every other test sees the
            # same DB as before 229; test_aa817_* recreate the stand-in in their own transaction.
            cur.execute(TTV_STANDIN_DDL)
        if "005_tenant_config" in mf:
            print(f"PATCHING 005: {mf}")
            # Fix: tenant_id TEXT → UUID
            sql = sql.replace(
                "tenant_id       TEXT NOT NULL REFERENCES shared.tenants(tenant_id),",
                "tenant_id       UUID NOT NULL REFERENCES shared.tenants(tenant_id),",
            )
            # Fix seed: string tenant_id → UUID
            sql = sql.replace(
                "'aa_internal'",
                "'00000000-0000-0000-0000-000000000001'",
            )
            sql = sql.replace(
                "'wl_tenant_b2b_test'",
                "'00000000-0000-0000-0000-000000000099'",
            )

            # Strip GRANT SEQUENCE lines — sequences exist but GRANT fails in test env
            lines = sql.splitlines()
            sql = "\n".join(
                l for l in lines
                if not (l.strip().startswith("GRANT") and "SEQUENCE" in l)
            )
        if "006_export_webhook" in mf:
            print(f"PATCHING 006: {mf}")
            # Fix: tenant_id TEXT → UUID (FK to shared.tenants.tenant_id UUID)
            sql = sql.replace(
                "tenant_id       TEXT NOT NULL REFERENCES shared.tenants(tenant_id),",
                "tenant_id       UUID NOT NULL REFERENCES shared.tenants(tenant_id),",
            )
            # Fix: RLS policy cast
            sql = sql.replace(
                "USING (tenant_id = current_setting('app.tenant_id', true))",
                "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)",
            )
            # Strip GRANT SEQUENCE lines
            lines = sql.splitlines()
            sql = "\n".join(
                l for l in lines
                if not (l.strip().startswith("GRANT") and "SEQUENCE" in l)
            )
        # Fix 004: tenant_id TEXT → UUID to match shared.tenants(tenant_id UUID)
        if "004_tenant_rls" in mf:
            print(f"PATCHING 004: {mf}")
            # Fix 1: tenant_id TEXT → UUID (FK to shared.tenants.tenant_id UUID)
            sql = sql.replace(
                "ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'aa_internal'",
                "ADD COLUMN IF NOT EXISTS tenant_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'",
            )
            # Fix 2: seed INSERT missing slug + UUID string → real UUID
            sql = sql.replace(
                "INSERT INTO shared.tenants (tenant_id, name, plan_tier)",
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier)",
            )
            sql = sql.replace(
                "VALUES ('wl_tenant_b2b_test', 'Test B2B Tenant', 'starter')",
                "VALUES ('00000000-0000-0000-0000-000000000099', 'Test B2B Tenant', 'test-b2b', 'starter')",
            )
            # Fix 3: RLS policy current_setting() returns TEXT → cast to UUID
            sql = sql.replace(
                "USING (tenant_id = current_setting('app.tenant_id', true))",
                "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)",
            )
        # Use fresh connection per migration to avoid aborted transaction state
        try:
            mconn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASS,
            )
            mconn.autocommit = True
            mcur = mconn.cursor()
            mcur.execute(sql)
            mcur.close()
            mconn.close()
            print(f"Applied: {mf}")
        except Exception as e:
            print(f"Warning {mf}: {e}")
            try: mconn.close()
            except: pass
        if mf.startswith("229_"):
            cur.execute("DROP TABLE IF EXISTS gold_aa_internal.tenant_tour_versions")

    # Drop FK constraints in test env — cross-table seeding is fragile
    cur.execute("""
        ALTER TABLE silver_aa_internal.raw_tours
            DROP CONSTRAINT IF EXISTS raw_tours_batch_id_fkey;
        ALTER TABLE silver_aa_internal.seo_context
            DROP CONSTRAINT IF EXISTS seo_context_batch_id_fkey;
        ALTER TABLE silver_aa_internal.generated_content
            DROP CONSTRAINT IF EXISTS generated_content_batch_id_fkey;
        ALTER TABLE silver_aa_internal.quality_scores
            DROP CONSTRAINT IF EXISTS quality_scores_batch_id_fkey;
        ALTER TABLE gold_aa_internal.published_tours
            DROP CONSTRAINT IF EXISTS published_tours_tour_id_fkey;
        ALTER TABLE gold_aa_internal.published_tours
            DROP CONSTRAINT IF EXISTS published_tours_generated_content_id_fkey;
    """)

    # Set tenant_id column defaults from app.tenant_id setting (test convenience)
    cur.execute("""
        ALTER TABLE silver_aa_internal.raw_tours
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
        ALTER TABLE silver_aa_internal.seo_context
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
        ALTER TABLE silver_aa_internal.generated_content
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
        ALTER TABLE silver_aa_internal.quality_scores
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
        ALTER TABLE gold_aa_internal.published_tours
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
        ALTER TABLE shared.pipeline_runs
            ALTER COLUMN tenant_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
    """)

    # Add columns missing from schema — used by test_export_integration.py/test_pipeline_e2e.py
    # (raw SQL, not the AA-454-removed ExportService, which was dead code with no real caller)
    cur.execute("""
        ALTER TABLE gold_aa_internal.published_tours
            ADD COLUMN IF NOT EXISTS slug TEXT,
            ADD COLUMN IF NOT EXISTS country TEXT,
            ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS quality_score NUMERIC(4,2);
    """)

    # Add test-only columns missing from schema (test code uses them)
    cur.execute("""
        ALTER TABLE silver_aa_internal.quality_scores
            ADD COLUMN IF NOT EXISTS hitl_flag BOOLEAN DEFAULT FALSE;
    """)

    # Fix 005 seed: UPDATE brand_rules with correct system_prompt (ON CONFLICT DO NOTHING skips if row exists)
    cur.execute("""
        UPDATE shared.tenant_brand_rules
        SET system_prompt = 'You are an expert travel content writer for Adventure Asia. Write in an engaging, active voice that inspires travellers.',
            style_guide = 'Use title case for tour names. Subtitles must be descriptive clauses, not city lists. Summaries 80-150 words.',
            forbidden_words = '["guaranteed", "best in class", "world-class", "unparalleled", "once in a lifetime"]'::jsonb
        WHERE tenant_id = '00000000-0000-0000-0000-000000000001';

        UPDATE shared.tenant_brand_rules
        SET system_prompt = 'Write professional tour content for WorldLux travel brand.',
            style_guide = 'Use formal tone. Highlight luxury aspects. Avoid casual language.',
            forbidden_words = '["cheap", "budget", "affordable", "basic"]'::jsonb
        WHERE tenant_id = '00000000-0000-0000-0000-000000000099';
    """)

    # Grant app_user access to all schemas/tables (after migrations applied)
    cur.execute("""
        GRANT USAGE ON SCHEMA shared, silver_aa_internal, gold_aa_internal TO app_user;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA silver_aa_internal TO app_user;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA gold_aa_internal TO app_user;
        GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA shared TO app_user;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA silver_aa_internal TO app_user;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA gold_aa_internal TO app_user;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared TO app_user;
    """)
    # AA-817 (237): on Dev only the owner and aa_app_user hold privileges on shared.tenant_integrations,
    # and 237's guard 1 refuses any other grantee (with RLS on and a policy TO aa_app_user only, it would
    # read 0 rows). Keep the CI-only app_user off it so test_aa817_* can re-apply 237 per test.
    cur.execute("REVOKE ALL ON shared.tenant_integrations FROM app_user")

    # Seed pipeline_runs for both tenants (FK required by raw_tours.batch_id)
    # Must set RLS context + use BYPASSRLS or set app.tenant_id
    # Seed pipeline_runs for tenant A
    cur.execute("SET app.tenant_id = '00000000-0000-0000-0000-000000000001'")
    cur.execute("""
        INSERT INTO shared.pipeline_runs
            (id, tenant_id, batch_id, status, tours_total, tours_passed,
             tours_hitl, tours_failed, started_at)
        VALUES (%s, %s, %s, 'completed', 0, 0, 0, 0, NOW())
        ON CONFLICT DO NOTHING;
    """, (BATCH_ID, TENANT_ID, BATCH_ID))
    # Seed pipeline_runs for tenant B
    cur.execute("SET app.tenant_id = '00000000-0000-0000-0000-000000000099'")
    cur.execute("""
        INSERT INTO shared.pipeline_runs
            (id, tenant_id, batch_id, status, tours_total, tours_passed,
             tours_hitl, tours_failed, started_at)
        VALUES ('00000000-0000-0000-0000-000000000098', '00000000-0000-0000-0000-000000000099', '00000000-0000-0000-0000-000000000098',
                'completed', 0, 0, 0, 0, NOW())
        ON CONFLICT DO NOTHING;
    """)
    cur.execute("RESET app.tenant_id")

    # Seed test tenant
    cur.execute("""
        INSERT INTO shared.tenants
            (tenant_id, name, slug, plan_tier, posts_per_week, api_key_hash, rate_limit_rpm, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;
    """, (
        TENANT_ID, 'Test Tenant', 'test-tenant',
        'internal', 7, None, 60, True
    ))

    cur.close()
    _SCHEMA_APPLIED = True


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    """Apply schema once per session."""
    conn = _get_conn()
    _apply_schema(conn)
    conn.close()


@pytest.fixture
def db_conn():
    """Function-scoped connection. Truncates all tables on teardown."""
    from _constants import BATCH_ID, TENANT_ID
    conn = _get_conn()
    conn.autocommit = True
    # Seed pipeline_runs for FK constraint
    cur = conn.cursor()
    cur.execute("SET app.tenant_id = %s", (TENANT_ID,))
    cur.execute("""
        INSERT INTO shared.pipeline_runs
            (id, tenant_id, batch_id, status, tours_total, tours_passed, tours_hitl, tours_failed, started_at)
        VALUES (%s, %s, %s, 'completed', 0, 0, 0, 0, NOW())
        ON CONFLICT DO NOTHING;
    """, (BATCH_ID, TENANT_ID, BATCH_ID))
    # Keep app.tenant_id set for duration of test — RLS requires it
    cur.close()
    yield conn
    cur = conn.cursor()
    cur.execute("""
        TRUNCATE TABLE
            gold_aa_internal.published_tours,
            silver_aa_internal.quality_scores,
            silver_aa_internal.generated_content,
            silver_aa_internal.seo_context,
            silver_aa_internal.raw_tours,
            shared.pipeline_runs
        RESTART IDENTITY CASCADE;
    """)
    cur.close()
    conn.close()


# ── fakeredis ─────────────────────────────────────────────────────

_fake_server = fakeredis.FakeServer(version=(7, 0, 0))


@pytest.fixture
def redis_client():
    client = fakeredis.FakeRedis(server=_fake_server, decode_responses=True)
    yield client
    client.flushall()


# ── Mock fixtures ─────────────────────────────────────────────────

@pytest.fixture
def mock_s3():
    with patch("boto3.client") as mock:
        s3 = MagicMock()
        mock.return_value = s3
        s3.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b'{"tours": []}')}
        yield s3


@pytest.fixture
def mock_llm_client():
    client = AsyncMock()
    client.generate.return_value = {
        "content": SAMPLE_GENERATED,
        "model": "claude-3-5-sonnet-20241022",
        "tokens_input": 1200, "tokens_output": 800,
        "cost_usd": 0.018, "cached": False,
    }
    return client


@pytest.fixture
def mock_dataforseo():
    client = MagicMock()
    client.get_keywords.return_value = SAMPLE_SEO
    return client


@pytest.fixture
def mock_langfuse():
    lf = MagicMock()
    lf.trace.return_value = MagicMock(id="trace-test-001")
    return lf
