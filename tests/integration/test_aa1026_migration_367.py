"""AA-1026 (S267) — migration 367 against real Postgres: the acc3 candidate models as DISABLED catalog + deployment
rows, and the per-deployment probe state (access_state, last_probe_at, last_probe_error) cis_app may write.

conftest applies every migration, 367 included. Each test re-runs the file (idempotent, errors not swallowed).
"""
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

M367 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "367_llm_candidate_models_probe_state.sql")
NEW_KEYS = ("sonnet-4-5", "opus-4-5", "opus-4-6", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra")
UNPRICED = ("gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra")
ACC3_EXTRA = {"kimi-k3": "global.moonshotai.kimi-k3",
              "llama4-maverick": "us.meta.llama4-maverick-17b-instruct-v1:0"}
STATES = ("unknown", "ok", "access_denied", "quota_exhausted", "throttled", "unconfigured", "error")


M376 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "376_llm_anthropic_api_catalog_fallbacks.sql")
# AA-1042 (S269): CIS 376 switches the Opus 4.6 / 4.5 candidates on for their Anthropic API deployment. This module
# checks the post-367 state, so it sets that aside first (the rows as 367 left them) and re-applies 376 at the end.
SET_376_ASIDE = ("UPDATE shared.llm_model_catalog SET enabled = false, updated_by = 'migration-367-aa1026', "
                 "blocked_reason = 'Candidate (CIS 367): not routed. Enable after the availability sweep answers and "
                 "an approved trial.' WHERE model_key IN ('opus-4-6', 'opus-4-5') "
                 "AND updated_by = 'migration-376-aa1042'")


def _run(c):
    c.execute(SET_376_ASIDE)
    c.execute(open(M367, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)                 # another module may have set 324's rows aside (cascade): back to post-367
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _run(c)
        c.execute(open(M376, encoding="utf-8").read())      # back to the post-376 state the later modules see
        conn.close()


def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '367'")
    assert cur.fetchone()["ok"] == "367"


def test_probe_state_columns_default_and_checks(cur):
    cur.execute("SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'shared' AND table_name = 'llm_model_deployment' "
                "AND column_name IN ('access_state', 'last_probe_at', 'last_probe_error')")
    cols = {r["column_name"]: r for r in cur.fetchall()}
    assert set(cols) == {"access_state", "last_probe_at", "last_probe_error"}
    assert cols["access_state"]["is_nullable"] == "NO" and "unknown" in cols["access_state"]["column_default"]
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment WHERE access_state <> 'unknown' "
                "AND last_probe_at IS NULL")
    assert cur.fetchone()["n"] == 0
    cur.execute("BEGIN")
    try:
        for state in STATES:
            cur.execute("UPDATE shared.llm_model_deployment SET access_state = %s "
                        "WHERE model_key = 'opus-4-5' AND connection_key = 'acc3'", (state,))
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE shared.llm_model_deployment SET access_state = 'maybe' "
                        "WHERE model_key = 'opus-4-5' AND connection_key = 'acc3'")
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE shared.llm_model_deployment SET last_probe_error = repeat('x', 301) "
                        "WHERE model_key = 'opus-4-5' AND connection_key = 'acc3'")
    finally:
        cur.execute("ROLLBACK")


def test_cis_app_may_update_only_the_probe_columns(cur):
    cur.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = 'cis_app'")
    if cur.fetchone() is None:
        pytest.skip("no cis_app role in this database")
    for col in ("access_state", "last_probe_at", "last_probe_error"):
        cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_model_deployment', %s, 'UPDATE') AS ok",
                    (col,))
        assert cur.fetchone()["ok"] is True, col
    # AA-1042 (S268): 370 adds UPDATE on priority (the admin's provider order); enabled and wire_id stay migration-only
    for col in ("enabled", "wire_id"):
        cur.execute("SELECT has_column_privilege('cis_app', 'shared.llm_model_deployment', %s, 'UPDATE') AS ok",
                    (col,))
        assert cur.fetchone()["ok"] is False, col


