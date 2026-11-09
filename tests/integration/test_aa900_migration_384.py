"""AA-900 S271 W0 — migration 384 registers prompt version writing_memory_eval v2 in shared.prompt_version, against
real Postgres.

conftest applies every migration, 384 included; a later test may rebuild the table (lessons: never rely on seeded rows
of a shared table), so each test runs 384's statements itself inside one transaction and rolls back. The guard test
proves a version registered earlier with another hash stops the file.
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
M384 = os.path.join(ROOT, "api", "migrations", "384_prompt_version_writing_memory_eval_v2.sql")
SQL = open(M384, encoding="utf-8").read()
# the file's own row (the INSERT): the test never restates the hash
ROWS = {(s, v): (sha, path) for s, v, sha, path in
        re.findall(r"\('(\w+)', '(v\d+)', '([0-9a-f]{64})', '([^']+)', 'migration-384'\)", SQL)}
BODY = SQL.replace("BEGIN;", "", 1).replace("COMMIT;", "")   # run inside the test's transaction


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("DELETE FROM shared.prompt_version WHERE created_by = 'migration-384'")
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_the_file_holds_the_one_version():
    assert list(ROWS) == [("writing_memory_eval", "v2")]
    assert ROWS[("writing_memory_eval", "v2")][1] == "services/writing_memory/eval_judge.py"


def test_registers_the_version_with_its_hash_and_is_idempotent(cur):
    cur.execute(BODY)
    cur.execute(BODY)
    cur.execute("SELECT stage, version, prompt_sha256, source_path FROM shared.prompt_version "
                "WHERE created_by = 'migration-384'")
    got = {(r["stage"], r["version"]): (r["prompt_sha256"], r["source_path"]) for r in cur.fetchall()}
    assert got == ROWS
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '384'")
    assert cur.fetchone()["ok"] == "384"


def test_v1_is_kept(cur):
    cur.execute(BODY)
    cur.execute("SELECT count(*) AS n FROM shared.prompt_version WHERE stage = 'writing_memory_eval' "
                "AND version = 'v1'")
    assert cur.fetchone()["n"] <= 1          # never removed or duplicated (a rebuilt table may hold none)


def test_a_version_registered_with_another_hash_stops_the_file(cur):
    cur.execute("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) "
                "VALUES ('writing_memory_eval', 'v2', repeat('0', 64), 'services/writing_memory/eval_judge.py', "
                "'test')")
    with pytest.raises(psycopg2.errors.RaiseException, match="writing_memory_eval v2"):
        cur.execute(BODY)


def test_header_is_contract_class():
    # rows into an existing table = contract (root ADR 0017 d3): no "-- migrate:" directive line at all
    assert not re.search(r"^--\s*migrate:", SQL, flags=re.M)
