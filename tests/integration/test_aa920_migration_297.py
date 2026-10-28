"""AA-920 WM-25 — migration 297: gold_aa_internal.tenant_tour_versions.generation_meta (jsonb, nullable). Real Postgres.

The CI replay has no tenant_tour_versions (CIS 001 is skipped; 229 runs against a stand-in dropped right after), so
the replay's run of 297 refuses with its guard and registers nothing. These tests run the file directly (errors NOT
swallowed): its refusal without the table, then on a stand-in with the live columns the code reads (twice:
idempotent), the column's shape, its registration, and the post-activation T2 read (`post_activation._T2_SQL`) on it:
a version stamped in `generation_meta` is read even when its job row is gone, one with neither stamp nor job is not.
Every test leaves the replay state behind (no stand-in, 297 not registered).
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
                         "297_tenant_tour_versions_generation_meta.sql")
STANDIN_MARK = "aa920_migration_297_standin"
# The live columns the T2 read touches (db-schema-reference.md), nothing more.
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
    c.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '297'")
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
        c.execute("DELETE FROM shared.schema_versions WHERE version = '297'")
        c.execute("DELETE FROM shared.job WHERE payload->>'probe' = %s", (STANDIN_MARK,))
        conn.close()


def test_297_refuses_without_the_table(cur):
    with pytest.raises(psycopg2.Error) as e:
        _run(cur)
    assert "tenant_tour_versions is missing" in str(e.value)
    cur.execute("ROLLBACK")
    assert _registered(cur) == 0


def test_297_adds_a_nullable_jsonb_column_and_registers_idempotently(cur):
    cur.execute(STANDIN_DDL)
    _run(cur)
    _run(cur)                                   # idempotent: ADD COLUMN IF NOT EXISTS, ON CONFLICT DO NOTHING
    cur.execute("""SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
                          col_description(a.attrelid, a.attnum)
                     FROM pg_attribute a
                     LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE a.attrelid = 'gold_aa_internal.tenant_tour_versions'::regclass
                      AND a.attname = 'generation_meta' AND NOT a.attisdropped""")
    typ, notnull, default, comment = cur.fetchone()
    assert (typ, notnull, default) == ("jsonb", False, None)
    assert "Admin-only" in comment and "never rule text" in comment
    assert _registered(cur) == 1
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'gold_aa_internal.tenant_tour_versions'::regclass"
                " AND contype = 'c'")
    assert cur.fetchone()[0] == 0               # no CHECK: the shape is the app's stamp


def test_the_post_activation_t2_read_uses_generation_meta(cur):
    from services.writing_memory.post_activation import _T2_SQL
    cur.execute(STANDIN_DDL)
    _run(cur)
    tenant = str(uuid.uuid4())
    meta = {"writing_release": {"writer": "t2", "platform": {"release_id": "p", "release_no": 1,
                                                             "items_sha256": "a" * 64},
                                "tenant": None, "ignored": False}, "rule_ids": ["r1"]}
    job_stamp = {"writer": "t2", "platform": {"release_id": "p", "release_no": 1, "items_sha256": "b" * 64},
                 "tenant": None, "ignored": False}
    cur.execute("INSERT INTO shared.job (kind, payload, result) VALUES ('t2_rewrite', %s::jsonb, %s::jsonb) "
                "RETURNING id::text",
                (json.dumps({"probe": STANDIN_MARK}), json.dumps({"writing_release": job_stamp})))
    job_id = cur.fetchone()[0]
    rows = {}
    for name, meta_v, job in (("stamped_no_job", meta, None),          # job row gone: its own stamp still counts
                              ("stamped_and_job", meta, job_id),       # generation_meta wins over the job result
                              ("job_only", None, job_id),              # written before 297: the job result
                              ("neither", None, None)):                # a tenant edit: not a T2 write
        cur.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, rewritten_content, job_id, "
                    "generation_meta) VALUES (%s, '{\"name\": \"x\"}'::jsonb, %s, %s::jsonb) RETURNING id::text",
                    (tenant, job, json.dumps(meta_v) if meta_v else None))
        rows[cur.fetchone()[0]] = name
    cur.execute(f"PREPARE aa920_t2 AS {_T2_SQL}")
    try:
        cur.execute("EXECUTE aa920_t2 (now() - interval '1 hour', NULL, %s::uuid, 50)", (tenant,))
        got = {rows[r[0]]: r[4] for r in cur.fetchall()}
    finally:
        cur.execute("DEALLOCATE aa920_t2")
    assert set(got) == {"stamped_no_job", "stamped_and_job", "job_only"}
    assert got["stamped_no_job"] == meta["writing_release"]
    assert got["stamped_and_job"] == meta["writing_release"]
    assert got["job_only"] == job_stamp


def test_readiness_caps_the_version_while_297_is_missing(cur):
    """AA-920: max() is already above 297 on Dev (299), so /health/ready reports a version capped below the lowest
    out-of-order version that is not registered: 296 without 297, the real head once it is registered."""
    from api.core import readiness
    sql = readiness.SCHEMA_VERSION_SQL.replace("$1::int[]", "%s::int[]")
    probe = "aa920_readiness_probe"
    cur.execute("SELECT max(version::int) FROM shared.schema_versions WHERE version ~ '^[0-9]+$'")
    head = cur.fetchone()[0]
    added = head < 299
    if added:                                   # make the replay look like Dev: a head above 297
        cur.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                    "VALUES ('299', now(), %s) ON CONFLICT (version) DO NOTHING", (probe,))
    try:
        cur.execute(sql, (list(readiness.OUT_OF_ORDER_CIS),))
        assert cur.fetchone()[0] == 296         # 297 not registered (the fixture guarantees it)
        cur.execute(STANDIN_DDL)
        _run(cur)
        cur.execute(sql, (list(readiness.OUT_OF_ORDER_CIS),))
        # AA-795: REQUIRED_CIS_SCHEMA is 300 now (above this probe's head); the cap is what this test pins.
        assert cur.fetchone()[0] == max(head, 299) >= 297
    finally:
        cur.execute("DELETE FROM shared.schema_versions WHERE version = '299' AND description = %s", (probe,))
