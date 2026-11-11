"""AA-1080 — the Brand Identity POST's INSERT (admin_pipeline._BRAND_INSERT_SQL) writes the signature words on the
tenant role (CIS 397 grant):

  * as the tenant role with its own app.tenant_id: a sent list lands under signature_words and keeps the previous
    version's other keys (voice_rubric); NULL (not sent) carries the previous value over; a first version or a
    non-object previous value gives {"signature_words": [...]};
  * RLS still refuses a version for another tenant.
Invented tenant ids and words only.
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

TENANT_ROLE = "aa_app_user"
BRAND = "shared.tenant_brand_rules"


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c) -> str:
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) "
              "VALUES (%s, 'Quillfern Journeys', %s, 3)", (tid, f"aa1080-{tid[:8]}"))
    return tid


def _seed_version(c, tenant, version, custom_validators):
    c.execute(f"""INSERT INTO {BRAND} (tenant_id, brand_name, version, is_active, updated_at, custom_validators)
                  VALUES (%s, 'default', %s, true, now(), %s::jsonb)""",
              (tenant, version, None if custom_validators is None else json.dumps(custom_validators)))


def _tenant_insert(c, tenant, guc, version, words):
    """The POST's INSERT, prepared with its own $n parameters, run as the tenant role."""
    from api.routers import admin_pipeline
    c.execute("SAVEPOINT aa1080")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        c.execute(f"UPDATE {BRAND} SET is_active = false WHERE tenant_id = %s", (tenant,))   # the POST's deactivate
        c.execute("PREPARE aa1080_ins AS " + admin_pipeline._BRAND_INSERT_SQL)
        c.execute("EXECUTE aa1080_ins (%s, 'ctx', 'style', '[]', %s, 'default', 'kind', 'idea', 'seg', 'mind', "
                  "'[]', 'good', ARRAY['UK']::text[], 'Get in touch.', %s)",
                  (tenant, version, None if words is None else json.dumps(words)))
        c.execute("DEALLOCATE aa1080_ins")
        c.execute("RESET ROLE")
        c.execute("RELEASE SAVEPOINT aa1080")
    except Exception:
        c.execute("ROLLBACK TO SAVEPOINT aa1080")
        c.execute("DEALLOCATE ALL")
        raise


def _validators(c, tenant, version):
    c.execute(f"SELECT custom_validators AS cv FROM {BRAND} WHERE tenant_id = %s AND version = %s", (tenant, version))
    return c.fetchone()[0]


def test_tenant_insert_writes_signature_words_and_keeps_other_keys(cur):
    tenant = _tenant(cur)
    _seed_version(cur, tenant, 1, {"voice_rubric": {"profile": "calm"}, "signature_words": ["old word"]})
    _tenant_insert(cur, tenant, tenant, 2, ["slow road", "maker visit"])
    assert _validators(cur, tenant, 2) == {"voice_rubric": {"profile": "calm"},
                                           "signature_words": ["slow road", "maker visit"]}


def test_tenant_insert_without_the_field_carries_the_previous_value_over(cur):
    tenant = _tenant(cur)
    _seed_version(cur, tenant, 1, {"signature_words": ["kiln morning"]})
    _tenant_insert(cur, tenant, tenant, 2, None)
    assert _validators(cur, tenant, 2) == {"signature_words": ["kiln morning"]}


def test_tenant_insert_on_a_first_version_or_a_non_object(cur):
    tenant = _tenant(cur)
    _tenant_insert(cur, tenant, tenant, 1, ["fjell walk"])
    assert _validators(cur, tenant, 1) == {"signature_words": ["fjell walk"]}
    other = _tenant(cur)
    _seed_version(cur, other, 1, ["a legacy list"])
    _tenant_insert(cur, other, other, 2, [])
    assert _validators(cur, other, 2) == {"signature_words": []}
    _tenant_insert(cur, other, other, 3, None)
    assert _validators(cur, other, 3) == {"signature_words": []}


def test_rls_still_refuses_a_version_for_another_tenant(cur):
    mine, theirs = _tenant(cur), _tenant(cur)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="row-level security"):
        _tenant_insert(cur, theirs, mine, 1, ["x"])
