"""AA-923 WM-34 — migration 316: quarantine shared.pipeline_lessons and acp_shared.acp_output_rules in `retired`.

conftest replays 316 as committed: the CI database has pipeline_lessons (CIS 049) and no acp_output_rules (no repo
migration creates it; it is live-only on Dev), and the file must apply there as committed. Checked:
  * the replay applied it: pipeline_lessons sits in `retired` with no grant, nothing named acp_output_rules is left
    in acp_shared, 316 registered; the file re-runs as a no-op;
  * from the Dev shape (both tables in their source schemas, cis_app holding DELETE / INSERT / SELECT / UPDATE, an
    owned sequence) it moves both, tags the comments with the way back and leaves no privilege to any app role;
  * the guard refuses a view over one, an inbound FK from outside, and an output rule of a non-platform tenant;
    platform and NULL-tenant rules pass.
Every test works in a transaction that is rolled back.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import _get_conn  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402

PLATFORM = "00000000-0000-0000-0000-000000000001"
APP_ROLES = ("cis_app", "aa_app_user", "catalog_owner")


@pytest.fixture
def cur():
    conn = _get_conn()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _where(c, name):
    c.execute("SELECT n.nspname FROM pg_class k JOIN pg_namespace n ON n.oid = k.relnamespace "
              "WHERE k.relname = %s AND k.relkind = 'r' AND n.nspname IN ('retired', 'shared', 'acp_shared')", (name,))
    return sorted(r[0] for r in c.fetchall())


def _privileged_roles(c, table):
    out = []
    for role in APP_ROLES:
        c.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,))
        if not c.fetchone()[0]:
            continue
        c.execute("SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')",
                  (role, table))
        if c.fetchone()[0]:
            out.append(role)
    return out


def _dev_shape(c):
    """Both tables back in their source schemas as on Dev (the output rules with the live columns that matter here),
    granted to cis_app; 316 unregistered."""
    c.execute("SELECT to_regclass('retired.pipeline_lessons') IS NOT NULL")
    if c.fetchone()[0]:
        c.execute("ALTER TABLE retired.pipeline_lessons SET SCHEMA shared")
    c.execute("CREATE TABLE IF NOT EXISTS acp_shared.acp_output_rules (rule_id uuid PRIMARY KEY, "
              "tenant_id varchar(50), rule_type varchar(20) NOT NULL DEFAULT 'flag', is_active boolean)")
    for t in ("shared.pipeline_lessons", "acp_shared.acp_output_rules"):
        c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO cis_app")
    c.execute("GRANT USAGE ON SEQUENCE shared.pipeline_lessons_id_seq TO cis_app")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '316'")


def _refused(c, needle):
    c.execute("SAVEPOINT aa923")
    with pytest.raises(psycopg2.Error) as ei:
        c.execute(migration_body(316))
    c.execute("ROLLBACK TO SAVEPOINT aa923")
    assert needle in str(ei.value)


def test_replay_applied_316(cur):
    assert _where(cur, "pipeline_lessons") == ["retired"]
    assert _where(cur, "acp_output_rules") == []
    assert _privileged_roles(cur, "retired.pipeline_lessons") == []
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '316'")
    assert cur.fetchone()[0] == 1
    cur.execute(migration_body(316))                                   # re-run: a no-op
    assert _where(cur, "pipeline_lessons") == ["retired"]


def test_moves_both_from_the_dev_shape_and_revokes_every_grant(cur):
    _dev_shape(cur)
    cur.execute("INSERT INTO acp_shared.acp_output_rules (rule_id, tenant_id) VALUES (gen_random_uuid(), NULL), "
                "(gen_random_uuid(), %s)", (PLATFORM,))
    assert _privileged_roles(cur, "acp_shared.acp_output_rules") == ["cis_app"]
    cur.execute(migration_body(316))
    assert _where(cur, "pipeline_lessons") == ["retired"] and _where(cur, "acp_output_rules") == ["retired"]
    for t in ("retired.pipeline_lessons", "retired.acp_output_rules"):
        assert _privileged_roles(cur, t) == []
        cur.execute("SELECT obj_description(%s::regclass, 'pg_class')", (t,))
        assert cur.fetchone()[0].startswith("[AA-923 quarantine ")
    cur.execute("SELECT has_sequence_privilege('cis_app', 'retired.pipeline_lessons_id_seq', 'USAGE')")
    assert cur.fetchone()[0] is False
    cur.execute("SELECT count(*) FROM retired.acp_output_rules")
    assert cur.fetchone()[0] == 2                                        # no row changes


def test_guard_refuses_a_view_an_inbound_fk_and_a_tenant_rule(cur):
    _dev_shape(cur)
    cur.execute("CREATE VIEW shared.zz923_v AS SELECT id FROM shared.pipeline_lessons")
    _refused(cur, "view(s)")
    cur.execute("DROP VIEW shared.zz923_v")
    cur.execute("CREATE TABLE shared.zz923_child (rule_id uuid REFERENCES acp_shared.acp_output_rules (rule_id))")
    _refused(cur, "foreign key(s)")
    cur.execute("DROP TABLE shared.zz923_child")
    cur.execute("INSERT INTO acp_shared.acp_output_rules (rule_id, tenant_id) VALUES (gen_random_uuid(), %s)",
                ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",))
    _refused(cur, "non-platform tenant")
