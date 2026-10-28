"""AA-850 — migration 294: Dead Table Registry wave 1, ten dead CIS objects quarantined in schema `retired` (DM-3.9).

conftest replays 294 as committed: the file holds Dev's DRY RUN counts, and its guard accepts an empty table (the
replay's tables are empty) besides the DRY RUN count. CIS 296 (AA-850 step 2) then drops the ten objects, so every
test below rebuilds the pre-294 state from minimal stand-ins (`_rls_fixture_helpers.wave1_standins`: the columns the
tests touch and Dev's outbound FKs) and applies the file to them. Checked:
  * the replay really applied it (conftest swallows migration errors, lesson AA-741): the version registered, schema
    `retired` exists with no ACL entry but the owner's (296's own test checks the drop);
  * from the pre-294 state (Dev's cis_app grants and a PUBLIC grant mirrored) the file moves all ten, strips every
    grant and writes the comment prefix with the source schema;
  * no ACL entry but the owner's on the schema, the relations or their columns; cis_app (the CIS runtime) cannot
    reach a quarantined table (42501), the outbound FKs are still enforced;
  * the file re-runs as a no-op;
  * the guard refuses: an unfilled count (-1), a non-zero live count that differs from the DRY RUN one (an insert into
    a table the DRY RUN found empty; rows in a table the DRY RUN counted 11 for), a view outside the set over a
    wave-1 table, an inbound FK from outside the set; it accepts the exact DRY RUN count;
  * the file's DRY RUN counts are filled in (fails while a -1 placeholder is left: fill them before merge).
Every test works in a transaction that is rolled back.
"""
import os
import re
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import _get_conn  # noqa: E402
from _rls_fixture_helpers import wave1_standins  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "294_aa850_quarantine_wave1.sql")
WAVE1 = {  # name -> (source schema, relkind)
    "atom_decompose_jobs": ("acp_contract", "r"), "s1_from_atom_runs": ("acp_contract", "r"),
    "route_pick": ("acp_contract", "r"), "marketplace_portfolios": ("acp_shared", "r"),
    "tenant_atom_state": ("acp_shared", "r"), "acp_runs": ("shared", "r"),
    "tenant_brand_rule_versions": ("shared", "r"), "tenant_brand_rules_deleted_aa404": ("shared", "r"),
    "v_batch_stats": ("shared", "v"), "v_pipeline_summary": ("shared", "v"),
}
APP_ROLES = ("cis_app", "aa_app_user", "catalog_owner", "booking", "tripplanner", "app_user")

_LEFT_ACL_SQL = """
SELECT c.oid::regclass::text || ':' || CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
  FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE ns.nspname = 'retired' AND a.grantee <> c.relowner
UNION ALL
SELECT c.oid::regclass::text || '.' || att.attname || ':' || pg_get_userbyid(a.grantee)
  FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid JOIN pg_namespace ns ON ns.oid = c.relnamespace
 CROSS JOIN LATERAL aclexplode(att.attacl) a
 WHERE ns.nspname = 'retired' AND att.attnum > 0 AND a.grantee <> c.relowner
UNION ALL
SELECT 'schema:' || CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
  FROM pg_namespace ns CROSS JOIN LATERAL aclexplode(ns.nspacl) a
 WHERE ns.nspname = 'retired' AND a.grantee <> ns.nspowner
"""


def _raw() -> str:
    with open(MIGRATION) as f:
        return f.read()


def _body(sql: str) -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    return "\n".join(line for line in sql.splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


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
              "WHERE k.relname = %s AND n.nspname IN ('retired', %s)", (name, WAVE1[name][0]))
    return sorted(r[0] for r in c.fetchall())


def _pre_294(c):
    """The pre-294 state (in the test's transaction): the ten objects as stand-ins in their source schemas (296
    dropped the real ones), 294 unregistered. 294's guard wants all ten, so a test changes one after this."""
    wave1_standins(c)
    c.execute("DELETE FROM shared.schema_versions WHERE version = '294'")


def _quarantine(c):
    """The pre-294 state with Dev's grants (wave1_grant: cis_app on each table) plus a PUBLIC one, then 294 applied."""
    _pre_294(c)
    for name, (src, _) in WAVE1.items():
        c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {src}.{name} TO cis_app")
    c.execute("GRANT SELECT ON shared.acp_runs TO PUBLIC")
    c.execute("GRANT SELECT (id) ON shared.acp_runs TO aa_app_user")
    c.execute("GRANT USAGE ON SCHEMA retired TO cis_app")
    c.execute("COMMENT ON TABLE acp_contract.route_pick IS 'route picks'")
    c.execute(_body(_raw()))


def _refused(c, sql, needle):
    c.execute("SAVEPOINT aa850")
    with pytest.raises(psycopg2.Error) as ei:
        c.execute(sql)
    c.execute("ROLLBACK TO SAVEPOINT aa850")
    assert needle in str(ei.value), str(ei.value)


def test_replay_applied_294(cur):
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '294'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT to_regnamespace('retired') IS NOT NULL")
    assert cur.fetchone()[0] is True
    cur.execute(_LEFT_ACL_SQL)
    assert cur.fetchall() == []


