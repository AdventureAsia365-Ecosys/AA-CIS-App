"""AA-1027 E4 (root plan S263, gap TP-5) — migration 350: catalog.v_llm_gateway_config_v1 gains supports_temperature
and stage_params (the shared.stage_model_params row of the stage and model), applied the way DB Migrate applies a cis/
file since X1 S22.

conftest skips 323, 341 and 350 in the replay (catalog recipe, 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 323,
341, then 350 twice as db_migrator under the live-owner switch (errors NOT swallowed). Teardown drops the view and
revokes the three files' column grants, so later modules see the replay state.

  * the migration: registered once; the view keeps 341's columns in order and appends exactly two; catalog_owner reads
    only the listed columns (never stage_model_params notes / created_at / updated_at / updated_by); tripplanner still
    cannot read shared.stage_model_params;
  * stage_params (as tripplanner, rolled back): the row of (route stage, chain model) as one object, NULL for a model
    with no row, never another stage's row; supports_temperature from the catalog;
  * the SELECT TripPlanner runs (the 323 contract columns + the 341 and 350 appended ones) answers.
Invented stage and model names only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa1027_migration_341 as t341  # noqa: E402
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

from shared.aa_llm import contract  # noqa: E402

HERE = os.path.dirname(__file__)
M350 = os.path.join(HERE, "..", "..", "api", "migrations", "350_llm_gateway_view_stage_params.sql")
APPENDED = ("supports_temperature", "stage_params")
GRANTED_350 = {
    "llm_model_catalog": {"supports_temperature"},
    "stage_model_params": {"stage", "model_key", "max_tokens", "min_output_tokens", "temperature", "effort",
                           "reasoning_mode"},
}
PARAM_KEYS = {"max_tokens", "min_output_tokens", "temperature", "effort", "reasoning_mode"}


def undo_350(a):
    t341.undo_341(a)                                         # drops the view, 323's + 341's grants and registrations
    for table, cols in GRANTED_350.items():
        a.execute(f"REVOKE SELECT ({', '.join(sorted(cols))}) ON shared.{table} FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '350'")


@pytest.fixture(scope="module", autouse=True)
def applied_350(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_350(a)
        t323._apply(t323.MIGRATION)
        t323._apply(t341.M341)
        t323._apply(M350)
        t323._apply(M350)                                    # idempotent re-run
        yield
    finally:
        undo_350(a)
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


def _json(value):
    return value if value is None or isinstance(value, (list, dict)) else json.loads(value)


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '350'")
    assert cur.fetchone()[0] == 1


def test_columns_are_341_then_the_two_appended(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (contract.GATEWAY_CONFIG_VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == contract.GATEWAY_CONFIG_COLUMNS + t341.APPENDED + APPENDED


def test_owner_barrier_and_consumers(cur):
    view = contract.GATEWAY_CONFIG_VIEW
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (view,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (view,))
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user", "cis_app"):
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, view))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_listed_columns(cur):
    want = {t: set(c) for t, c in t323.GRANTED.items()}
    for grants in (t341.GRANTED_341, GRANTED_350):
        for table, cols in grants.items():
            want.setdefault(table, set()).update(cols)
    for table, cols in want.items():
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (f"shared.{table}",))
        assert cur.fetchone()[0] is False, f"table-wide SELECT on {table}"
        cur.execute("""SELECT a.attname FROM pg_attribute a
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                          AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""",
                    (f"shared.{table}",))
        assert {r[0] for r in cur.fetchall()} == cols, table


def test_tripplanner_cannot_read_the_params_table(cur):
    cur.execute("SELECT has_any_column_privilege('tripplanner', 'shared.stage_model_params', 'SELECT')")
    assert cur.fetchone()[0] is False


def _seed(cur):
    for key, temp in (("zz-writer", True), ("zz-other", False)):
        cur.execute("""INSERT INTO shared.llm_model_catalog
                           (model_key, label, vendor, provider, api_style, bedrock_profile_ids, callable_via,
                            supports_temperature, price_in_per_mtok, price_out_per_mtok, enabled, updated_at,
                            updated_by, capabilities)
                       VALUES (%s, %s, 'anthropic', 'mock', 'anthropic_native', '{}'::jsonb, ARRAY['llm_client'], %s,
                               1.0, 5.0, true, now(), 'test', '{}'::jsonb)""", (key, key, temp))
    for stage, model in (("tp_zz_compose", "zz-writer"), ("tp_zz_elsewhere", "zz-other")):
        cur.execute("""INSERT INTO shared.llm_role_config
                           (stage, role, provider, model_id, is_active, updated_at, updated_by, fallback_model_ids,
                            shadow_sample_pct)
                       VALUES (%s, 'writer', 'claude', %s, true, now(), 'test', %s, 0)""",
                    (stage, model, ["zz-other"] if stage == "tp_zz_compose" else []))
    p = """INSERT INTO shared.stage_model_params
               (stage, model_key, max_tokens, temperature, effort, reasoning_mode, created_at, updated_at, updated_by)
           VALUES (%s, %s, %s, %s, %s, %s, now(), now(), 'test')"""
    cur.execute(p, ("tp_zz_compose", "zz-writer", None, 0.3, "low", None))
    cur.execute(p, ("tp_zz_elsewhere", "zz-other", 900, None, None, "adaptive"))   # another stage's row


def test_stage_params_as_tripplanner(cur):
    _seed(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, "SELECT position, model_key, supports_temperature, stage_params "
                      "FROM catalog.v_llm_gateway_config_v1 WHERE stage = %s ORDER BY position", "tp_zz_compose")
    assert [r["model_key"] for r in rows] == ["zz-writer", "zz-other"]
    writer, other = rows
    params = _json(writer["stage_params"])
    assert set(params) == PARAM_KEYS
    assert (params["temperature"], params["effort"], params["max_tokens"], params["reasoning_mode"]) == (
        0.3, "low", None, None)
    assert writer["supports_temperature"] is True
    assert other["stage_params"] is None                     # tp_zz_elsewhere's row of zz-other is not this stage's
    assert other["supports_temperature"] is False


def test_the_tripplanner_select_runs(cur):
    _seed(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    cols = contract.GATEWAY_CONFIG_COLUMNS + t341.APPENDED + APPENDED
    sql = (f"SELECT {', '.join(cols)} FROM {contract.GATEWAY_CONFIG_VIEW} "
           "WHERE stage = %s AND is_active ORDER BY position")
    rows = _rows(cur, sql, "tp_zz_compose")
    assert [r["model_key"] for r in rows] == ["zz-writer", "zz-other"]
    rows = _rows(cur, contract.GATEWAY_CONFIG_SQL.replace("$1", "%s"), "tp_zz_compose")
    assert len(rows) == 2                                    # the 323 contract SELECT still runs
