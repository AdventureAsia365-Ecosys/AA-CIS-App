"""AA-1039 follow-up (tech lead decision S276) — migration 395 against real Postgres: the agency opt-in
``acp_shared.tour_retirement_policy.auto_draft`` (boolean NOT NULL DEFAULT false) and the CHECK that keeps the platform
row (tenant_id NULL) from turning automatic drafts on for every agency.

conftest applies every migration, 395 included. Each test works inside one transaction that rolls back; the re-run
test applies the file's body again (it is idempotent). Invented ids only; imports no code from the code PR.
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
M395 = ROOT / "api" / "migrations" / "395_tour_retirement_auto_draft.sql"


def _body() -> str:
    return M395.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def test_395_registered_and_column_shape(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '395'")
    assert cur.fetchone()["ok"] == 1
    cur.execute("SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'acp_shared' AND table_name = 'tour_retirement_policy' "
                "AND column_name = 'auto_draft'")
    col = cur.fetchone()
    assert col["data_type"] == "boolean" and col["is_nullable"] == "NO" and col["column_default"] == "false"
    cur.execute("SELECT 1 AS ok FROM pg_constraint WHERE conname = 'tour_retirement_policy_auto_draft_scope_check'")
    assert cur.fetchone()["ok"] == 1


def test_a_new_agency_row_is_not_opted_in_and_can_opt_in(cur):
    tenant = str(uuid.uuid4())
    cur.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by) "
                "VALUES (%s, 5, 'test') RETURNING auto_draft", (tenant,))
    assert cur.fetchone()["auto_draft"] is False
    cur.execute("UPDATE acp_shared.tour_retirement_policy SET auto_draft = true WHERE tenant_id = %s "
                "RETURNING auto_draft", (tenant,))
    assert cur.fetchone()["auto_draft"] is True


def test_the_platform_row_cannot_opt_in(cur):
    cur.execute("DELETE FROM acp_shared.tour_retirement_policy WHERE tenant_id IS NULL")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by, auto_draft) "
                    "VALUES (NULL, 7, 'test', true)")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    cur.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by) "
                "VALUES (NULL, 10, 'test') RETURNING auto_draft")
    assert cur.fetchone()["auto_draft"] is False


def test_the_file_is_rerunnable_and_its_guards_pass_with_rows_present(cur):
    tenant = str(uuid.uuid4())
    cur.execute("DELETE FROM acp_shared.tour_retirement_policy WHERE tenant_id IS NULL")
    cur.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by) "
                "VALUES (NULL, 9, 'test'), (%s, 4, 'test')", (tenant,))
    cur.execute(_body())                     # the platform row exists: the guard probes the CHECK with an UPDATE
    cur.execute("SELECT count(*) AS n FROM acp_shared.tour_retirement_policy WHERE auto_draft")
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT notice_days FROM acp_shared.tour_retirement_policy WHERE tenant_id IS NULL")
    assert cur.fetchone()["notice_days"] == 9


def test_the_first_apply_guard_refuses_an_opted_in_row_and_a_rerun_keeps_opt_ins(cur):
    tenant = str(uuid.uuid4())
    cur.execute("INSERT INTO acp_shared.tour_retirement_policy (tenant_id, notice_days, updated_by, auto_draft) "
                "VALUES (%s, 4, 'test', true)", (tenant,))
    cur.execute(_body())                     # 395 registered: a re-run keeps the agency's opt-in
    cur.execute("SELECT auto_draft FROM acp_shared.tour_retirement_policy WHERE tenant_id = %s", (tenant,))
    assert cur.fetchone()["auto_draft"] is True
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '395'")
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException, match="opted in"):
        cur.execute(_body())                 # a first apply never finds an opted-in row
    cur.execute("ROLLBACK TO SAVEPOINT s")