def test_six_disabled_catalog_rows_priced_or_flagged(cur):
    cur.execute("SELECT model_key, vendor, family, provider, api_style, bedrock_profile_ids, enabled, blocked_reason, "
                "price_in_per_mtok, price_out_per_mtok, price_source, callable_via "
                "FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(NEW_KEYS),))
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert set(rows) == set(NEW_KEYS)
    for key, r in rows.items():
        assert r["enabled"] is False and r["blocked_reason"], key
        assert (r["provider"], r["api_style"], r["callable_via"]) == ("bedrock", "converse", ["llm_client"])
        assert r["family"] == r["vendor"] == ("openai" if key.startswith("gpt-") else "anthropic")
        assert set(r["bedrock_profile_ids"]) == {"acc3"}
        assert r["bedrock_profile_ids"]["acc3"].startswith("global.")     # a profile id, never an ARN
        if key in UNPRICED:
            assert r["price_in_per_mtok"] is None and r["price_out_per_mtok"] is None
            assert r["price_source"].startswith("UNPRICED")
        else:
            assert r["price_in_per_mtok"] > 0 and r["price_out_per_mtok"] > 0


def test_eight_disabled_acc3_deployments(cur):
    cur.execute("SELECT d.model_key, d.wire_id, d.priority, d.enabled, d.origin, d.access_state, "
                "c.bedrock_profile_ids ->> 'acc3' AS profile, d.price_in_override "
                "FROM shared.llm_model_deployment d JOIN shared.llm_model_catalog c USING (model_key) "
                "WHERE d.connection_key = 'acc3' AND d.updated_by = 'migration-367-aa1026'")
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert set(rows) == set(NEW_KEYS) | set(ACC3_EXTRA)
    for key, r in rows.items():
        assert r["enabled"] is False and r["origin"] == "seed", key
        if key in NEW_KEYS:
            assert (r["wire_id"], r["priority"]) == (r["profile"], 10)
        else:
            assert (r["wire_id"], r["priority"]) == (ACC3_EXTRA[key], 30)
    assert float(rows["kimi-k3"]["price_in_override"]) == 3.0
    # acc2 stays the first (and only enabled) way to the two existing models
    cur.execute("SELECT model_key, enabled, priority FROM shared.llm_model_deployment "
                "WHERE model_key = ANY (%s) AND connection_key = 'acc2'", (list(ACC3_EXTRA),))
    assert {r["model_key"]: (r["enabled"], r["priority"]) for r in cur.fetchall()} == {
        "kimi-k3": (True, 20), "llama4-maverick": (True, 20)}


def test_no_route_or_alias_names_a_new_key(cur):
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE model_id = ANY (%(k)s) "
                "OR shadow_model_id = ANY (%(k)s) OR fallback_model_ids && %(k)s::text[]", {"k": list(NEW_KEYS)})
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_alias WHERE model_key = ANY (%s)", (list(NEW_KEYS),))
    assert cur.fetchone()["n"] == 0


def test_guard_refuses_when_a_stage_routes_to_a_new_key(cur):
    cur.execute("SELECT stage FROM shared.llm_role_config ORDER BY stage LIMIT 1")
    row = cur.fetchone()
    if row is None:
        pytest.skip("no llm_role_config row in this DB")
    cur.execute("BEGIN")
    try:
        cur.execute("UPDATE shared.llm_role_config SET shadow_model_id = 'opus-4-6' WHERE stage = %s",
                    (row["stage"],))
        with pytest.raises(psycopg2.errors.RaiseException, match="route changes are separate"):
            cur.execute(open(M367, encoding="utf-8").read().replace("BEGIN;", "", 1).replace("COMMIT;", ""))
    finally:
        cur.execute("ROLLBACK")


def test_the_gateway_can_call_every_new_row_on_acc3(cur):
    """No app code needed for the sweep to reach them: the gateway's readers and callability rule accept each row on
    the acc3 assume-role connection (disabled rows included: the probe calls a disabled deployment on purpose)."""
    from shared.llm_client import catalog, connections
    from shared.llm_client.vendor_route import _callable

    cur.execute(catalog._SELECT_SQL.replace(" ORDER BY model_key", ""))
    models = {r["model_key"]: catalog._row_to_model(r) for r in cur.fetchall() if r["model_key"] in NEW_KEYS}
    cur.execute(connections._SELECT_SQL)
    acc3 = next(connections._row_to_connection(r) for r in cur.fetchall() if r["connection_key"] == "acc3")
    assert set(models) == set(NEW_KEYS)
    for key, model in models.items():
        assert _callable(model, acc3) is None, key
