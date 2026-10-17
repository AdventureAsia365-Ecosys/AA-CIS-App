"""AA-791 — migration 231 (acp_shared.content_piece_label) against real Postgres.

Lesson S236 (test_aa679 / test_aa712): conftest applies migrations as a SUPERUSER, which owns
everything and skips every privilege check; Dev RDS runs DB Migrate as a plain role. The module
fixture undoes the superuser-applied 231 and applies it again as a NOSUPERUSER NOBYPASSRLS runner
with only the privileges the file needs (CREATE on acp_shared, REFERENCES on content_piece, ownership of
schema_versions as on Dev) — twice (idempotent), errors NOT swallowed.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

RUNNER = "zz791_rds_runner"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "231_content_piece_label.sql")


def _connect(user=DB_USER):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=DB_PASS)


def _drop_runner(a):
    a.execute(f"SELECT 1 FROM pg_roles WHERE rolname = '{RUNNER}'")
    if a.fetchone():
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER; DROP OWNED BY {RUNNER}; DROP ROLE {RUNNER}")


@pytest.fixture(scope="module", autouse=True)
def rds_like_231():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    a.execute("DROP TABLE IF EXISTS acp_shared.content_piece_label; "
              "DELETE FROM shared.schema_versions WHERE version = '231'")
    _drop_runner(a)
    a.execute(f"CREATE ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS INHERIT", (DB_PASS,))
    a.execute(f"GRANT USAGE ON SCHEMA shared TO {RUNNER}")
    a.execute(f"GRANT USAGE, CREATE ON SCHEMA acp_shared TO {RUNNER}")
    a.execute(f"GRANT REFERENCES ON acp_shared.content_piece TO {RUNNER}")
    # Dev: the runner (the master) owns the registry. Since CIS 243 the key guard lets only members of the
    # registry's owner insert a CIS key, so a plain INSERT grant is no longer enough (AA-852).
    a.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        sql = open(MIGRATION, encoding="utf-8").read()
        runner.cursor().execute(sql)  # raises on any error
        runner.cursor().execute(sql)  # idempotent re-run
    finally:
        runner.close()
    try:
        yield
    finally:
        # hand the table back to the test user so later modules (and re-runs) see the usual owner
        _drop_runner(a)
        admin.close()


@pytest.fixture
def cur():
    conn = _connect()
    conn.autocommit = True
    c = conn.cursor()
    yield c
    conn.close()


@pytest.fixture
def piece(cur):
    tid = uuid.uuid4()
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, 'L791', %s, 3)",
                (str(tid), f"aa791-{tid.hex[:8]}"))
    cur.execute("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel) "
                "VALUES (%s, 'a791', 'linkedin') RETURNING request_id", (str(tid),))
    req = cur.fetchone()[0]
    cur.execute("INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, "
                "content_text, status) VALUES (%s, %s, 1, 'A piece.', 'approved') RETURNING piece_id",
                (str(tid), req))
    pid = cur.fetchone()[0]
    yield pid
    cur.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = %s", (str(tid),))  # cascades
    cur.execute("DELETE FROM shared.tenants WHERE tenant_id = %s", (str(tid),))


_UPSERT = ("INSERT INTO acp_shared.content_piece_label (piece_id, labelled_by, label, defects, reason) "
           "VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT (piece_id, labelled_by) DO UPDATE SET "
           "label = EXCLUDED.label, defects = EXCLUDED.defects, reason = EXCLUDED.reason, labelled_at = now()")


def test_231_registered_and_owned_by_the_runner(cur):
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '231'")
    assert cur.fetchone(), "migration 231 did not register"
    cur.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'acp_shared' "
                "AND tablename = 'content_piece_label'")
    assert cur.fetchone()[0] == RUNNER


def test_one_label_per_piece_and_labeller(cur, piece):
    cur.execute(_UPSERT, (piece, "admin:a", "light", '{"fact": "light"}', "one invented time"))
    cur.execute(_UPSERT, (piece, "admin:a", "heavy", '{"fact": "heavy"}', "changed my mind"))
    cur.execute(_UPSERT, (piece, "admin:b", "as_is", "{}", None))
    cur.execute("SELECT labelled_by, label, defects->>'fact' FROM acp_shared.content_piece_label "
                "WHERE piece_id = %s ORDER BY labelled_by", (piece,))
    assert cur.fetchall() == [("admin:a", "heavy", "heavy"), ("admin:b", "as_is", None)]


@pytest.mark.parametrize("label, defects, minutes, by", [
    ("great", "{}", None, "admin:a"),        # not one of the four verdicts
    ("light", "[]", None, "admin:a"),        # defects must be an object
    ("light", "{}", 601, "admin:a"),         # minutes out of range
    ("light", "{}", None, "   "),            # blank labeller
])
def test_checks_refuse_bad_rows(cur, piece, label, defects, minutes, by):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO acp_shared.content_piece_label (piece_id, labelled_by, label, defects, "
                    "minutes_to_fix) VALUES (%s, %s, %s, %s::jsonb, %s)", (piece, by, label, defects, minutes))


def test_unknown_piece_is_refused_and_delete_cascades(cur, piece):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute(_UPSERT, (str(uuid.uuid4()), "admin:a", "light", "{}", None))
    cur.execute(_UPSERT, (piece, "admin:a", "reject", "{}", None))
    cur.execute("DELETE FROM acp_shared.content_piece WHERE piece_id = %s", (piece,))
    cur.execute("SELECT count(*) FROM acp_shared.content_piece_label WHERE piece_id = %s", (piece,))
    assert cur.fetchone()[0] == 0


def test_tenant_role_has_no_access(cur):
    cur.execute("SELECT has_table_privilege('aa_app_user', 'acp_shared.content_piece_label', 'SELECT'), "
                "has_table_privilege('aa_app_user', 'acp_shared.content_piece_label', 'INSERT')")
    assert cur.fetchone() == (False, False)
