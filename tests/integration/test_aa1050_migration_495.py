"""AA-1050 (S294 D6) — migration 495 retires the t10_claims route against real Postgres.

conftest applies every migration, 495 included. A test never trusts the applied state: it sets the state the file
expects, replays the file's body (BEGIN / COMMIT stripped) inside the fixture's transaction and checks the result.
Every test rolls back what it wrote. The ledger rows use the real stage key (a route name, not data).
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

M495 = Path(__file__).resolve().parents[2] / "api" / "migrations" / "495_llm_retire_t10_claims.sql"
STAGE = "t10_claims"


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "")


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


def _switches(c) -> dict:
    c.execute("SELECT stage, is_active, disabled_reason AS reason, updated_by FROM shared.llm_role_config")
    return {r["stage"]: (r["is_active"], r["reason"], r["updated_by"]) for r in c.fetchall()}


def _on(c) -> None:
    c.execute("UPDATE shared.llm_role_config SET is_active = true, disabled_reason = NULL WHERE stage = %s", (STAGE,))


def _ledger(c, days_ago: int, *, piece: bool) -> None:
    c.execute("INSERT INTO shared.llm_call_log (stage, role, model, account, quality_signal, created_at, "
              "content_piece_id) VALUES (%s, 'validate', 'gpt-6-luna', 'acc3', '{}'::jsonb, "
              "now() - make_interval(days => %s), %s)",
              (STAGE, days_ago, str(uuid.UUID(int=7)) if piece else None))


def test_495_registered_and_the_route_is_off_with_a_reason(cur):
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '495'")
    assert cur.fetchone() is not None
    active, reason, _by = _switches(cur)[STAGE]
    assert active is False and reason and reason.startswith("Retired (AA-1050")
    assert len(reason) <= 500


def test_495_replay_switches_off_only_t10_claims(cur):
    _on(cur)
    before = _switches(cur)
    _ledger(cur, 2, piece=False)          # a contract-smoke row: no object, allowed
    _ledger(cur, 45, piece=True)          # older than 30 days: history
    cur.execute(_body(M495))
    after = _switches(cur)
    assert after[STAGE][0] is False and after[STAGE][2] == "migration-495-aa1050"
    assert {s: v[0] for s, v in after.items() if s != STAGE} == {s: v[0] for s, v in before.items() if s != STAGE}


def test_495_refuses_object_tied_traffic(cur):
    _on(cur)
    _ledger(cur, 3, piece=True)
    with pytest.raises(psycopg2.errors.RaiseException, match="object-tied ledger traffic"):
        cur.execute(_body(M495))


def test_495_rerun_is_a_no_op_and_a_missing_row_is_not_an_error(cur):
    cur.execute(_body(M495))
    first = _switches(cur)[STAGE]
    cur.execute(_body(M495))
    assert _switches(cur)[STAGE] == first
    cur.execute("DELETE FROM shared.llm_role_config WHERE stage = %s", (STAGE,))
    cur.execute(_body(M495))
    assert STAGE not in _switches(cur)
