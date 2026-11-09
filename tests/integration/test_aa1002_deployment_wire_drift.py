"""AA-1002 (gateway slice S3) — standing guard: deployment wire ids stay in step with the catalog.

With ``LLM_DEPLOYMENT_ROUTING`` on, CIS sends ``shared.llm_model_deployment.wire_id`` on the wire, while TripPlanner
(until slice S8) still reads ``shared.llm_model_catalog.bedrock_profile_ids`` / ``wire_model``. Migration 304 checked
the two agree when it ran; nothing keeps them agreeing afterwards. A later catalog migration (a model upgrade like 227)
that changes a profile but forgets the deployment row would make CIS keep calling the old profile while TripPlanner
calls the new one, with no error. conftest applies EVERY migration, so these checks run against the state the newest
migration leaves and fail CI on such a migration.

Rules (enabled deployments on enabled-or-not connections; the DB here holds only migration rows, no admin edits):
- Bedrock connection: ``wire_id`` = ``bedrock_profile_ids ->> connection_key``; an enabled Bedrock deployment whose
  catalog row has no profile for that connection is drift too (TripPlanner cannot call it there);
- legacy rows (origin ``legacy_constant``, enabled or not): ``wire_id`` = the code constant the legacy chain invokes
  (``bedrock_satellite._PROFILE_ID_*`` on acc3, ``pricing.BEDROCK_*`` on acc2);
- OpenAI connection: ``wire_id`` = ``COALESCE(wire_model, model_key)`` (what ``_call_openai`` sent before S3);
- Anthropic connection, seed rows: ``wire_id`` = the acc3 profile without its region prefix and version suffix
  (306's derivation; AA-1042 / CIS 376: a bare ``-v1`` suffix too, as ``global.anthropic.claude-opus-4-6-v1`` is the
  API's ``claude-opus-4-6``), so a catalog upgrade of the acc3 profile cannot leave the API row on the old model; a
  row reached only by the Anthropic API (no acc3 profile, CIS 376) calls its catalog ``wire_model``.
"""
import os
import sys

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import bedrock_satellite as bs  # noqa: E402
from shared.llm_client import pricing  # noqa: E402

_FIX = ("A migration changed a catalog profile / wire_model without the matching shared.llm_model_deployment row "
        "(or the reverse): update both in the same migration")


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    try:
        yield conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    finally:
        conn.close()


def _rows(cur, sql: str) -> list[str]:
    cur.execute(sql)
    return [r["drift"] for r in cur.fetchall()]


def test_the_registry_is_populated(cur):
    """The checks below are vacuous on an empty table: the migrations must have seeded every kind they check."""
    cur.execute("SELECT c.kind, count(*) AS n FROM shared.llm_model_deployment d "
                "JOIN shared.llm_provider_connection c USING (connection_key) WHERE d.enabled GROUP BY c.kind")
    kinds = {r["kind"]: r["n"] for r in cur.fetchall()}
    assert kinds.get("bedrock", 0) >= 5 and kinds.get("openai", 0) >= 1 and kinds.get("anthropic", 0) >= 1, kinds


def test_bedrock_deployments_call_the_catalog_profile(cur):
    drift = _rows(cur, """
        SELECT d.model_key || '@' || d.connection_key || ': deployment ' || d.wire_id || ' vs catalog '
               || COALESCE(m.bedrock_profile_ids ->> d.connection_key, '<no profile>') AS drift
          FROM shared.llm_model_deployment d
          JOIN shared.llm_provider_connection c USING (connection_key)
          JOIN shared.llm_model_catalog m USING (model_key)
         WHERE c.kind = 'bedrock' AND d.enabled
           AND d.wire_id IS DISTINCT FROM NULLIF(m.bedrock_profile_ids ->> d.connection_key, '')
         ORDER BY 1""")
    assert drift == [], f"{_FIX}: {drift}"


def test_legacy_rows_call_the_chain_constants(cur):
    expected = {
        ("haiku", "acc3"): bs._PROFILE_ID_HAIKU,
        ("sonnet", "acc3"): bs._PROFILE_ID_SONNET,
        ("haiku", "acc2"): pricing.BEDROCK_HAIKU,
        ("sonnet", "acc2"): pricing.BEDROCK_SONNET,
    }
    cur.execute("SELECT model_key, connection_key, wire_id FROM shared.llm_model_deployment "
                "WHERE origin = 'legacy_constant'")
    got = {(r["model_key"], r["connection_key"]): r["wire_id"] for r in cur.fetchall()}
    assert got == expected


def test_openai_deployments_call_the_catalog_wire_model(cur):
    drift = _rows(cur, """
        SELECT d.model_key || '@' || d.connection_key || ': deployment ' || d.wire_id || ' vs catalog '
               || COALESCE(m.wire_model, m.model_key) AS drift
          FROM shared.llm_model_deployment d
          JOIN shared.llm_provider_connection c USING (connection_key)
          JOIN shared.llm_model_catalog m USING (model_key)
         WHERE c.kind = 'openai' AND d.enabled
           AND d.wire_id IS DISTINCT FROM COALESCE(m.wire_model, m.model_key)
         ORDER BY 1""")
    assert drift == [], f"{_FIX}: {drift}"


def test_anthropic_api_seed_rows_follow_the_acc3_profile(cur):
    drift = _rows(cur, """
        SELECT d.model_key || '@' || d.connection_key || ': deployment ' || d.wire_id || ' vs acc3 profile '
               || COALESCE(m.bedrock_profile_ids ->> 'acc3', '<no acc3 profile, wire_model '
                           || COALESCE(m.wire_model, '<none>') || '>') AS drift
          FROM shared.llm_model_deployment d
          JOIN shared.llm_provider_connection c USING (connection_key)
          JOIN shared.llm_model_catalog m USING (model_key)
         WHERE c.kind = 'anthropic' AND d.enabled AND d.origin = 'seed'
           AND d.wire_id IS DISTINCT FROM COALESCE(
               regexp_replace(regexp_replace(NULLIF(m.bedrock_profile_ids ->> 'acc3', ''),
                                             '^(global|us|eu|apac|jp|au)\\.anthropic\\.', ''),
                              '-v[0-9]+(:[0-9]+)?$', ''),
               m.wire_model)
         ORDER BY 1""")
    assert drift == [], f"{_FIX}: {drift}"


def test_an_anthropic_api_only_row_names_its_api_model(cur):
    """AA-1042 (CIS 376): a catalog row reached only by the Anthropic API has no Bedrock profile, so its wire_model is
    the model id the API row sends (and TripPlanner's view reads)."""
    drift = _rows(cur, """
        SELECT m.model_key || ': wire_model ' || COALESCE(m.wire_model, '<none>') AS drift
          FROM shared.llm_model_catalog m
         WHERE m.provider = 'anthropic' AND m.api_style = 'anthropic_messages'
           AND (m.wire_model IS NULL OR m.wire_model !~ '^claude-[a-z0-9-]+$' OR m.bedrock_profile_ids <> '{}'::jsonb)
         ORDER BY 1""")
    assert drift == [], f"{_FIX}: {drift}"