def test_no_privilege_left_in_retired(cur):
    _quarantine(cur)
    cur.execute(_LEFT_ACL_SQL)
    assert cur.fetchall() == []
    for role in APP_ROLES:
        cur.execute("SELECT to_regrole(%s) IS NOT NULL", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_schema_privilege(%s, 'retired', 'USAGE')", (role,))
        assert cur.fetchone()[0] is False, role
        for name in WAVE1:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE')",
                        (role, f"retired.{name}"))
            assert cur.fetchone()[0] is False, (role, name)


def test_runtime_role_cannot_read_a_quarantined_table(cur):
    _quarantine(cur)
    cur.execute("SET LOCAL ROLE cis_app")
    _refused(cur, "SELECT count(*) FROM retired.acp_runs", "permission denied for schema retired")


def test_outbound_foreign_key_still_enforced(cur):
    _quarantine(cur)
    cur.execute("SAVEPOINT aa850fk")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("INSERT INTO retired.route_pick (route_pick_id, tenant_id, hub_name, route_snapshot, selected_at) "
                    "VALUES (%s, %s, 'zz850', '{}'::jsonb, now())", (str(uuid.uuid4()), str(uuid.uuid4())))
    cur.execute("ROLLBACK TO SAVEPOINT aa850fk")


def test_rerun_is_a_noop(cur):
    _quarantine(cur)
    cur.execute("SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'retired' ORDER BY 1")
    before = cur.fetchall()
    cur.execute(_body(_raw()))                     # the file as committed: every object already in retired
    cur.execute("SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'retired' ORDER BY 1")
    assert cur.fetchall() == before


def test_applies_from_the_pre_294_state(cur):
    _quarantine(cur)
    for name, (src, _) in WAVE1.items():
        assert _where(cur, name) == ["retired"], name
        cur.execute("SELECT obj_description(%s::regclass, 'pg_class')", (f"retired.{name}",))
        comment = cur.fetchone()[0]
        assert re.match(rf"\[AA-850 quarantine \d{{4}}-\d\d-\d\d from {src}; drop not before \d{{4}}-\d\d-\d\d\]",
                        comment), (name, comment)
    cur.execute(_LEFT_ACL_SQL)
    assert cur.fetchall() == []
    cur.execute("SELECT obj_description('retired.route_pick'::regclass, 'pg_class')")
    assert re.fullmatch(r"\[AA-850 quarantine \S+ from acp_contract; drop not before \S+\] route picks",
                        cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '294'")
    assert cur.fetchone()[0] == 1


def test_guard_refuses_an_unfilled_count(cur):
    _pre_294(cur)
    sql = re.sub(r'("name": "acp_runs",\s*"kind": "r",\s*"rows": )-?\d+', r"\g<1>-1", _raw())
    _refused(cur, _body(sql), "has no DRY RUN row count")


def _insert_decompose_job(c):
    c.execute("INSERT INTO acp_contract.atom_decompose_jobs (job_id, tour_ids, status, input_s3_uri, submitted_at) "
              "VALUES ('zz850', '[]'::jsonb, 'failed', 's3://zz850/in', now())")


def _insert_portfolios(c, k):
    for _ in range(k):
        c.execute("INSERT INTO acp_shared.marketplace_portfolios (portfolio_id, tour_ids, status, created_at) "
                  "VALUES (%s, '{}'::uuid[], 'draft', now())", (str(uuid.uuid4()),))


def test_guard_refuses_a_write_to_a_table_counted_empty(cur):
    _pre_294(cur)
    _insert_decompose_job(cur)
    _refused(cur, _body(_raw()), "atom_decompose_jobs has 1 rows, the approved DRY RUN counted 0")


def test_guard_refuses_a_count_that_moved(cur):
    _pre_294(cur)
    _insert_portfolios(cur, 3)                     # the DRY RUN counted 11; 3 is neither 11 nor an empty replay
    _refused(cur, _body(_raw()), "marketplace_portfolios has 3 rows, the approved DRY RUN counted 11")


def test_guard_accepts_the_dry_run_count(cur):
    _pre_294(cur)
    _insert_portfolios(cur, 11)                    # Dev's state at DRY RUN T0
    cur.execute(_body(_raw()))
    assert _where(cur, "marketplace_portfolios") == ["retired"]
    cur.execute("SELECT count(*) FROM retired.marketplace_portfolios")
    assert cur.fetchone()[0] == 11


def test_guard_refuses_a_view_outside_the_set(cur):
    _pre_294(cur)
    cur.execute("CREATE VIEW shared.zz850_v AS SELECT id FROM shared.acp_runs")
    _refused(cur, _body(_raw()), "view(s) shared.zz850_v depend on shared.acp_runs")


def test_guard_refuses_an_inbound_foreign_key(cur):
    _pre_294(cur)
    cur.execute("CREATE TABLE shared.zz850_fk (id int PRIMARY KEY, "
                "route_pick_id uuid REFERENCES acp_contract.route_pick (route_pick_id))")
    _refused(cur, _body(_raw()), "reference acp_contract.route_pick from outside wave 1")


def test_dry_run_counts_filled_in():
    """FILL BEFORE MERGE: Dev's apply is refused while a table's count is -1 (the file's guard). Fill each `rows`
    from the approved DRY RUN (SCRIPT wave1 … rows=<n>), AA-Ecosys docs/audits/2026-10-28-S255-aa850-dead-tables."""
    left = re.findall(r'"name": "(\w+)",\s*"kind": "r",\s*"rows": -1', _raw())
    assert not left, f"DRY RUN row counts not filled in for: {left}"
