"""AA-795 — migration 300: gold_aa_internal.tenant_tour_versions.judge_breakdown (jsonb, nullable). Real Postgres.

The CI replay has no tenant_tour_versions (CIS 001 is skipped), so the replay's run of 300 refuses with its guard and
registers nothing. These tests run the file directly (errors NOT swallowed): its refusal without the table, then on a
stand-in (twice: idempotent) the column's shape and registration, and the admin list's two derived columns
(`admin_content_quality._JUDGE_LIST_COLUMNS`) on real rows. Every test leaves the replay state behind (no stand-in,
300 not registered).
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "300_tenant_tour_versions_judge_breakdown.sql")
STANDIN_MARK = "aa795_migration_300_standin"
STANDIN_DDL = f"""
    CREATE TABLE gold_aa_internal.tenant_tour_versions (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id         UUID NOT NULL,
        published_tour_id UUID,
        rewritten_content JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        status            TEXT NOT NULL DEFAULT 'pending',
        edit_source       TEXT NOT NULL DEFAULT 'ai_generated',
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        job_id            UUID
    );
    COMMENT ON TABLE gold_aa_internal.tenant_tour_versions IS '{STANDIN_MARK}';
"""


def _connect():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    return conn


def _run(c):
    with open(MIGRATION, encoding="utf-8") as f:
        c.execute(f.read())


def _table(c):
    c.execute("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL")
    return c.fetchone()[0]


def _registered(c):
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '300'")
    return c.fetchone()[0]


@pytest.fixture
def cur():
    conn = _connect()
    c = conn.cursor()
    if _table(c):
        conn.close()
        pytest.skip("this DB has a real tenant_tour_versions; the test only runs on the CI replay shape")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("SELECT obj_description('gold_aa_internal.tenant_tour_versions'::regclass, 'pg_class')"
                  if _table(c) else "SELECT NULL")
        if c.fetchone()[0] == STANDIN_MARK:
            c.execute("DROP TABLE gold_aa_internal.tenant_tour_versions")
        c.execute("DELETE FROM shared.schema_versions WHERE version = '300'")
        conn.close()


def test_300_refuses_without_the_table(cur):
    with pytest.raises(psycopg2.Error) as e:
        _run(cur)
    assert "tenant_tour_versions is missing" in str(e.value)
    cur.execute("ROLLBACK")
    assert _registered(cur) == 0


def test_300_adds_a_nullable_jsonb_column_and_registers_idempotently(cur):
    cur.execute(STANDIN_DDL)
    _run(cur)
    _run(cur)                                   # idempotent: ADD COLUMN IF NOT EXISTS, ON CONFLICT DO NOTHING
    cur.execute("""SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
                          col_description(a.attrelid, a.attnum)
                     FROM pg_attribute a
                     LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = 'gold_aa_internal.tenant_tour_versions'::regclass
                      AND a.attname = 'judge_breakdown' AND NOT a.attisdropped""")
    typ, notnull, default, comment = cur.fetchone()
    assert (typ, notnull, default) == ("jsonb", False, None)
    assert "Admin-only" in comment and "brand_claims" in comment
    assert _registered(cur) == 1
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'gold_aa_internal.tenant_tour_versions'::regclass"
                " AND contype = 'c'")
    assert cur.fetchone()[0] == 0               # no CHECK: the shape is the app's


def test_the_admin_list_columns_read_the_breakdown(cur):
    from api.routers.admin_content_quality import _JUDGE_LIST_COLUMNS
    from services.content_generation import t2_brand_claims, t2_judge_breakdown
    cur.execute(STANDIN_DDL)
    _run(cur)
    tenant = str(uuid.uuid4())
    claims = t2_brand_claims.check({"summary": "A trek for children aged seven to sixteen."}, "Day 1: Walk.")
    flagged = t2_judge_breakdown.build(
        {"judge_brand_fit": 8.0, "judge_cross_brand_distinct": 7.0, "judge_mission_present": True,
         "judge_score": 8.0, "judge_feedback": "Warm, specific.",
         "judge_t2_axes": {"gate": "brand_fit_mission", "validate_score": 9.0, "binding": "brand_fit"}},
        final_score=8.0, brand_claims=claims)
    rows = {}
    for name, value in (("flagged", flagged), ("no_check", {"binding": "validate"}), ("null", None)):
        cur.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, judge_breakdown) "
                    "VALUES (%s, %s::jsonb) RETURNING id::text",
                    (tenant, json.dumps(value) if value is not None else None))
        rows[cur.fetchone()[0]] = name
    cur.execute(f"SELECT ttv.id::text, {_JUDGE_LIST_COLUMNS} FROM gold_aa_internal.tenant_tour_versions ttv "
                "WHERE ttv.tenant_id = %s", (tenant,))
    got = {rows[r[0]]: (r[1], r[2]) for r in cur.fetchall()}
    assert got == {"flagged": ("brand_fit", 1), "no_check": ("validate", None), "null": (None, None)}
