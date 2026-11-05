"""AA-1039 (R6) — migration 373 (shared.llm_role_config.pii_class) against real Postgres.

conftest applies every migration; these tests also run the file again directly, errors NOT swallowed (lesson S231):
registration, every seeded stage is content, the two CHECKs (the class values; a pii stage only with allowed_scopes =
{aws_only}, not NULL, not vendor_direct), idempotence, that the runtime role cannot change a class, and that the
decision path (decide._load_config) reads the pii stages from the real table. Every test removes its own rows.
"""
import asyncio
import os
import sys

import asyncpg
import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.llm_client import decide  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "373_llm_stage_pii_class.sql")
PREFIX = "zz_aa1039_pii_"


def _connect():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    return conn


@pytest.fixture
def cur():
    conn = _connect()
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_role_config WHERE stage LIKE %s", (PREFIX + "%",))
        conn.close()


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _stage(c, name, scopes="ARRAY['aws_only']", pii=None):
    cols, vals = "", ""
    if pii is not None:
        cols, vals = ", pii_class", f", '{pii}'"
    c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, updated_by, "
              f"fallback_model_ids, shadow_sample_pct, allowed_scopes{cols}) "
              f"VALUES (%s, 'writer', 'claude', 'haiku-4-5', true, now(), 'test', '{{}}', 0, {scopes}{vals})",
              (PREFIX + name,))


def _class(c, name):
    c.execute("SELECT pii_class AS cls FROM shared.llm_role_config WHERE stage = %s", (PREFIX + name,))
    return c.fetchone()[0]


def test_the_file_is_a_contract_file_with_both_checks():
    sql = open(MIGRATION, encoding="utf-8").read()
    assert "-- migrate:" not in sql                     # contract: no directive line (root ADR 0017)
    assert "CHECK (pii_class IN ('content', 'pii'))" in sql
    assert "allowed_scopes IS NOT NULL AND allowed_scopes = ARRAY['aws_only']::text[]" in sql


def test_registered_and_every_seeded_stage_is_content(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '373'")
    assert cur.fetchone()
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE pii_class <> 'content' AND stage NOT LIKE %s",
                (PREFIX + "%",))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'shared' AND table_name = 'llm_role_config' AND column_name = 'pii_class'")
    nullable, default = cur.fetchone()
    assert nullable == "NO" and default.startswith("'content'")


def test_a_new_row_is_content_and_a_rerun_keeps_a_decision(cur):
    _stage(cur, "new")
    _stage(cur, "personal", pii="pii")
    _run(cur)
    _run(cur)                                              # idempotent: constraints and column are not added twice
    assert _class(cur, "new") == "content"
    assert _class(cur, "personal") == "pii"
    cur.execute("SELECT count(*) AS n FROM pg_constraint WHERE conrelid = 'shared.llm_role_config'::regclass "
                "AND conname IN ('llm_role_config_pii_class_check', 'llm_role_config_pii_scope_check')")
    assert cur.fetchone()[0] == 2


def test_the_class_values_are_checked(cur):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _stage(cur, "other", pii="personal")


@pytest.mark.parametrize("scopes", ["ARRAY['aws_only','vendor_direct']", "ARRAY['vendor_direct']", "NULL"])
def test_a_pii_stage_stays_on_our_aws_accounts(cur, scopes):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _stage(cur, "leaks", scopes=scopes, pii="pii")


def test_opening_a_pii_stage_to_vendor_apis_is_refused(cur):
    """What a later migration like 366 does to every writer fails on a pii row."""
    _stage(cur, "guarded", pii="pii")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_role_config SET allowed_scopes = ARRAY['aws_only','vendor_direct'] "
                    "WHERE stage = %s", (PREFIX + "guarded",))
    cur.execute("ROLLBACK")
    _stage(cur, "content", scopes="ARRAY['aws_only','vendor_direct']")    # a content stage may
    assert _class(cur, "content") == "content"


def test_the_runtime_role_cannot_set_a_class(cur):
    cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_role_config', 'pii_class', 'UPDATE') AS up, "
                "has_column_privilege('cis_app', 'shared.llm_role_config', 'pii_class', 'INSERT') AS ins")
    up, ins = cur.fetchone()
    assert up is False and ins is False


def test_the_decision_path_reads_the_pii_stages(cur):
    """decide() skips TypeSafe for a pii stage (root ADR 0019 d12): its real query sees the pii row, not content."""
    _stage(cur, "personal", pii="pii")
    _stage(cur, "content")

    async def load():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            decide._questions_loaded_at = 0.0
            await decide._load_config(conn)
        finally:
            await conn.close()

    try:
        asyncio.run(load())
        assert decide.stage_is_pii(PREFIX + "personal")
        assert not decide.stage_is_pii(PREFIX + "content") and not decide.stage_is_pii(PREFIX + "missing")
    finally:
        decide._questions_loaded_at = 0.0
        decide._pii_stages = frozenset()
