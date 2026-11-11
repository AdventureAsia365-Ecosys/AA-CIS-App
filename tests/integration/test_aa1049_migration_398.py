"""AA-1049 (tech lead decision S277) — migration 398 against real Postgres: published_tours.master_status_reason
accepts 'policy_hold' (a Master held for the elephant-riding policy review) next to the four CIS 272 values.

conftest applies every migration, 398 included. Each test works inside one transaction that rolls back; the re-run
test applies the file's body again (it is idempotent). Invented ids and names only; imports no code from the code PR.
"""
import os
import sys
import uuid
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
M398 = ROOT / "api" / "migrations" / "398_master_status_reason_policy_hold.sql"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
REASONS = ("staff_deactivated", "staff_trashed", "source_deleted", "superseded", "policy_hold")


def _body() -> str:
    return M398.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("BEGIN")
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        conn.close()


def _master(cur) -> str:
    tour_id = str(uuid.uuid4())
    cur.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country) "
                "VALUES (%s, %s, %s, 'Quillmere Ridge 3D', 'Vietnam')", (tour_id, MASTER, BATCH_ID))
    cur.execute("INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name) "
                "VALUES (%s, %s, %s, 'Quillmere Ridge')", (tour_id, str(uuid.uuid4()), MASTER))
    return tour_id


def test_398_registered_and_the_constraint_validated(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '398'")
    assert cur.fetchone()["ok"] == 1
    cur.execute("SELECT convalidated AS ok, pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'gold_aa_internal.published_tours'::regclass "
                "AND conname = 'published_tours_master_status_reason_check'")
    row = cur.fetchone()
    assert row["ok"] is True and "policy_hold" in row["def"]


@pytest.mark.parametrize("reason", REASONS)
def test_every_reason_is_accepted(cur, reason):
    tour_id = _master(cur)
    cur.execute("UPDATE gold_aa_internal.published_tours SET master_status = 'inactive', master_status_reason = %s "
                "WHERE tour_id = %s RETURNING master_status_reason AS r", (reason, tour_id))
    assert cur.fetchone()["r"] == reason


def test_an_unknown_reason_is_refused(cur):
    tour_id = _master(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE gold_aa_internal.published_tours SET master_status_reason = 'elephant' "
                    "WHERE tour_id = %s", (tour_id,))
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_the_file_is_rerunnable_and_keeps_rows(cur):
    tour_id = _master(cur)
    cur.execute("UPDATE gold_aa_internal.published_tours SET master_status = 'inactive', "
                "master_status_reason = 'policy_hold' WHERE tour_id = %s", (tour_id,))
    cur.execute(_body())
    cur.execute("SELECT master_status::text AS s, master_status_reason AS r FROM gold_aa_internal.published_tours "
                "WHERE tour_id = %s", (tour_id,))
    row = cur.fetchone()
    assert (row["s"], row["r"]) == ("inactive", "policy_hold")
