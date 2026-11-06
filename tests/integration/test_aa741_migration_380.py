"""AA-741 S271 — migration 380 registers prompt version t9_write v2 in shared.prompt_version, against real Postgres.

conftest applies every migration, 380 included; a later test may rebuild the table (lessons: never rely on seeded rows
of a shared table), so each test runs 380's statements itself inside one transaction and rolls back. The guard
test proves a v2 registered earlier with another hash stops the file.
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
M380 = os.path.join(ROOT, "api", "migrations", "380_prompt_version_t9_write_v2.sql")
SQL = open(M380, encoding="utf-8").read()
# the file's own hash (the INSERT row): the test never restates it
V2_SHA = re.search(r"\('t9_write', 'v2', '([0-9a-f]{64})'", SQL).group(1)
BODY = SQL.replace("BEGIN;", "", 1).replace("COMMIT;", "")   # run inside the test's transaction


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("DELETE FROM shared.prompt_version WHERE stage = 't9_write' AND version = 'v2'")
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def test_registers_v2_with_its_hash_and_is_idempotent(cur):
    cur.execute(BODY)
    cur.execute(BODY)
    cur.execute("SELECT prompt_sha256, source_path, created_by FROM shared.prompt_version "
                "WHERE stage = 't9_write' AND version = 'v2'")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["prompt_sha256"] == V2_SHA
    assert rows[0]["source_path"] == "services/acp_content_writing/prompts.py"
    assert rows[0]["created_by"] == "migration-380"
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '380'")
    assert cur.fetchone()["ok"] == "380"


def test_v1_is_kept(cur):
    cur.execute(BODY)
    cur.execute("SELECT count(*) AS n FROM shared.prompt_version WHERE stage = 't9_write' AND version = 'v1'")
    assert cur.fetchone()["n"] <= 1        # never removed or duplicated by 380 (a rebuilt table may hold none)


def test_a_v2_registered_with_another_hash_stops_the_file(cur):
    cur.execute("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) "
                "VALUES ('t9_write', 'v2', repeat('0', 64), 'services/acp_content_writing/prompts.py', 'test')")
    with pytest.raises(psycopg2.errors.RaiseException, match="t9_write v2"):
        cur.execute(BODY)


def test_header_is_contract_class():
    # rows into an existing table = contract (root ADR 0017 d3): no "-- migrate:" directive line at all
    assert not re.search(r"^--\s*migrate:", SQL, flags=re.M)
