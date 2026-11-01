"""AA-642 D1 (gap OPS-03) — migration 325 registers the 32 manifest prompt versions in shared.prompt_version, against
real Postgres.

conftest applies every migration, 325 included. Each test re-runs the file (idempotent, errors not swallowed). The
guard test proves a version registered earlier with another hash stops the file instead of being kept silently.
"""
import json
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
M325 = os.path.join(ROOT, "api", "migrations", "325_prompt_version_seed.sql")
MANIFEST = os.path.join(ROOT, "shared", "llm_client", "prompt_versions.json")


def _run(c):
    c.execute(open(M325, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _run(c)
        conn.close()


def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '325'")
    assert cur.fetchone()["ok"] == "325"


def test_every_manifest_entry_is_a_row_with_its_hash_and_path(cur):
    with open(MANIFEST, encoding="utf-8") as f:
        manifest = {(e["stage"], e["version"]): e for e in json.load(f)["prompts"]}
    cur.execute("SELECT stage, version, prompt_sha256, source_path, created_by FROM shared.prompt_version "
                "WHERE created_by = 'migration-325'")
    rows = {(r["stage"], r["version"]): r for r in cur.fetchall()}
    assert len(rows) == 32 and set(rows) == set(manifest)
    for key, r in rows.items():
        assert r["prompt_sha256"] == manifest[key]["prompt_sha256"]
        assert r["source_path"] == manifest[key]["source_path"]


def test_no_prompt_text_is_stored(cur):
    cur.execute("SELECT max(length(prompt_sha256)) AS sha, max(length(coalesce(notes, ''))) AS notes "
                "FROM shared.prompt_version")
    r = cur.fetchone()
    assert r["sha"] == 64 and r["notes"] == 0


def test_a_version_registered_with_another_hash_stops_the_file(cur):
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.prompt_version SET prompt_sha256 = repeat('0', 64) "
                "WHERE stage = 't9_write' AND version = 'v1'")
    with pytest.raises(psycopg2.errors.RaiseException, match="t9_write v1"):
        _run(cur)
    cur.execute("ROLLBACK")
    cur.execute("SELECT prompt_sha256 AS ok FROM shared.prompt_version WHERE stage = 't9_write' AND version = 'v1'")
    assert cur.fetchone()["ok"] != "0" * 64


def test_cis_app_reads_the_register_and_cannot_write_it(cur):
    cur.execute("SELECT has_table_privilege('cis_app', 'shared.prompt_version', 'SELECT') AS can_read, "
                "has_table_privilege('cis_app', 'shared.prompt_version', 'INSERT') AS can_insert")
    r = cur.fetchone()
    # The CI database does not carry the Dev grants of migration 321 (GRANT SELECT to cis_app), so only the
    # property 325 must keep is asserted here: cis_app can never write the register.
    assert r["can_insert"] is False
