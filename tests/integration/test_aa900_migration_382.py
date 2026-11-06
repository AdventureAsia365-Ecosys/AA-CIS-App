"""AA-900 (S271 lane l11) — migration 382: capabilities.min_output_tokens = 8192 on the catalog rows gpt-6-luna and
gpt-6-luna-openai (the R2 judge cuts at the 4096 code floor).

conftest replays 382 like every file. The module re-applies the file itself (errors are not swallowed; a second run is
a no-op) and checks:
  * both rows state min_output_tokens 8192 (an integer), their other capability keys are kept, updated_by moves;
  * no other catalog row changes;
  * a row that already states a higher floor keeps it (NOTICE), a lower one is raised;
  * a second run changes nothing (updated_at stays);
  * registered once;
  * the guard refuses when a row is missing: nothing changes (the file's transaction rolls back);
  * the gateway's pure rule (shared.aa_llm.tokens.floor_for) reads the stored capabilities as 8192.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import psycopg2  # noqa: E402

from shared.aa_llm import tokens  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "382_llm_gpt6_luna_min_output_tokens.sql")
KEYS = ("gpt-6-luna", "gpt-6-luna-openai")
ACTOR = "migration-382"


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


def _caps(cur, key):
    cur.execute("SELECT capabilities FROM shared.llm_model_catalog WHERE model_key = %s", (key,))
    row = cur.fetchone()
    caps = row[0] if row else None
    return json.loads(caps) if isinstance(caps, str) else caps


def _strip(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = capabilities - 'min_output_tokens' "
                "WHERE model_key = ANY(%s)", (list(KEYS),))


@pytest.fixture(scope="module")
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True
    k = c.cursor()
    k.execute("SELECT count(*) AS n FROM shared.llm_model_catalog WHERE model_key = ANY(%s)", (list(KEYS),))
    if k.fetchone()[0] != len(KEYS):
        pytest.skip("the gpt-6-luna catalog rows are missing (migration 170 did not replay here)")
    _strip(k)
    k.execute(_sql())                                         # errors raise here (conftest swallows its own run)
    k.execute(_sql())                                         # a second run is a no-op
    yield k
    _strip(k)
    k.execute(_sql())                                         # leave the replayed state as conftest made it
    c.close()


def test_both_rows_state_the_floor(cur):
    for key in KEYS:
        caps = _caps(cur, key)
        assert caps["min_output_tokens"] == 8192, (key, caps)
        assert isinstance(caps["min_output_tokens"], int)
    cur.execute("SELECT model_key, updated_by FROM shared.llm_model_catalog WHERE model_key = ANY(%s)", (list(KEYS),))
    assert {r[0]: r[1] for r in cur.fetchall()} == {k: ACTOR for k in KEYS}


def test_other_capability_keys_are_kept(cur):
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = capabilities - 'min_output_tokens' "
                "|| '{\"invented_flag\": true}'::jsonb WHERE model_key = 'gpt-6-luna-openai'")
    cur.execute(_sql())
    caps = _caps(cur, "gpt-6-luna-openai")
    assert caps["invented_flag"] is True and caps["min_output_tokens"] == 8192
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = capabilities - 'invented_flag' "
                "WHERE model_key = 'gpt-6-luna-openai'")


def test_no_other_row_changes(cur):
    cur.execute("SELECT model_key FROM shared.llm_model_catalog WHERE updated_by = %s ORDER BY 1", (ACTOR,))
    assert tuple(r[0] for r in cur.fetchall()) == KEYS
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_catalog "
                "WHERE capabilities ? 'min_output_tokens' AND NOT (model_key = ANY(%s))", (list(KEYS),))
    assert cur.fetchone()[0] == 0


def test_a_higher_floor_is_kept_and_a_lower_one_raised(cur):
    conn = cur.connection
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = capabilities || '{\"min_output_tokens\": 12000}'"
                "::jsonb WHERE model_key = 'gpt-6-luna'")
    cur.execute("UPDATE shared.llm_model_catalog SET capabilities = capabilities || '{\"min_output_tokens\": 2048}'"
                "::jsonb WHERE model_key = 'gpt-6-luna-openai'")
    conn.notices.clear()
    cur.execute(_sql())
    assert _caps(cur, "gpt-6-luna")["min_output_tokens"] == 12000
    assert _caps(cur, "gpt-6-luna-openai")["min_output_tokens"] == 8192
    assert any("382" in n and "gpt-6-luna" in n and "keeps" in n for n in conn.notices), conn.notices
    _strip(cur)
    cur.execute(_sql())
    assert _caps(cur, "gpt-6-luna")["min_output_tokens"] == 8192


def test_a_second_run_changes_nothing(cur):
    cur.execute("SELECT model_key, updated_at FROM shared.llm_model_catalog WHERE model_key = ANY(%s)", (list(KEYS),))
    before = dict(cur.fetchall())
    cur.execute(_sql())
    cur.execute("SELECT model_key, updated_at FROM shared.llm_model_catalog WHERE model_key = ANY(%s)", (list(KEYS),))
    assert dict(cur.fetchall()) == before


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '382'")
    assert cur.fetchone()[0] == 1


def test_the_guard_refuses_a_missing_row(cur):
    _strip(cur)
    bad = _sql().replace("ARRAY['gpt-6-luna', 'gpt-6-luna-openai']) AS k",
                         "ARRAY['gpt-6-luna', 'invented-model-zz']) AS k")
    assert bad != _sql()
    with pytest.raises(psycopg2.Error) as err:
        cur.execute(bad)
    assert "invented-model-zz" in str(err.value)
    cur.execute("ROLLBACK")                                   # the file's own BEGIN left the session in a failed block
    for key in KEYS:
        assert "min_output_tokens" not in (_caps(cur, key) or {})
    cur.execute(_sql())
    assert _caps(cur, "gpt-6-luna")["min_output_tokens"] == 8192


def test_the_gateway_rule_reads_the_stored_floor(cur):
    for key in KEYS:
        caps = _caps(cur, key)
        assert tokens.floor_for(key, caps) == 8192
        assert tokens.floored(1200, key, caps) == 8192
        assert tokens.floored(12000, key, caps) == 12000           # a higher stage ceiling is never lowered
    assert tokens.floor_for("gpt-6-astra", {}) == tokens.REASONING_FLOOR    # left on the code floor
