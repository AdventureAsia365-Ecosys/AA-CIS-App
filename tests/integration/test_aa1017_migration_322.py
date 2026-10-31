"""AA-1017 — migration 322 (provider connection v2: the decide / data / guard kinds, a secret and host guard per kind,
failure_domain / served_roles / access_state / limits, and the D9 revoke) against real Postgres.

conftest applies every migration, 322 included. Each test re-runs the file (idempotent, errors not swallowed). The route
snapshot test proves what the migration does to the S262 plan's Q0 hash: it adds exactly the four seeded 'cn|' lines,
and the hash over the LLM kinds (every row the LLM dispatch can use) is the same with and without them.
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

from shared.llm_client.invariant import failure_domain  # noqa: E402

M322 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "322_provider_connection_v2.sql")
LLM_KINDS = ("bedrock", "anthropic", "openai", "azure_openai", "mock")
SEEDED = {"typesafe", "dataforseo", "geoapify", "bedrock-guardrails"}
CIS_APP_UPDATE = {"enabled", "timeout_s", "max_retries", "max_concurrency", "rpm_limit", "monthly_budget_usd", "notes",
                  "updated_at", "updated_by", "access_state", "last_probe_at", "daily_credit_limit", "low_balance_usd"}

# The S262 plan's Q0 lines (explicit columns). The hash itself is md5(string_agg(t, E'\n' ORDER BY t)).
Q0_LINES = """
  SELECT format('rc|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', stage, role, provider, model_id,
         coalesce(account_route,''), fallback_model_ids, coalesce(shadow_model_id,''),
         shadow_sample_pct, coalesce(allowed_scopes::text,''), is_active) AS t
    FROM shared.llm_role_config
  UNION ALL SELECT format('dp|%s|%s|%s|%s|%s', model_key, connection_key, wire_id, priority, enabled)
    FROM shared.llm_model_deployment
  UNION ALL SELECT format('cn|%s|%s|%s|%s', connection_key, kind, enabled, data_scope)
    FROM shared.llm_provider_connection {cn_where}
  UNION ALL SELECT format('ct|%s|%s|%s|%s', model_key, enabled, api_style, bedrock_profile_ids::text)
    FROM shared.llm_model_catalog
  UNION ALL SELECT format('al|%s', a::text) FROM shared.llm_model_alias a
