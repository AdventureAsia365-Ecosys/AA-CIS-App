"""AA-1017 — migration 324 (five acc2 candidate chat models as catalog + deployment rows, no route) against real
Postgres: the "add a model = one migration PR, no app code" proof.

conftest applies every migration, 324 included. Each test re-runs the file (idempotent, errors not swallowed). The
route snapshot test proves what the migration does to the S262 plan's Q0 hash: exactly five 'ct|' and five 'dp|'
lines, and every other line (role config, other deployments, connections, aliases) unchanged.
"""
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _acc2_routes import candidates_off_routes  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

M324 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "324_llm_acc2_candidate_models.sql")
KEYS = ("nova-lite", "nova-2-lite", "llama4-maverick", "llama4-scout", "kimi-k3")
FAMILY = {"nova-lite": "amazon", "nova-2-lite": "amazon", "llama4-maverick": "meta", "llama4-scout": "meta",
          "kimi-k3": "moonshot"}

# The S262 plan's Q0 lines (explicit columns). The hash itself is md5(string_agg(t, E'\n' ORDER BY t)).
Q0_LINES = """
  SELECT format('rc|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', stage, role, provider, model_id,
         coalesce(account_route,''), fallback_model_ids, coalesce(shadow_model_id,''),
         shadow_sample_pct, coalesce(allowed_scopes::text,''), is_active) AS t
    FROM shared.llm_role_config
  UNION ALL SELECT format('dp|%s|%s|%s|%s|%s', model_key, connection_key, wire_id, priority, enabled)
    FROM shared.llm_model_deployment
  UNION ALL SELECT format('cn|%s|%s|%s|%s', connection_key, kind, enabled, data_scope)
    FROM shared.llm_provider_connection
  UNION ALL SELECT format('ct|%s|%s|%s|%s', model_key, enabled, api_style, bedrock_profile_ids::text)
    FROM shared.llm_model_catalog
  UNION ALL SELECT format('al|%s', a::text) FROM shared.llm_model_alias a
"""


def _run(c):
    # AA-1026 I6: 331 / 332 end some judge / validate routes on a candidate, which 324's guard refuses on a re-run.
    with candidates_off_routes(c):
        c.execute(open(M324, encoding="utf-8").read())


def _q0_lines(c) -> set:
    c.execute(f"SELECT t FROM ({Q0_LINES}) s")
    return {r["t"] for r in c.fetchall()}


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
        _run(c)
        conn.close()


def test_applies_twice_and_registers(cur):
    _run(cur)
    cur.execute("SELECT version AS ok FROM shared.schema_versions WHERE version = '324'")
    assert cur.fetchone()["ok"] == "324"


