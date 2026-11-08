"""AA-1024 (S273 nightly random order, seed 92395): every integration module starts on a fresh copy of the replay.

conftest replays the migrations once, freezes the result as REPLAY_TEMPLATE_DB and, before each module, replaces DB_NAME
with a copy of it (_fresh_db_per_module). Before this, test_aa712_migration_226 dropped the catalog and geo schemas and
x1_244_undo revoked cis_app's grants for every module that ran after them, so the result hung on the file order.

This module checks the mechanism itself: the frozen template, and that a reset undoes what a module leaves behind
(a dropped schema, a revoked grant, an extra table, a changed row) while the database owner and ACL stay as replayed.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import conftest  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

pytestmark = pytest.mark.skipif(not conftest._DB_RESET, reason="CIS_TEST_DB_RESET=off: one shared database")


def _connect():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    return conn


def _state(cur):
    cur.execute("SELECT to_regnamespace('catalog') IS NOT NULL AS ok")
    catalog = cur.fetchone()[0]
    cur.execute("SELECT to_regclass('shared.zz1024_leftover') IS NOT NULL AS ok")
    leftover = cur.fetchone()[0]
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions")
    versions = cur.fetchone()[0]
    cur.execute("SELECT has_schema_privilege('app_user', 'shared', 'USAGE') AS ok")
    app_user_usage = cur.fetchone()[0]
    return {"catalog": catalog, "leftover": leftover, "versions": versions, "app_user_usage": app_user_usage}


def test_the_replay_is_frozen_as_a_template_nobody_connects_to():
    admin = conftest._admin_conn()
    try:
        cur = admin.cursor()
        cur.execute("SELECT datallowconn AS ok FROM pg_database WHERE datname = %s", (conftest.REPLAY_TEMPLATE_DB,))
        row = cur.fetchone()
        assert row is not None, f"template {conftest.REPLAY_TEMPLATE_DB} missing"
        assert row[0] is False
        assert conftest._TEMPLATE_ACL["owner"]
    finally:
        admin.close()


def test_a_reset_undoes_what_a_module_leaves_behind():
    conn = _connect()
    cur = conn.cursor()
    before = _state(cur)
    assert before["catalog"] and not before["leftover"] and before["versions"] > 0 and before["app_user_usage"]
    admin = conftest._admin_conn()
    acl_before = conftest._database_acl(admin.cursor(), DB_NAME)
    # what the S273 polluters did: a dropped schema, a revoked grant, a table and a registry row left behind
    cur.execute("DROP SCHEMA catalog CASCADE")
    cur.execute("REVOKE USAGE ON SCHEMA shared FROM app_user")
    cur.execute("CREATE TABLE shared.zz1024_leftover (id int)")
    cur.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                "VALUES ('zz1024', now(), 'zz1024 leftover')")
    assert _state(cur) == {"catalog": False, "leftover": True, "versions": before["versions"] + 1,
                           "app_user_usage": False}

    conftest.reset_db_from_template()           # closes every connection to DB_NAME, this one too
    with pytest.raises(psycopg2.Error):
        cur.execute("SELECT 1 AS ok")
    conn.close()

    conn = _connect()
    try:
        assert _state(conn.cursor()) == before
        assert conftest._database_acl(admin.cursor(), DB_NAME) == acl_before
    finally:
        conn.close()
        admin.close()
