"""AA-803 (S246) — migration 259 (Haiku 5.5 / Sonnet 5.5 catalog rows; 'sonnet' drops its LEGACY acc2
profile) against real Postgres.

conftest applies every migration (169 seeds 'sonnet' with the Sonnet 4.5 acc2 profile, 259 adds the rows
and removes it). These tests run the file again directly, errors NOT swallowed: from the pre-259 state,
twice (idempotent), and each guard. Every test restores the post-replay state: 259, then 295 (AA-998), which
removes the acc1 profiles 259 writes — so a test that re-runs 259 first undoes it (the 259 guard would refuse the
post-295 rows).
"""
import os
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "259_llm_catalog_haiku_sonnet_5_5.sql")
MIGRATION_295 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "295_llm_acc3_only.sql")
NEW = ("haiku-5-5", "sonnet-5-5")
SONNET45_ACC2 = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor()
    # _undo deletes the catalog rows, which cascades to their deployments (304 / 306); keep them to put back (AA-1026:
    # migration 335 and its test need the haiku-5-5 / sonnet-5-5 acc3 deployments the replay made).
    c.execute("SELECT to_jsonb(d)::text FROM shared.llm_model_deployment d WHERE d.model_key = ANY (%s)", (list(NEW),))
    deployments = [r[0] for r in c.fetchall()]
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        _undo(c)
        _run(c)                                 # back to the post-259 state ...
        c.execute(open(MIGRATION_295, encoding="utf-8").read())   # ... and the post-295 one (AA-998)
        for row in deployments:
            c.execute("INSERT INTO shared.llm_model_deployment SELECT (jsonb_populate_record("
                      "NULL::shared.llm_model_deployment, %s::jsonb)).* ON CONFLICT DO NOTHING", (row,))
        conn.close()


def _undo(c):
    """The pre-259 state: no new rows, 'sonnet' still names the Sonnet 4.5 acc2 profile."""
    c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = ANY (%s)", (list(NEW),))
    c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids || "
              "jsonb_build_object('acc2', %s::text), label = 'Claude Sonnet 4.5 / 4.6' "
              "WHERE model_key = 'sonnet'", (SONNET45_ACC2,))
    c.execute("DELETE FROM shared.schema_versions WHERE version = '259'")


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


def _row(c, key):
    c.execute("SELECT bedrock_profile_ids, vendor, provider, api_style, callable_via, supports_temperature, "
              "max_output_tokens, price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok, "
              "price_cache_write_per_mtok, price_source, enabled, blocked_reason, label, updated_by "
              "FROM shared.llm_model_catalog WHERE model_key = %s", (key,))
    r = c.fetchone()
    if r is None:
        return None
    cols = ("profiles", "vendor", "provider", "api_style", "callable_via", "supports_temperature",
            "max_output_tokens", "price_in", "price_out", "cache_read", "cache_write", "price_source",
            "enabled", "blocked_reason", "label", "updated_by")
    return dict(zip(cols, r))


@pytest.mark.parametrize("key,prices", [
    ("haiku-5-5", (0.10, 0.50, 0.01, 0.125)),
    ("sonnet-5-5", (2.00, 10.00, 0.20, 2.50)),
])
def test_new_rows_from_pre_259_state(cur, key, prices):
    _undo(cur)
    _run(cur)
    r = _row(cur, key)
    profile = f"global.anthropic.claude-{key}"
    # what 259 itself writes (no acc2: it refuses Claude, AA-843); 295 later removes acc1 (AA-998)
    assert r["profiles"] == {"acc3": profile, "acc1": profile}
    assert (r["vendor"], r["provider"], r["api_style"]) == ("anthropic", "bedrock", "converse")
    assert r["callable_via"] == ["llm_client"]
    assert r["supports_temperature"] is False                   # 400 on a non-default temperature
    assert r["max_output_tokens"] == 128000
    assert tuple(float(v) for v in (r["price_in"], r["price_out"], r["cache_read"], r["cache_write"])) == \
        pytest.approx(prices)
    assert "Bedrock pricing page" in r["price_source"]
    assert r["enabled"] is True and r["blocked_reason"] is None
    assert r["updated_by"] == "migration-259-aa803"


def test_new_rows_are_on_no_route(cur):
    _undo(cur)
    _run(cur)
    cur.execute("SELECT stage, updated_by FROM shared.llm_role_config WHERE model_id = ANY (%s) "
                "OR shadow_model_id = ANY (%s) OR fallback_model_ids && %s::text[]",
                (list(NEW), list(NEW), list(NEW)))
    rows = cur.fetchall()
    # 259 routes no stage; the later migrations that route one are listed here: 282 (S251, supply_content_writer) and
    # 335 (AA-1026, every route that named the legacy haiku / sonnet keys, supply_content_writer included).
    assert "supply_content_writer" in {r[0] for r in rows}
    assert {r[1] for r in rows} <= {"migration-282-aa835", "migration-335-aa1026"}


def test_sonnet_drops_the_legacy_acc2_profile_only(cur):
    _undo(cur)
    cur.execute("SELECT bedrock_profile_ids FROM shared.llm_model_catalog WHERE model_key = 'sonnet'")
    before = cur.fetchone()[0]
    assert before["acc2"] == SONNET45_ACC2
    _run(cur)
    r = _row(cur, "sonnet")
    assert r["profiles"] == {k: v for k, v in before.items() if k != "acc2"}
    assert "acc3" in r["profiles"]
    assert r["label"] == "Claude Sonnet 4.6"
    assert r["updated_by"] == "migration-259-aa803"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '259'")
    assert cur.fetchone()


def test_idempotent(cur):
    _undo(cur)
    _run(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET updated_by = 'after-first-run' "
                "WHERE model_key = ANY (%s) OR model_key = 'sonnet'", (list(NEW),))
    _run(cur)
    cur.execute("SELECT model_key, updated_by FROM shared.llm_model_catalog "
                "WHERE model_key = ANY (%s) OR model_key = 'sonnet'", (list(NEW),))
    assert {k: u for k, u in cur.fetchall()} == {k: "after-first-run" for k in (*NEW, "sonnet")}


def test_guard_hand_edited_sonnet_acc2(cur):
    _undo(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids || "
                "'{\"acc2\": \"us.anthropic.claude-sonnet-4-6\"}'::jsonb WHERE model_key = 'sonnet'")
    with pytest.raises(psycopg2.Error, match="259: sonnet acc2 profile is us.anthropic.claude-sonnet-4-6"):
        _run(cur)


def test_guard_existing_row_with_other_profiles(cur):
    _undo(cur)
    _run(cur)
    cur.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = "
                "'{\"acc3\": \"global.anthropic.claude-haiku-5-5\"}'::jsonb WHERE model_key = 'haiku-5-5'")
    with pytest.raises(psycopg2.Error, match="259: haiku-5-5 already exists with bedrock_profile_ids"):
        _run(cur)


def test_guard_missing_sonnet_row(cur):
    cur.execute("BEGIN")
    # 307's alias rows reference the catalog row (ON DELETE RESTRICT); the rollback restores them too
    cur.execute("DELETE FROM shared.llm_model_alias WHERE model_key = 'sonnet'")
    cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'sonnet'")
    with pytest.raises(psycopg2.Error, match="259: shared.llm_model_catalog has no sonnet row"):
        _run(cur)
    # fixture ROLLBACK restores the row
