"""AA-1002 (gateway slice S5) — migration 311 (shared.llm_role_config.allowed_scopes) against real Postgres.

conftest applies every migration; these tests run the file again directly, errors NOT swallowed (lesson S231): the
backfill from the routes, idempotence (a later decision is kept), the CHECK, the default for a new row, and that the
runtime role cannot change a scope while it still edits routes. Every test removes its own rows.
"""
import os
import sys

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "311_llm_stage_allowed_scopes.sql")
PREFIX = "zz_aa1002_s5_"
BOTH = ["aws_only", "vendor_direct"]


def _connect(user=DB_USER, password=DB_PASS):
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=password)
    conn.autocommit = True
    return conn


@pytest.fixture
def cur():
    conn = _connect()
    c = conn.cursor()
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_role_config WHERE stage LIKE %s", (PREFIX + "%",))
        conn.close()


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _stage(c, name, model_id, fallbacks=(), shadow=None, scopes="NULL"):
    c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, updated_by, "
              "fallback_model_ids, shadow_model_id, shadow_sample_pct, allowed_scopes) "
              f"VALUES (%s, 'judge', 'openai', %s, true, now(), 'test', %s, %s, 0, {scopes})",
              (PREFIX + name, model_id, list(fallbacks), shadow))


def _scopes(c, name):
    c.execute("SELECT allowed_scopes FROM shared.llm_role_config WHERE stage = %s", (PREFIX + name,))
    return c.fetchone()[0]


def test_registered_and_every_seeded_stage_has_a_scope(cur):
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '311'")
    assert cur.fetchone()
    cur.execute("SELECT stage FROM shared.llm_role_config WHERE allowed_scopes IS NULL AND stage NOT LIKE %s",
                (PREFIX + "%",))
    assert cur.fetchall() == []


def test_the_backfill_follows_the_route(cur):
    # stages written before 311 (NULL) — the shapes of today's routes
    _stage(cur, "judge_last_resort", "gpt-5.6-luna", ["gpt-6-luna", "gpt-6-luna-openai"])
    _stage(cur, "bedrock_only", "gpt-5.6-luna", ["gpt-6-luna"], shadow="gpt-6-luna")
    _stage(cur, "anthropic", "haiku", ["sonnet"])          # Anthropic rows have acc3 AND anthropic-direct: in AWS
    _stage(cur, "decided", "gpt-5.6-luna", ["gpt-6-luna"], scopes="ARRAY['aws_only','vendor_direct']")
    _run(cur)
    _run(cur)                                              # idempotent
    assert _scopes(cur, "judge_last_resort") == BOTH
    assert _scopes(cur, "bedrock_only") == ["aws_only"]
    assert _scopes(cur, "anthropic") == ["aws_only"]
    assert _scopes(cur, "decided") == BOTH                 # a later decision is never overwritten


def test_seeded_stages_naming_the_openai_api_allow_vendor_direct_and_only_they(cur):
    cur.execute("""
        SELECT r.stage, r.allowed_scopes,
               EXISTS (SELECT 1 FROM shared.llm_model_catalog m WHERE m.provider = 'openai'
                        AND (m.model_key = r.model_id OR m.model_key = ANY (r.fallback_model_ids)
                             OR m.model_key = r.shadow_model_id)) AS names_openai_api
          FROM shared.llm_role_config r WHERE r.stage NOT LIKE %s""", (PREFIX + "%",))
    rows = cur.fetchall()
    assert rows
    for stage, scopes, names_openai_api in rows:
        assert scopes == (BOTH if names_openai_api else ["aws_only"]), stage


def test_a_new_row_defaults_to_aws_only(cur):
    cur.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, "
                "updated_by, fallback_model_ids, shadow_sample_pct) "
                "VALUES (%s, 'writer', 'claude', 'haiku', true, now(), 'test', '{}', 0)", (PREFIX + "new",))
    assert _scopes(cur, "new") == ["aws_only"]


@pytest.mark.parametrize("bad", ["ARRAY['anywhere']", "ARRAY[]::text[]", "ARRAY['aws_only','eu_only']"])
def test_the_check_refuses_unknown_or_empty_scopes(cur, bad):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _stage(cur, "bad", "haiku", scopes=bad)


def test_cis_app_edits_routes_but_not_scopes(cur):
    _stage(cur, "grants", "gpt-5.6-luna", ["gpt-6-luna"], scopes="ARRAY['aws_only']")
    # The replay skips CIS 244 and conftest's x1_244_undo revokes the shared tables from cis_app, so (as in the 251
    # test) schema USAGE is granted for this test only, and 311 is re-run (idempotent) to put its column grants back.
    cur.execute("SELECT has_schema_privilege('cis_app', 'shared', 'USAGE')")
    usage_granted = not cur.fetchone()[0]
    if usage_granted:
        cur.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    cur.execute("SELECT has_table_privilege('cis_app', 'shared.llm_role_config', 'SELECT')")
    select_granted = not cur.fetchone()[0]
    if select_granted:  # an UPDATE ... WHERE stage = ... also needs SELECT; 244 gives it on Dev
        cur.execute("GRANT SELECT ON shared.llm_role_config TO cis_app")
    _run(cur)
    try:
        conn = _connect("cis_app", CIS_APP_CI_PASSWORD)
    except psycopg2.OperationalError as e:
        if select_granted:
            cur.execute("REVOKE SELECT ON shared.llm_role_config FROM cis_app")
        if usage_granted:
            cur.execute("REVOKE USAGE ON SCHEMA shared FROM cis_app")
        pytest.skip(f"cannot log in as cis_app here: {e}")
    try:
        c = conn.cursor()
        c.execute("UPDATE shared.llm_role_config SET fallback_model_ids = %s, updated_by = 'cis_app' WHERE stage = %s",
                  (["gpt-6-luna", "gpt-6-luna-openai"], PREFIX + "grants"))
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            c.execute("UPDATE shared.llm_role_config SET allowed_scopes = %s WHERE stage = %s",
                      (BOTH, PREFIX + "grants"))
        c.execute("ROLLBACK")
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            c.execute("INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, "
                      "updated_by, fallback_model_ids, shadow_sample_pct, allowed_scopes) "
                      "VALUES (%s, 'writer', 'claude', 'haiku', true, now(), 'x', '{}', 0, %s)",
                      (PREFIX + "grants2", BOTH))
    finally:
        conn.close()
        if select_granted:
            cur.execute("REVOKE SELECT ON shared.llm_role_config FROM cis_app")
        if usage_granted:
            cur.execute("REVOKE USAGE ON SCHEMA shared FROM cis_app")
    assert _scopes(cur, "grants") == ["aws_only"]
