"""AA-1143 (decision G3) — migrations 455 and 456: one budget per LLM connection as rows (the total, one share per
app, CIS takes the remainder), the seed from today's values, and TripPlanner's v2 cap view.

455 is replayed by conftest like every plain file (the table is empty there). 456 needs the catalog recipe (245's
owners) and conftest replays only its settings row (unregistered): this module builds on test_aa860_migration_246's
module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), writes the state the seed reads
(invented connections with a CIS monthly budget, an approved provider budget value), then applies 456 twice as
db_migrator under the live-owner switch (errors NOT swallowed). Teardown removes the rows (replica mode: both tables
are append-only), the view, the grant, the setting key and the registration, so later modules see the replay state.

  * 455: the rules of the table (append-only, no cis row, a share needs a total, shares never above the total at
    commit, the remainder written to monthly_budget_usd at commit, the current view);
  * 456 seed: total = provider budget else CIS budget; crm = provider - CIS when provider > CIS; the CIS cap unchanged
    when provider >= CIS, the provider budget when it is lower or CIS had none; a key naming no connection ignored;
    a re-run seeds nothing more;
  * 456 view (as tripplanner): its columns, owner, barrier and sole consumer; the remainder, the pool and the share
    rules; the setting row.
Invented connection keys, stages and amounts only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M455 = os.path.join(HERE, "..", "..", "api", "migrations", "455_aa1143_connection_budget_rows.sql")
M456 = os.path.join(HERE, "..", "..", "api", "migrations", "456_aa1143_connection_budget_seed_and_cap_v2.sql")
VIEW = "catalog.v_llm_connection_cap_v2"
COLUMNS = ("connection_key", "app", "total_usd", "share_usd", "in_cis_pool", "month_start", "month_spent_usd",
           "month_calls", "cis_pool_spent_usd", "alert_pct", "hard_stop", "over_cap", "stops")
PROVIDER_KEY = "cis.spend.connection_provider_budget_usd"
SWITCH_KEY = "tripplanner.spend.connection_share_enforce"
# key: CIS monthly budget before the seed; the provider budget value names the same keys plus one stray.
CONNECTIONS = {"zz-split": 95, "zz-even": 25, "zz-nocis": None, "zz-lower": 40, "zz-cisonly": 12}
PROVIDER = {"zz-split": 100, "zz-even": 30, "zz-nocis": 20, "zz-lower": 30, "zz-stray": 7}


def _insert_connection(a, key, budget):
    a.execute("""INSERT INTO shared.llm_provider_connection
                     (connection_key, kind, auth_mode, label, data_scope, enabled, monthly_budget_usd, updated_by)
                 VALUES (%s, 'mock', 'none', %s, 'aws_only', true, %s, 'test')""", (key, key, budget))


def _cleanup(a):
    a.execute("BEGIN")
    a.execute("SET LOCAL session_replication_role = replica")
    a.execute("DELETE FROM shared.llm_connection_budget WHERE connection_key LIKE 'zz-%%'")
    a.execute("DELETE FROM shared.setting_value WHERE key = %s AND proposed_by = 'admin:zz-seed'", (PROVIDER_KEY,))
    a.execute("DELETE FROM shared.llm_call_log WHERE stage = 'adhoc_zz_budget'")
    a.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key LIKE 'zz-%%'")
    a.execute("DELETE FROM shared.setting_definition WHERE key = %s", (SWITCH_KEY,))
    a.execute("COMMIT")


# The column grants 456 repeats (323 / 347 / 452 made them on Dev; the replay skips those files).
REPEATED = {"llm_app_profile": ("app",), "llm_call_log": ("account", "app", "cost_usd", "created_at"),
            "llm_connection_cost_policy": ("connection_key", "alert_pct", "hard_stop")}
_added: list = []


def _missing_grants(a):
    out = []
    for table, cols in REPEATED.items():
        for col in cols:
            a.execute("SELECT has_column_privilege('catalog_owner', %s, %s, 'SELECT') AS ok", (f"shared.{table}", col))
            if not a.fetchone()[0]:
                out.append((table, col))
    return out


def undo_456(a):
    for table, col in _added:
        a.execute(f"REVOKE SELECT ({col}) ON shared.{table} FROM catalog_owner")
    _added.clear()
    a.execute(f"DROP VIEW IF EXISTS {VIEW}")
    a.execute("REVOKE SELECT ON shared.llm_connection_budget_current FROM catalog_owner")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '456'")
    _cleanup(a)


@pytest.fixture(scope="module", autouse=True)
def applied_456(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_456(a)
        a.execute("SELECT count(*) AS n FROM shared.llm_connection_budget")
        if a.fetchone()[0]:
            pytest.skip("shared.llm_connection_budget is not empty in this database: 456 would not seed")
        for key, budget in CONNECTIONS.items():
            _insert_connection(a, key, budget)
        a.execute("BEGIN")
        a.execute("SET LOCAL session_replication_role = replica")
        a.execute("""INSERT INTO shared.setting_value
                         (key, value, version, status, effective_from, proposed_by, proposed_at, reason,
                          self_approved, decided_by, decided_at)
                     VALUES (%s, %s::jsonb, 9001, 'approved', now() - interval '1 minute', 'admin:zz-seed',
                             now() - interval '2 minutes', 'zz test: the provider budgets of the invented connections',
                             true, 'admin:zz-seed', now() - interval '1 minute')""",
                  (PROVIDER_KEY, json.dumps(PROVIDER)))
        a.execute("COMMIT")
        _added.extend(_missing_grants(a))
        t323._apply(M455)                                    # replayed by conftest already: a no-op re-run
        t323._apply(M456)
        t323._apply(M456)                                    # idempotent re-run: the table is no longer empty
        yield
    finally:
        undo_456(a)
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _rows(cur, sql, *args):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _current(cur):
    return {(r["connection_key"], r["app"]): float(r["budget_usd"]) for r in _rows(
        cur, "SELECT connection_key, app, budget_usd FROM shared.llm_connection_budget_current "
             "WHERE connection_key LIKE 'zz-%%'")}


def _cis_cap(cur, key):
    cur.execute("SELECT monthly_budget_usd AS usd FROM shared.llm_provider_connection WHERE connection_key = %s",
                (key,))
    v = cur.fetchone()[0]
    return None if v is None else float(v)


def _add(cur, key, app, usd, version, reason="zz test: a change of at least twenty characters"):
    cur.execute("""INSERT INTO shared.llm_connection_budget (connection_key, app, budget_usd, version, reason, actor)
                   VALUES (%s, %s, %s, %s, %s, 'admin:zz')""", (key, app, usd, version, reason))


# ── 456: the seed ────────────────────────────────────────────────────────────────────────────────────────────────

def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version IN ('455', '456')")
    assert cur.fetchone()[0] == 2


def test_seed_rows_follow_todays_values(cur):
    assert _current(cur) == {
        ("zz-split", None): 100.0, ("zz-split", "crm"): 5.0,      # provider 100, CIS 95: the CRM had 5
        ("zz-even", None): 30.0, ("zz-even", "crm"): 5.0,
        ("zz-nocis", None): 20.0,                                 # no CIS budget: the provider budget bounds CIS
        ("zz-lower", None): 30.0,                                 # provider below CIS: no CRM share
        ("zz-cisonly", None): 12.0,                               # no provider budget: the CIS budget is the total
    }


def test_seed_keeps_the_cis_cap_or_lowers_it_to_the_provider_budget(cur):
    assert {k: _cis_cap(cur, k) for k in CONNECTIONS} == {
        "zz-split": 95.0, "zz-even": 25.0, "zz-nocis": 20.0, "zz-lower": 30.0, "zz-cisonly": 12.0}


def test_seed_rows_name_the_migration(cur):
    rows = _rows(cur, "SELECT DISTINCT actor, version FROM shared.llm_connection_budget "
                      "WHERE connection_key LIKE 'zz-%%'")
    assert rows == [{"actor": "migration:456", "version": 1}]


# ── 455: the rules ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_share_change_moves_the_cis_remainder_at_commit(cur):
    _add(cur, "zz-split", "crm", 8, 2)
    assert _cis_cap(cur, "zz-split") == 92.0
    assert _current(cur)[("zz-split", "crm")] == 8.0


def test_a_tripplanner_share_and_a_removed_share(cur):
    _add(cur, "zz-split", "tripplanner", 10, 1)
    _add(cur, "zz-split", "crm", None, 2)                     # the CRM share removed
    assert _cis_cap(cur, "zz-split") == 90.0
    assert ("zz-split", "crm") not in _current(cur)


def test_one_statement_lowers_the_total_and_a_share_together(cur):
    cur.execute("""INSERT INTO shared.llm_connection_budget (connection_key, app, budget_usd, version, reason, actor)
                   VALUES ('zz-split', NULL, 3, 2, 'zz test: a change of at least twenty characters', 'admin:zz'),
                          ('zz-split', 'crm', 1, 2, 'zz test: a change of at least twenty characters', 'admin:zz')""")
    assert _cis_cap(cur, "zz-split") == 2.0


def test_shares_above_the_total_are_refused(cur):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation, match="add up to more than the total"):
        _add(cur, "zz-even", "tripplanner", 26, 1)           # 26 + the CRM's 5 > 30


def test_lowering_the_total_under_the_shares_is_refused(cur):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation, match="add up to more than the total"):
        _add(cur, "zz-split", None, 4, 2)                     # the CRM holds 5


def test_cis_never_has_a_row_and_a_total_is_never_removed(cur):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("SAVEPOINT s")
        _add(cur, "zz-split", "cis", 10, 1)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _add(cur, "zz-split", None, None, 2)


def test_a_share_needs_a_total(cur):
    import psycopg2

    _insert_connection(cur, "zz-bare", None)
    with pytest.raises(psycopg2.errors.CheckViolation, match="needs the connection's total"):
        _add(cur, "zz-bare", "tripplanner", 1, 1)


def test_rows_are_append_only(cur):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation, match="append-only"):
        cur.execute("SAVEPOINT s")
        cur.execute("UPDATE shared.llm_connection_budget SET budget_usd = 1 WHERE connection_key = 'zz-split'")
    cur.execute("ROLLBACK TO SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation, match="append-only"):
        cur.execute("DELETE FROM shared.llm_connection_budget WHERE connection_key = 'zz-split'")


def test_a_reason_is_twenty_characters(cur):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        _add(cur, "zz-split", "crm", 6, 2, reason="too short")


def test_cis_app_writes_rows_and_reads_the_current_view(cur):
    cur.execute("SET LOCAL ROLE cis_app")
    _add(cur, "zz-even", "crm", 4, 2)
    assert _current(cur)[("zz-even", "crm")] == 4.0
    assert _cis_cap(cur, "zz-even") == 26.0


# ── 456: the v2 view ─────────────────────────────────────────────────────────────────────────────────────────────

def _spend(cur, account, app, usd, when="now()"):
    cur.execute(f"""INSERT INTO shared.llm_call_log
                        (stage, role, model, account, app, cost_usd, status, quality_signal, created_at)
                    VALUES ('adhoc_zz_budget', 'writer', 'zz-writer', %s, %s, %s, 'ok', '{{}}'::jsonb, {when})""",
                (account, app, usd))


def test_view_shape_owner_and_consumer(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == COLUMNS
    cur.execute("SELECT pg_get_userbyid(relowner) AS o FROM pg_class WHERE oid = %s::regclass", (VIEW,))
    assert cur.fetchone()[0] == "catalog_owner"
    for role, expected in (("tripplanner", True), ("cis_app", False), ("booking", False), ("crm_app", False)):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone():
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, VIEW))
            assert cur.fetchone()[0] is expected, role


def test_view_pool_and_own_share(cur):
    _add(cur, "zz-split", "tripplanner", 10, 1)               # total 100, crm 5, tripplanner 10: CIS 85
    _spend(cur, "zz-split", "cis", 80)
    _spend(cur, "zz-split", None, 3)                          # an old row without app counts as CIS
    _spend(cur, "zz-split", "tripplanner", 12)                # 2 above its share: into the CIS pool
    _spend(cur, "zz-split", "cis", 50, "date_trunc('month', now()) - interval '2 days'")
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = {r["app"]: r for r in _rows(cur, f"SELECT * FROM {VIEW} WHERE connection_key = 'zz-split'")}
    assert set(rows) >= {"cis", "tripplanner", "crm", "booking"}
    cis, tp, booking = rows["cis"], rows["tripplanner"], rows["booking"]
    assert float(cis["share_usd"]) == 85.0 and float(cis["total_usd"]) == 100.0
    assert float(cis["cis_pool_spent_usd"]) == pytest.approx(85.0)          # 80 + 3 + 2
    assert (cis["over_cap"], cis["stops"]) == (True, True)
    assert float(tp["share_usd"]) == 10.0 and tp["in_cis_pool"] is False
    assert float(tp["month_spent_usd"]) == pytest.approx(12.0) and (tp["over_cap"], tp["stops"]) == (True, True)
    assert booking["share_usd"] is None and booking["in_cis_pool"] is True and booking["stops"] is True


def test_view_app_in_the_pool_follows_the_cis_remainder(cur):
    _spend(cur, "zz-even", "tripplanner", 5)
    _spend(cur, "zz-even", "cis", 4)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = {r["app"]: r for r in _rows(cur, f"SELECT * FROM {VIEW} WHERE connection_key = 'zz-even'")}
    tp = rows["tripplanner"]
    assert tp["in_cis_pool"] is True and tp["share_usd"] is None
    assert float(tp["cis_pool_spent_usd"]) == pytest.approx(9.0) and tp["over_cap"] is False   # 9 of CIS's 25


def test_switch_setting_row(cur):
    rows = _rows(cur, "SELECT app, value_type, default_value::text AS d, approval, impact, enforced, "
                      "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = %s",
                 SWITCH_KEY)
    assert rows == [{"app": "tripplanner", "value_type": "bool", "d": "false", "approval": "maker_checker",
                     "impact": "high", "enforced": True, "guided": True}]
