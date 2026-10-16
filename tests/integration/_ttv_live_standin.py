"""AA-859 (PR-D6, reused by PR-D5): a live-shape stand-in for gold_aa_internal.tenant_tour_versions.

The CI DB never creates the live table (migration 001 is skipped). conftest's TTV_STANDIN_DDL is the
minimal stand-in the AA-817 RLS tests use, and it must stay without `version_number` (test_aa766's
_ttv_minimal() detects a stand-in by that). The T2 version routes need the live columns, so this helper
creates the live shape (db-schema-reference.md: every column, the UNIQUE (tenant_id, published_tour_id,
version_number), the CHECKs and the three FKs), inside the caller's own transaction, then re-applies
migration 229 (RLS tenant_isolation + SELECT, INSERT, UPDATE to aa_app_user) on it. With
master_read=True it also re-applies 239 (the tenant role reads the AA master published_tours /
raw_tours / seo_context rows, for the pt/rt/sc joins of the read routes) and 241 after it, as
test_aa817_rls_tenant_isolation does. The caller rolls the transaction back, so nothing persists.

Defaults: the live dump has no column defaults; the ones below are what the live route INSERTs rely on
(the edit INSERT names neither rewrite_language nor seo_mode nor the qa_* columns).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _rls_fixture_helpers import migration_body, undo_241_for_239_reapply  # noqa: E402

TTV_LIVE_STANDIN_DDL = """
    CREATE TABLE gold_aa_internal.tenant_tour_versions (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         UUID NOT NULL,
        published_tour_id UUID NOT NULL REFERENCES gold_aa_internal.published_tours(id),
        version_number    INTEGER NOT NULL,
        parent_version_id UUID REFERENCES gold_aa_internal.tenant_tour_versions(id),
        rewritten_content JSONB NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending',
        quality_score     NUMERIC(4,2),
        edit_source       TEXT NOT NULL DEFAULT 'ai_generated',
        rewrite_language  TEXT NOT NULL DEFAULT 'en-US',
        seo_mode          TEXT NOT NULL DEFAULT 'standard',
        edited_at         TIMESTAMPTZ,
        edited_by_user    TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        qa_status         TEXT NOT NULL DEFAULT 'pending',
        qa_repair_count   SMALLINT NOT NULL DEFAULT 0,
        qa_checked_at     TIMESTAMPTZ,
        qa_auto_passed    BOOLEAN NOT NULL DEFAULT false,
        job_id            UUID REFERENCES shared.job(id),
        CONSTRAINT tenant_tour_versions_tenant_id_published_tour_id_version_nu_key
            UNIQUE (tenant_id, published_tour_id, version_number),
        CONSTRAINT tenant_tour_versions_edit_source_check
            CHECK (edit_source = ANY (ARRAY['ai_generated', 'tenant_edit', 're_rewrite'])),
        CONSTRAINT tenant_tour_versions_qa_status_check
            CHECK (qa_status = ANY (ARRAY['pending', 'passed', 'failed', 'escalated'])),
        CONSTRAINT tenant_tour_versions_status_check
            CHECK (status = ANY (ARRAY['pending', 'approved', 'rejected', 'needs_review', 'ai_generated',
                                       'failed']))
    );
    CREATE INDEX idx_ttv_tenant_id ON gold_aa_internal.tenant_tour_versions (tenant_id);
    CREATE INDEX idx_ttv_published_tour ON gold_aa_internal.tenant_tour_versions (published_tour_id);
"""


class _Collect:
    """A cursor stand-in that records statements, so the sync helper undo_241_for_239_reapply can feed
    an asyncpg connection too."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        assert params is None, "undo_241_for_239_reapply passes no parameters"
        self.statements.append(sql)


def ttv_live_standin_statements(master_read: bool = False) -> list:
    """Every statement, in order. Plain CREATE TABLE (no IF NOT EXISTS): a leftover table of another
    shape fails loudly instead of being tested against."""
    statements = [TTV_LIVE_STANDIN_DDL, migration_body(229)]
    if master_read:
        undo = _Collect()
        undo_241_for_239_reapply(undo)
        statements += undo.statements + [migration_body(239), migration_body(241)]
    return statements


async def install_ttv_live_standin(conn, master_read: bool = False):
    """asyncpg: run inside a transaction the caller rolls back."""
    for sql in ttv_live_standin_statements(master_read):
        await conn.execute(sql)


def install_ttv_live_standin_sync(cur, master_read: bool = False):
    """psycopg2 cursor (autocommit off): same, for sync tests."""
    for sql in ttv_live_standin_statements(master_read):
        cur.execute(sql)


async def seed_published_tour(conn, name: str = "AA-859 stand-in tour"):
    """One AA master published_tours row (the FK target; CI dropped published_tours' own FKs)."""
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, aa_name) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), $1) RETURNING id", name)
