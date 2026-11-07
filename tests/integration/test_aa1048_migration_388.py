"""AA-1048 (Quality W2, root ADR 0020) — CIS 388 shared.eval_claim_label + the 'golden_claim' disagreement source, on
real Postgres (conftest replays every migration).

The table, its CHECKs (set id / version shape, writer and scope, the object ref, sha shapes, the span length rule,
class / label / labeller / adjudication values), the one judgement per (set, version, claim, labeller kind, lens) with
a NULL lens counted as a value, the pii:free_text tags, RLS (ENABLE + FORCE, the X1 parity policies and
tenant_isolation), cis_app's grants (SELECT, INSERT, DELETE; never UPDATE), the new queue source, the registration,
and that re-applying the file is a no-op.

Invented values only; every row of this module has set id ZZ-AA1048 or tenant TENANT and is deleted at the end.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "00000000-0000-4000-8000-000000a10480"
SET = "ZZ-AA1048"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "388_eval_claim_label.sql")
CLAIM = "Lunch at the Velmora lakeside camp."
COLUMNS = ("set_id", "set_version", "writer", "scope", "tenant_id", "sample_id", "piece_id", "content_sha256", "field",
           "span_start", "span_end", "claim_text", "claim_hash", "claim_class", "label", "labeller_kind", "labeller",
           "lens", "split", "stratum", "adjudication")


def _migration() -> str:
    with open(MIGRATION, encoding="utf-8") as f:
        return f.read()


def _row(**over) -> dict:
    row = {"set_id": SET, "set_version": "1.0.0", "writer": "t9_blog", "scope": "tenant", "tenant_id": TENANT,
           "sample_id": None, "piece_id": str(uuid.uuid4()), "content_sha256": "a" * 64, "field": "body",
           "span_start": 10, "span_end": 10 + len(CLAIM), "claim_text": CLAIM, "claim_hash": "b" * 64,
           "claim_class": "fact.inclusion", "label": "violation", "labeller_kind": "agent_panel",
           "labeller": "panel:zz-run", "lens": "fidelity", "split": "train", "stratum": "hard",
           "adjudication": "agreed"}
    row.update(over)
    return row


async def _insert(conn, **over):
    row = _row(**over)
    cols = ", ".join(COLUMNS)
    args = ", ".join(f"${i + 1}" + ("::uuid" if c in ("tenant_id", "piece_id") else "")
                     for i, c in enumerate(COLUMNS))
    return await conn.fetchval(f"INSERT INTO shared.eval_claim_label ({cols}) VALUES ({args}) RETURNING label_id",
                               *[row[c] for c in COLUMNS])


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    await c.execute(_migration())           # idempotent: a test before this one may have dropped the table
    await c.execute("DELETE FROM shared.eval_claim_label WHERE set_id = $1 OR tenant_id = $2::uuid", SET, TENANT)
    yield c
    await c.execute("DELETE FROM shared.eval_claim_label WHERE set_id = $1 OR tenant_id = $2::uuid", SET, TENANT)
    await c.execute("DELETE FROM shared.writing_memory_disagreement WHERE tenant_id = $1::uuid", TENANT)
    await c.close()


@pytest.mark.asyncio
async def test_388_registered_and_one_judgement_per_lens(conn):
    assert await conn.fetchval("SELECT version FROM shared.schema_versions WHERE version = '388'") == "388"
    first = await _insert(conn)
    assert isinstance(first, uuid.UUID)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):      # same lens again
        await _insert(conn)
    assert await _insert(conn, lens="promise")                          # another lens of the panel
    assert await _insert(conn, set_version="1.1.0")                     # the same claim in the next version
    final = dict(labeller_kind="tech_lead", labeller="tech_lead", lens=None, adjudication="final", label="ok")
    assert await _insert(conn, **final)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):      # a NULL lens is one value (NULLS NOT DISTINCT)
        await _insert(conn, **final)
    row = await conn.fetchrow("SELECT policy_sha, reason, tour_id, version_id FROM shared.eval_claim_label "
                              "WHERE label_id = $1", first)
    assert dict(row) == {"policy_sha": None, "reason": None, "tour_id": None, "version_id": None}
    default = await conn.fetchval("SELECT column_default FROM information_schema.columns WHERE table_schema = "
                                  "'shared' AND table_name = 'eval_claim_label' AND column_name = 'stratum'")
    assert "representative" in default
    await conn.execute(_migration())          # re-apply: no-op
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.eval_claim_label WHERE set_id = $1", SET) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"set_id": "-bad"}, {"set_version": "1.0"}, {"writer": "t5"},
    {"scope": "platform"},                                      # platform with a tenant
    {"writer": "a1", "scope": "platform", "tenant_id": None},   # fine scope, but next rule: a1 is platform-only (ok)
    {"piece_id": None},                                         # no object ref at all
    {"sample_id": "Bad_Id"}, {"content_sha256": "x"}, {"claim_hash": "c" * 63}, {"field": "Body"},
    {"span_end": 10 + len(CLAIM) + 1},                          # span length differs from the claim text
    {"span_start": -1, "span_end": len(CLAIM) - 1},
    {"claim_class": "inclusion"}, {"label": "fail"}, {"labeller_kind": "human"},
    {"lens": None},                                             # a panel row needs its lens
    {"adjudication": "final"},                                  # final is the tech lead's
    {"split": "holdout"}, {"stratum": "mixed"},
])
async def test_388_checks(conn, over):
    if over.get("writer") == "a1":
        assert await _insert(conn, **over)                      # the control case: a valid platform row
        return
    extra = {}
    if over.get("piece_id", "x") is None:
        extra = {"sample_id": None}
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _insert(conn, **{**over, **extra})


@pytest.mark.asyncio
async def test_388_tags_rls_and_policies(conn):
    tags = dict(await conn.fetch(
        "SELECT a.attname AS col, col_description('shared.eval_claim_label'::regclass, a.attnum) AS tag "
        "FROM pg_attribute a WHERE a.attrelid = 'shared.eval_claim_label'::regclass AND a.attnum > 0"))
    assert tags["claim_text"].startswith("pii:free_text;") and tags["reason"].startswith("pii:free_text;")
    rls = await conn.fetchrow("SELECT relrowsecurity AS on_, relforcerowsecurity AS force FROM pg_class "
                              "WHERE oid = 'shared.eval_claim_label'::regclass")
    assert rls["on_"] and rls["force"]
    policies = {r["policyname"]: r for r in await conn.fetch(
        "SELECT policyname, roles::text[] AS roles, qual FROM pg_policies "
        "WHERE schemaname = 'shared' AND tablename = 'eval_claim_label'")}
    assert set(policies) == {"eval_claim_label_cis_app_all", "eval_claim_label_cis_owner_all", "tenant_isolation"}
    assert policies["tenant_isolation"]["roles"] == ["aa_app_user"]
    assert "NULLIF" in policies["tenant_isolation"]["qual"] and "scope" in policies["tenant_isolation"]["qual"]


@pytest.mark.asyncio
async def test_388_cis_app_grants_and_the_dev_default_privileges(conn):
    """On Dev Migration Apply creates the table as cis_owner, whose default privileges in shared give cis_app every
    privilege on a new table. Simulate them (and a PUBLIC / tenant pool grant), re-run the file in a rolled-back
    transaction: cis_app keeps SELECT, INSERT, DELETE; the others keep nothing."""
    n = await conn.fetchval("SELECT count(*) AS n FROM pg_roles WHERE rolname IN ('cis_app', 'aa_app_user', "
                            "'catalog_owner', 'cis_owner')")
    if n != 4:
        pytest.skip("the runtime roles are not in this database")
    body = _migration().replace("BEGIN;", "").replace("COMMIT;", "")
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute("GRANT ALL ON shared.eval_claim_label TO cis_app, aa_app_user, catalog_owner, PUBLIC")
        await conn.execute(body)
        for priv, want in (("SELECT", True), ("INSERT", True), ("DELETE", True), ("UPDATE", False),
                           ("TRUNCATE", False), ("REFERENCES", False), ("TRIGGER", False)):
            got = await conn.fetchval("SELECT has_table_privilege('cis_app', 'shared.eval_claim_label', $1) AS ok",
                                      priv)
            assert got is want, priv
        for role in ("aa_app_user", "catalog_owner", "public"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                got = await conn.fetchval("SELECT has_table_privilege($1, 'shared.eval_claim_label', $2) AS ok",
                                          role, priv)
                assert got is False, (role, priv)
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_388_golden_claim_queue_source(conn):
    insert = ("INSERT INTO shared.writing_memory_disagreement (source, source_ref, writer, scope, tenant_id, "
              "machine_verdict, human_label, week_start) VALUES ($1, $2::uuid, 't9_blog', 'tenant', $3::uuid, "
              "'violation', 'disputed', DATE '2026-11-02') RETURNING item_id")
    assert await conn.fetchval(insert, "golden_claim", str(uuid.uuid4()), TENANT)
    assert await conn.fetchval(insert, "golden", str(uuid.uuid4()), TENANT)            # 314 values still pass
    assert await conn.fetchval(insert, "piece_label", str(uuid.uuid4()), TENANT)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.fetchval(insert, "claim", str(uuid.uuid4()), TENANT)
    await conn.execute(_migration())      # re-apply keeps the one constraint
    n = await conn.fetchval("SELECT count(*) AS n FROM pg_constraint WHERE conrelid = "
                            "'shared.writing_memory_disagreement'::regclass AND conname = "
                            "'writing_memory_disagreement_source_check'")
    assert n == 1
