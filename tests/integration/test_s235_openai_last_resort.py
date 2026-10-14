"""S235 — migration 224 against real Postgres: after it, an OpenAI platform API model (catalog provider
'openai') appears in shared.llm_role_config only as the LAST fallback of a route — never as a primary
or a shadow — and GPT-4.1 is in no route and disabled in the catalog.

Lesson S231: conftest swallows migration errors, so this test seeds the rows it needs itself (the live
Dev shape and the fresh-DB shape), applies 224 inside one transaction, asserts, and rolls back.
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "224_openai_last_resort_only.sql")

# stage -> (model_id, fallback_model_ids, shadow_model_id, shadow_sample_pct) before 224
SEED = {
    "zz_s235_live_judge":  ("gpt-5.6-luna", ["gpt-6-luna", "gpt-6-luna-openai"], "gpt-6-luna", 10),
    "zz_s235_fresh_judge": ("gpt-5.6-luna", ["gpt-6-luna", "gpt-4.1", "gpt-6-luna-openai"], "gpt-4.1", 100),
    "zz_s235_openai_mid":  ("gpt-5.6-luna", ["gpt-6-luna-openai", "gpt-6-luna"], None, 0),
    "zz_s235_gpt41_last":  ("gpt-5.6-luna", ["gpt-6-luna", "gpt-4.1"], None, 0),
}
EXPECTED = {
    "zz_s235_live_judge":  (["gpt-6-luna", "gpt-6-luna-openai"], "gpt-6-luna", 10),
    "zz_s235_fresh_judge": (["gpt-6-luna", "gpt-6-luna-openai"], None, 0),
    "zz_s235_openai_mid":  (["gpt-6-luna"], None, 0),
    "zz_s235_gpt41_last":  (["gpt-6-luna"], None, 0),
}
CATALOG = [
    # model_key, label, vendor, provider, api_style, enabled
    ("gpt-4.1", "GPT-4.1 (OpenAI direct)", "openai", "openai", "openai_chat", True),
    ("gpt-6-luna-openai", "GPT-6 Luna (OpenAI API)", "openai", "openai", "openai_chat", True),
    ("gpt-5.6-luna", "GPT-5.6 Luna (Bedrock)", "openai", "bedrock", "converse", True),
    ("gpt-6-luna", "GPT-6 Luna (Bedrock)", "openai", "bedrock", "converse", True),
]


def _migration_body() -> str:
    """The file without its own BEGIN/COMMIT, so the test can run it in a transaction it rolls back."""
    with open(MIGRATION) as f:
        lines = f.read().splitlines()
    return "\n".join(line for line in lines if line.strip() not in ("BEGIN;", "COMMIT;"))


def _seed(c):
    for key, label, vendor, provider, api_style, enabled in CATALOG:
        c.execute("""
            INSERT INTO shared.llm_model_catalog
                (model_key, label, vendor, provider, api_style, price_in_per_mtok, price_out_per_mtok, enabled)
            VALUES (%s, %s, %s, %s, %s, 1, 1, %s)
            ON CONFLICT (model_key) DO UPDATE
               SET provider = EXCLUDED.provider, api_style = EXCLUDED.api_style,
                   price_in_per_mtok = 1, price_out_per_mtok = 1, enabled = EXCLUDED.enabled,
                   blocked_reason = NULL""",
                  (key, label, vendor, provider, api_style, enabled))
    for stage, (model, fallbacks, shadow, pct) in SEED.items():
        c.execute("""
            INSERT INTO shared.llm_role_config
                (stage, role, provider, model_id, account_route, fallback_model_ids, shadow_model_id,
                 shadow_sample_pct)
            VALUES (%s, 'judge', 'openai', %s, 'acc3', %s, %s, %s)""",
                  (stage, model, fallbacks, shadow, pct))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
                            password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    c.execute("SELECT to_regclass('shared.llm_role_config'), to_regclass('shared.llm_model_catalog')")
    assert all(c.fetchone()), "llm_role_config / llm_model_catalog missing — migrations 137/169/170"
    _seed(c)
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _apply(c):
    c.execute(_migration_body())


def _openai_keys(c) -> set:
    c.execute("SELECT model_key FROM shared.llm_model_catalog WHERE provider = 'openai'")
    return {r[0] for r in c.fetchall()}


def test_no_route_uses_an_openai_api_model_except_as_last_fallback(cur):
    _apply(cur)
    _apply(cur)  # idempotent: a second run changes nothing and does not fail
    openai_api = _openai_keys(cur)
    assert openai_api >= {"gpt-4.1", "gpt-6-luna-openai"}
    cur.execute("SELECT stage, model_id, fallback_model_ids, shadow_model_id FROM shared.llm_role_config")
    rows = cur.fetchall()
    assert rows
    for stage, model, fallbacks, shadow in rows:
        fallbacks = list(fallbacks or [])
        assert model not in openai_api, f"{stage}: OpenAI-API primary {model}"
        assert shadow not in openai_api, f"{stage}: OpenAI-API shadow {shadow}"
        assert all(k not in openai_api for k in fallbacks[:-1]), f"{stage}: OpenAI-API not last: {fallbacks}"
        assert "gpt-4.1" not in fallbacks, f"{stage}: gpt-4.1 still in the route"


def test_expected_routes_after_224(cur):
    _apply(cur)
    for stage, (fallbacks, shadow, pct) in EXPECTED.items():
        cur.execute("SELECT fallback_model_ids, shadow_model_id, shadow_sample_pct, updated_by "
                    "FROM shared.llm_role_config WHERE stage = %s", (stage,))
        got_fb, got_shadow, got_pct, updated_by = cur.fetchone()
        assert (list(got_fb), got_shadow, got_pct) == (fallbacks, shadow, pct), stage
        if stage != "zz_s235_live_judge":
            assert updated_by == "migration-224-s235"


def test_catalog_after_224(cur):
    _apply(cur)
    cur.execute("SELECT model_key, enabled, blocked_reason FROM shared.llm_model_catalog "
                "WHERE model_key IN ('gpt-4.1', 'gpt-6-luna-openai')")
    rows = {k: (enabled, reason) for k, enabled, reason in cur.fetchall()}
    assert rows["gpt-4.1"][0] is False
    assert rows["gpt-4.1"][1].startswith("S235: retired")
    assert rows["gpt-6-luna-openai"] == (True, None)  # the last-resort fallback stays callable
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '224'")
    assert cur.fetchone() is not None


@pytest.mark.parametrize("column,value", [("model_id", "gpt-6-luna-openai"),
                                          ("shadow_model_id", "gpt-6-luna-openai")])
def test_openai_api_primary_or_shadow_makes_224_fail(cur, column, value):
    cur.execute(f"UPDATE shared.llm_role_config SET {column} = %s WHERE stage = 'zz_s235_live_judge'",
                (value,))
    with pytest.raises(psycopg2.errors.RaiseException, match="primary/shadow"):
        _apply(cur)