"""


def _q0(c, llm_only: bool = False) -> tuple[str, int, set]:
    where = "WHERE kind IN ('bedrock', 'anthropic', 'openai', 'azure_openai', 'mock')" if llm_only else ""
    lines = Q0_LINES.format(cn_where=where)
    c.execute(f"SELECT md5(string_agg(t, E'\\n' ORDER BY t)) AS h, count(*) AS n FROM ({lines}) s")
    row = c.fetchone()
    c.execute(f"SELECT t FROM ({lines}) s WHERE t LIKE 'cn|%%'")
    return row["h"], row["n"], {r["t"] for r in c.fetchall()}


def _run(c):
    c.execute(open(M322, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("RESET ROLE")
        c.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key LIKE 'zz-aa1017%'")
        _run(c)
        conn.close()


def _insert(c, **kw):
    row = dict(connection_key="zz-aa1017-x", kind="typesafe", auth_mode="api_key", label="t",
               data_scope="vendor_direct", enabled=False, updated_by="test")
    row.update(kw)
    c.execute(f"INSERT INTO shared.llm_provider_connection ({', '.join(row)}) "
              f"VALUES ({', '.join(['%s'] * len(row))})", list(row.values()))


def _refused(c, **kw):
    c.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(c, **kw)
    c.execute("ROLLBACK")


def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '322'")
    assert cur.fetchone()
    cur.execute("SELECT connection_key FROM shared.llm_provider_connection")
    assert {r["connection_key"] for r in cur.fetchall()} >= SEEDED | {"acc3", "acc2", "openai-direct"}


def test_route_snapshot_gains_only_the_four_seeded_rows(cur):
    full_h, full_n, full_cn = _q0(cur)
    llm_h, llm_n, llm_cn = _q0(cur, llm_only=True)
    assert full_n == llm_n + 4
    assert full_cn - llm_cn == {"cn|typesafe|typesafe|t|vendor_direct", "cn|dataforseo|dataforseo|t|vendor_direct",
                                "cn|geoapify|geoapify|t|vendor_direct",
                                "cn|bedrock-guardrails|bedrock_guardrails|f|aws_only"}
    cur.execute("BEGIN")                                     # without the seeded rows: the LLM-only hash, unchanged
    cur.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = ANY(%s)", (sorted(SEEDED),))
    assert _q0(cur)[:2] == (llm_h, llm_n)
    cur.execute("ROLLBACK")
    assert full_h != llm_h


def test_llm_rows_are_backfilled_like_the_failure_domain_report(cur):
    cur.execute("SELECT * FROM shared.llm_provider_connection WHERE kind = ANY(%s)", (list(LLM_KINDS),))
    rows = {r["connection_key"]: r for r in cur.fetchall()}
    for r in rows.values():
        assert r["failure_domain"] == failure_domain(dict(r)), r["connection_key"]
        assert r["access_state"] == "unknown"
    assert set(rows["acc2"]["served_roles"]) == {"embed", "judge", "validate"}            # tech lead S261
    assert set(rows["acc3"]["served_roles"]) == {"writer", "judge", "validate", "embed"}
    assert set(rows["openai-direct"]["served_roles"]) == {"writer", "judge", "validate"}
    assert rows["acc3"]["balance_source"] == "cost_explorer" and rows["openai-direct"]["balance_source"] == "none"


def test_seeded_rows_hold_todays_values(cur):
    cur.execute("SELECT * FROM shared.llm_provider_connection WHERE connection_key = ANY(%s)", (sorted(SEEDED),))
    rows = {r["connection_key"]: r for r in cur.fetchall()}
    jev, dfs, geo, guard = rows["typesafe"], rows["dataforseo"], rows["geoapify"], rows["bedrock-guardrails"]
    assert (jev["base_url"], jev["timeout_s"], jev["max_retries"], float(jev["low_balance_usd"]), jev["enabled"]) == (
        "https://api.typesafe.ai", 3, 2, 3.0, True)
    assert jev["secret_name"] == "aa-cis/dev/typesafe" and jev["served_roles"] == ["decide"]
    assert (float(dfs["low_balance_usd"]), dfs["auth_mode"], dfs["secret_name"]) == (
        10.0, "basic_auth", "aa-cis/dev/dataforseo")
    assert (geo["daily_credit_limit"], geo["secret_name"], geo["balance_source"]) == (
        2500, "aa-cis/dev/geoapify", "daily_credit_window")
    assert guard["enabled"] is False and guard["auth_mode"] == "task_role" and guard["secret_name"] is None
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment WHERE connection_key = ANY(%s)",
                (sorted(SEEDED),))
    assert cur.fetchone()["n"] == 0                          # no LLM deployment on these kinds


def test_rerun_keeps_an_edited_limit(cur):
    cur.execute("UPDATE shared.llm_provider_connection SET daily_credit_limit = 1000 WHERE connection_key = 'geoapify'")
    _run(cur)
    cur.execute("SELECT daily_credit_limit FROM shared.llm_provider_connection WHERE connection_key = 'geoapify'")
    assert cur.fetchone()["daily_credit_limit"] == 1000
    cur.execute("UPDATE shared.llm_provider_connection SET daily_credit_limit = 2500 WHERE connection_key = 'geoapify'")


@pytest.mark.parametrize("fields", [
    dict(secret_name="aa-llm/dev/typesafe"),                                  # an LLM secret on a typesafe row
    dict(secret_name="aa-cis/dev/dataforseo"),                                # another kind's secret
    dict(secret_name="aa-cis/dev/rds"),
    dict(kind="anthropic", secret_name="aa-cis/dev/typesafe"),                # an LLM row reading the Jev secret
    dict(base_url="https://api.anthropic.com"),                               # another kind's host
    dict(base_url="https://evil.example.com"),
    dict(kind="anthropic", base_url="https://api.typesafe.ai"),
    dict(kind="geoapify", base_url="https://api.geoapify.com.evil.io"),
    dict(kind="anthropic", base_url="http://api.anthropic.com"),
    dict(kind="openai", base_url="https://api.openai.com:8443/v1"),
    dict(kind="anthropic", credential_env="OPENAI_API_KEY", secret_name=None),  # an env key of another kind
    dict(kind="typesafe", auth_mode="basic_auth"),
    dict(kind="dataforseo", auth_mode="api_key"),
    dict(kind="bedrock_guardrails", auth_mode="task_role", data_scope="aws_only", region="us-west-1",
         secret_name="aa-cis/dev/typesafe"),
    dict(kind="bedrock_guardrails", auth_mode="task_role", data_scope="aws_only", region="us-west-1",
         base_url="https://api.typesafe.ai"),
    dict(kind="dataforseo", auth_mode="basic_auth", enabled=True),            # enabled without a secret
    dict(kind="jev"),
    dict(served_roles=["decide", "admin"]),
    dict(served_roles=[]),
    dict(access_state="maybe"),
    dict(balance_source="vibes"),
    dict(daily_credit_limit=-1),
    dict(low_balance_usd=-1),
])
def test_per_kind_checks_refuse(cur, fields):
    _refused(cur, **fields)


def test_per_kind_checks_accept_complete_rows(cur):
    cur.execute("BEGIN")
    _insert(cur, connection_key="zz-aa1017-a", secret_name="aa-cis/prod/typesafe",
            base_url="https://api.typesafe.ai/v1", enabled=True, served_roles=["decide"])
    _insert(cur, connection_key="zz-aa1017-b", kind="dataforseo", auth_mode="basic_auth",
            secret_name="aa-cis/dev/dataforseo", base_url="https://api.dataforseo.com/v3", enabled=True)
    _insert(cur, connection_key="zz-aa1017-c", kind="geoapify", secret_name="aa-cis/dev/geoapify",
            base_url="https://api.geoapify.com/v1", daily_credit_limit=100, enabled=True)
    _insert(cur, connection_key="zz-aa1017-d", kind="bedrock_guardrails", auth_mode="assume_role",
            data_scope="aws_only", region="us-west-1", role_name="CRM-Guard", external_id="e",
            account_id_env="BEDROCK_ACCT_PRIMARY", enabled=True)
    _insert(cur, connection_key="zz-aa1017-e", kind="azure_openai", secret_name="aa-llm/dev/azure",
            base_url="https://my-resource.openai.azure.com", enabled=True)
    cur.execute("ROLLBACK")


def test_cis_app_cannot_move_a_secret_or_an_endpoint(cur):
    cur.execute("SELECT column_name FROM information_schema.column_privileges WHERE table_schema = 'shared' "
                "AND table_name = 'llm_provider_connection' AND grantee = 'cis_app' AND privilege_type = 'UPDATE'")
    assert {r["column_name"] for r in cur.fetchall()} == CIS_APP_UPDATE
    for sql in ("UPDATE shared.llm_provider_connection SET secret_name = 'aa-llm/dev/other' "
                "WHERE connection_key = 'anthropic-direct'",
                "UPDATE shared.llm_provider_connection SET base_url = 'https://api.anthropic.com' "
                "WHERE connection_key = 'anthropic-direct'",
                "UPDATE shared.llm_provider_connection SET served_roles = ARRAY['writer'] "
                "WHERE connection_key = 'acc2'"):
        cur.execute("BEGIN")
        cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")               # Dev has it; rolled back below
        cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            cur.execute(sql)
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    cur.execute("SET LOCAL SESSION AUTHORIZATION cis_app")
    cur.execute("UPDATE shared.llm_provider_connection SET access_state = 'ok', last_probe_at = now(), "
                "low_balance_usd = 5, updated_at = now(), updated_by = 'admin:t' WHERE connection_key = 'typesafe'")
    cur.execute("ROLLBACK")
