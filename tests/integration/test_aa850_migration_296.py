"""AA-850 — migration 296: drop the ten wave-1 objects CIS 294 quarantined in schema `retired` (DM-3.9 step 2).

conftest replays 296 as committed (after 294): the file holds the counts verified on Dev after the 294 apply, and its
guard accepts an empty table (the replay's) besides that count, like 294. The 14-day wait of DM-3.9 is waived by
Nghiệp (S255): the file has no age check, only "294 is registered". Checked:
  * the replay really applied it (conftest swallows migration errors, lesson AA-741): none of the ten exists, neither
    in `retired` nor in its source schema; 294 and 296 registered; schema `retired` stays, empty;
  * the file re-runs as a no-op;
  * from the pre-296 state (stand-ins of the ten in `retired`, `_rls_fixture_helpers.wave1_standins`) it drops all
    ten, with empty tables (a replay) and with the verified counts (11 portfolios, 6 AA-404 backup rows: Dev);
  * the guard refuses: 294 not registered, an unfilled count (-1), a non-zero count that differs from the verified
    one (a write to a table counted empty; 3 rows where 11 were counted);
  * RESTRICT: a view or an inbound FK from outside the set over a quarantined table stops the drop;
  * the file is a destructive migration for DB Migrate (first line), so the runner takes the pre-apply snapshot.
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
from _rls_fixture_helpers import WAVE1_850, wave1_standins  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "296_aa850_drop_wave1.sql")


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


def _existing(c):
    """The wave-1 names that exist anywhere (retired or the source schema), as schema.name."""
    found = []
    for name, (src, _) in WAVE1_850.items():
        for schema in ("retired", src):
            c.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{name}",))
            if c.fetchone()[0]:
                found.append(f"{schema}.{name}")
    return found


def _retired_relations(c):
    c.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
              "WHERE n.nspname = 'retired' ORDER BY 1")
    return [r[0] for r in c.fetchall()]


def _pre_296(c):
    """The pre-296 state (in the test's transaction): the ten objects as stand-ins in `retired`, 296 unregistered."""
    wave1_standins(c, schema="retired")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '296'")


def _refused(c, sql, needle):
    c.execute("SAVEPOINT aa850")
    with pytest.raises(psycopg2.Error) as ei:
        c.execute(sql)
    c.execute("ROLLBACK TO SAVEPOINT aa850")
    assert needle in str(ei.value), str(ei.value)


def _insert_portfolios(c, k):
    for _ in range(k):
        c.execute("INSERT INTO retired.marketplace_portfolios (portfolio_id, status) VALUES (%s, 'draft')",
                  (str(uuid.uuid4()),))


def _insert_aa404_backup(c, k):
    for _ in range(k):
        c.execute("INSERT INTO retired.tenant_brand_rules_deleted_aa404 (tenant_id) VALUES (%s)", (str(uuid.uuid4()),))


def test_replay_applied_296(cur):
    assert _existing(cur) == []
    cur.execute("SELECT version FROM shared.schema_versions WHERE version IN ('294', '296') ORDER BY 1")
    assert [r[0] for r in cur.fetchall()] == ["294", "296"]
    cur.execute("SELECT to_regnamespace('retired') IS NOT NULL")
    assert cur.fetchone()[0] is True
    assert _retired_relations(cur) == []


def test_rerun_is_a_noop(cur):
    cur.execute(_body(_raw()))
    assert _existing(cur) == []
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '296'")
    assert cur.fetchone()[0] == 1


def test_drops_all_ten_from_empty_tables(cur):
    _pre_296(cur)
    assert sorted(_existing(cur)) == sorted(f"retired.{n}" for n in WAVE1_850)
    cur.execute(_body(_raw()))
    assert _existing(cur) == []
    assert _retired_relations(cur) == []
    cur.execute("SELECT description FROM shared.schema_versions WHERE version = '296'")
    assert "waived" in cur.fetchone()[0]


def test_drops_with_the_verified_counts(cur):
    _pre_296(cur)
    _insert_portfolios(cur, 11)                     # Dev's state after the 294 apply
    _insert_aa404_backup(cur, 6)
    cur.execute(_body(_raw()))
    assert _existing(cur) == []


def test_guard_refuses_without_294(cur):
    _pre_296(cur)
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '294'")
    _refused(cur, _body(_raw()), "the quarantine 294 is not registered")


def test_guard_refuses_an_unfilled_count(cur):
    _pre_296(cur)
    sql = re.sub(r'("name": "acp_runs",\s*"kind": "r",\s*"rows": )-?\d+', r"\g<1>-1", _raw())
    _refused(cur, _body(sql), "retired.acp_runs has no verified row count")


def test_guard_refuses_a_write_to_a_table_counted_empty(cur):
    _pre_296(cur)
    cur.execute("INSERT INTO retired.atom_decompose_jobs (job_id, status) VALUES ('zz850', 'failed')")
    _refused(cur, _body(_raw()), "retired.atom_decompose_jobs has 1 rows, the verified count is 0")


def test_guard_refuses_a_count_that_moved(cur):
    _pre_296(cur)
    _insert_portfolios(cur, 3)                      # 11 were counted; 3 is neither 11 nor an empty replay
    _refused(cur, _body(_raw()), "retired.marketplace_portfolios has 3 rows, the verified count is 11")


def test_restrict_stops_on_a_view_outside_the_set(cur):
    _pre_296(cur)
    cur.execute("CREATE VIEW shared.zz850_v AS SELECT id FROM retired.acp_runs")
    _refused(cur, _body(_raw()), "because other objects depend on it")


def test_restrict_stops_on_an_inbound_foreign_key(cur):
    _pre_296(cur)
    cur.execute("CREATE TABLE shared.zz850_fk (id int PRIMARY KEY, "
                "route_pick_id uuid REFERENCES retired.route_pick (route_pick_id))")
    _refused(cur, _body(_raw()), "because other objects depend on it")


def test_file_contract():
    """Destructive header first (DB Migrate takes the pre-apply snapshot), counts filled in, no CASCADE, the 294
    check kept and the 14-day check replaced by the recorded waiver."""
    raw = _raw()
    assert raw.splitlines()[0] == "-- migrate: destructive"
    left = re.findall(r'"name": "(\w+)",\s*"kind": "r",\s*"rows": -1', raw)
    assert not left, f"verified row counts not filled in for: {left}"
    code = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    assert not re.search(r"\bCASCADE\b", code, re.I)
    assert "WHERE version = '294'" in code
    assert "interval" not in code and "waived" in raw
    drops = re.findall(r"^DROP (?:VIEW|TABLE) +IF EXISTS retired\.(\w+);$", raw, re.M)
    assert sorted(drops) == sorted(WAVE1_850)
