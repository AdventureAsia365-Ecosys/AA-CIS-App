"""AA-1047 S272 W1 — migration 387 registers the prompt versions the writing policy registry moves (the sixteen
stages whose prompt source reads the writing policy: t9_write v4, writing_memory_eval v3, the rest v2) in
shared.prompt_version, against real Postgres.

conftest applies every migration, 387 included; a later test may rebuild the table (lessons: never rely on seeded rows
of a shared table), so each test runs 387's statements itself inside one transaction and rolls back. The guard tests
prove a version registered earlier with another hash stops the file.
"""
import os
import re
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
M387 = os.path.join(ROOT, "api", "migrations", "387_prompt_versions_aa1047_policy.sql")
SQL = open(M387, encoding="utf-8").read()
# the file's own rows (the INSERT): the test never restates a hash
ROWS = {(s, v): (sha, path) for s, v, sha, path in
        re.findall(r"\('(\w+)', '(v\d+)', '([0-9a-f]{64})', '([^']+)', 'migration-387'\)", SQL)}
VERSIONS = [("a3_place_card", "v2"), ("a3_place_card_check", "v2"), ("s1_brand_audit", "v2"), ("s1_flag_fix", "v2"),
            ("s1_generate", "v2"), ("s1_judge", "v2"), ("t10_claims", "v2"), ("t10_judge", "v2"),
            ("t2_brand_audit", "v2"), ("t2_flag_fix", "v2"), ("t2_generate", "v2"), ("t2_judge", "v2"),
            ("t9_faq_answer_fit", "v2"), ("t9_repair", "v2"), ("t9_write", "v4"), ("writing_memory_eval", "v3")]
BODY = SQL.replace("BEGIN;", "", 1).replace("COMMIT;", "")   # run inside the test's transaction


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("DELETE FROM shared.prompt_version WHERE created_by = 'migration-387'")
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_the_file_holds_the_sixteen_versions():
    assert sorted(ROWS) == sorted(VERSIONS)
    for a, b in (("s1_brand_audit", "t2_brand_audit"), ("s1_judge", "t2_judge"), ("s1_flag_fix", "t2_flag_fix")):
        assert ROWS[(a, "v2")] == ROWS[(b, "v2")]   # one source, two stages
    assert ROWS[("t9_write", "v4")][1] == "services/acp_content_writing/prompts.py"


def test_registers_each_version_with_its_hash_and_is_idempotent(cur):
    cur.execute(BODY)
    cur.execute(BODY)
    cur.execute("SELECT stage, version, prompt_sha256, source_path FROM shared.prompt_version "
                "WHERE created_by = 'migration-387'")
    got = {(r["stage"], r["version"]): (r["prompt_sha256"], r["source_path"]) for r in cur.fetchall()}
    assert got == ROWS
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '387'")
    assert cur.fetchone()["ok"] == "387"


@pytest.mark.parametrize("stage, version", VERSIONS)
def test_a_version_registered_with_another_hash_stops_the_file(cur, stage, version):
    path = ROWS[(stage, version)][1]
    cur.execute("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) "
                "VALUES (%s, %s, repeat('0', 64), %s, 'test')", (stage, version, path))
    with pytest.raises(psycopg2.errors.RaiseException, match=f"{stage} {version}"):
        cur.execute(BODY)


def test_header_is_contract_class():
    # rows into an existing table = contract (root ADR 0017 d3): no "-- migrate:" directive line at all
    assert not re.search(r"^--\s*migrate:", SQL, flags=re.M)