def test_five_enabled_priced_catalog_rows_with_their_family(cur):
    cur.execute("SELECT model_key, vendor, family, provider, api_style, bedrock_profile_ids, enabled, callable_via, "
                "price_in_per_mtok, price_out_per_mtok, supports_temperature, capabilities "
                "FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(KEYS),))
    rows = {r["model_key"]: r for r in cur.fetchall()}
    assert set(rows) == set(KEYS)
    for key, r in rows.items():
        assert r["enabled"] is True and r["provider"] == "bedrock" and r["api_style"] == "converse"
        assert r["family"] == FAMILY[key] == r["vendor"]
        assert r["callable_via"] == ["llm_client"]
        assert r["price_in_per_mtok"] > 0 and r["price_out_per_mtok"] > 0
        assert set(r["bedrock_profile_ids"]) == {"acc2"}
        assert r["bedrock_profile_ids"]["acc2"].startswith("us.")    # an inference profile id, never an ARN
        # CIS 333 (AA-1026) records that Llama 4 Scout refuses a forced tool choice (live contract smoke, S263), CIS 337
        # (AA-1024 I6) the same for Maverick and the structured output mode of both; a test that sets 324's rows aside
        # re-creates the rows without them, so the keys may be missing here
        later = ("forced_tool_choice", "structured_output") if key.startswith("llama4-") else ()
        caps = {k: v for k, v in r["capabilities"].items() if k not in later}
        assert caps == {}, key
    for key in ("llama4-scout", "llama4-maverick"):
        assert rows[key]["capabilities"].get("forced_tool_choice") in (None, False)
        assert rows[key]["capabilities"].get("structured_output") in (None, "prompt")
    assert rows["kimi-k3"]["supports_temperature"] is False


def test_each_model_has_one_enabled_acc2_deployment_on_its_profile(cur):
    cur.execute("SELECT d.model_key, d.connection_key, d.wire_id, d.priority, d.enabled, d.origin, "
                "c.bedrock_profile_ids ->> 'acc2' AS profile "
                "FROM shared.llm_model_deployment d JOIN shared.llm_model_catalog c USING (model_key) "
                "WHERE d.model_key = ANY (%s)", (list(KEYS),))
    rows = cur.fetchall()
    assert sorted(r["model_key"] for r in rows) == sorted(KEYS)
    for r in rows:
        assert (r["connection_key"], r["priority"], r["enabled"], r["origin"]) == ("acc2", 20, True,
                                                                                  "bedrock_profile")
        assert r["wire_id"] == r["profile"]


def test_no_route_alias_or_stage_names_a_candidate(cur):
    """324 routes nothing. Since CIS 331 / 332 (AA-1026 I6) a candidate may END the route of a judge / validate stage,
    and since 333 it may stand right before the OpenAI API hop that ends one; never a primary, a shadow, a writer's
    model or an earlier fallback. (The route without its trailing OpenAI API hops ends on the one candidate.)"""
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config r "
                "CROSS JOIN LATERAL (SELECT array_remove(r.fallback_model_ids, %(v)s) AS g) x "
                "WHERE r.model_id = ANY (%(k)s) OR r.shadow_model_id = ANY (%(k)s) "
                "OR (r.fallback_model_ids && %(k)s::text[] AND (r.role NOT IN ('judge', 'validate') "
                "    OR NOT (x.g[cardinality(x.g)] = ANY (%(k)s)) "
                "    OR x.g[1:cardinality(x.g) - 1] && %(k)s::text[] "
                "    OR array_position(r.fallback_model_ids, %(v)s) < array_position(r.fallback_model_ids, "
                "                                                           x.g[cardinality(x.g)])))",
                {"k": list(KEYS), "v": "gpt-6-luna-openai"})
    assert cur.fetchone()["n"] == 0
    with candidates_off_routes(cur):
        cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE fallback_model_ids && %s::text[]",
                    (list(KEYS),))
        assert cur.fetchone()["n"] == 0                                   # only fallbacks, so the helper clears them
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_alias WHERE model_key = ANY (%s)", (list(KEYS),))
    assert cur.fetchone()["n"] == 0


def test_route_snapshot_gains_exactly_the_ten_candidate_lines(cur):
    with_rows = _q0_lines(cur)
    cur.execute("BEGIN")
    try:
        cur.execute("DELETE FROM shared.llm_model_deployment WHERE model_key = ANY (%s)", (list(KEYS),))
        cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(KEYS),))
        without = _q0_lines(cur)
    finally:
        cur.execute("ROLLBACK")
    added = with_rows - without
    assert without <= with_rows
    assert len(added) == 10
    assert {t.split("|")[0] for t in added} == {"ct", "dp"}
    assert {t.split("|")[1] for t in added} == set(KEYS)


def test_guard_refuses_when_a_stage_already_routes_to_a_candidate(cur):
    cur.execute("SELECT stage FROM shared.llm_role_config ORDER BY stage LIMIT 1")
    row = cur.fetchone()
    if row is None:
        pytest.skip("no llm_role_config row in this DB")
    cur.execute("BEGIN")
    try:
        cur.execute("UPDATE shared.llm_role_config SET shadow_model_id = 'nova-lite' WHERE stage = %s",
                    (row["stage"],))
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.RaiseException, match="route changes are separate"):
            cur.execute(open(M324, encoding="utf-8").read().replace("BEGIN;", "", 1).replace("COMMIT;", ""))
    finally:
        cur.execute("ROLLBACK")


def test_gateway_can_call_each_candidate_on_acc2_without_code_changes(cur):
    """The proof that no app code is needed: the gateway's own row readers and its callability rule accept every new
    row on the acc2 task-role connection (Converse through bedrock_satellite.native_runtime)."""
    from shared.llm_client import catalog, connections
    from shared.llm_client.vendor_route import _callable

    cur.execute(catalog._SELECT_SQL.replace(" ORDER BY model_key", ""))
    models = {r["model_key"]: catalog._row_to_model(r) for r in cur.fetchall() if r["model_key"] in KEYS}
    cur.execute(connections._SELECT_SQL)
    acc2 = next(connections._row_to_connection(r) for r in cur.fetchall() if r["connection_key"] == "acc2")
    assert set(models) == set(KEYS)
    for key, model in models.items():
        assert _callable(model, acc2) is None, key
        assert model.price_in_per_mtok and model.price_out_per_mtok
