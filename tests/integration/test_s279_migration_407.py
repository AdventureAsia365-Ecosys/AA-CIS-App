"""S279 thresholds L1 — CIS 407 shared.setting_definition + shared.setting_value on real Postgres (conftest replays
every migration).

The registration and the seed (one row per compiled default, equal values), the definition CHECKs (a default outside
its bounds, an enum default not allowed), the value trigger (insert = a proposal of a known key whose value fits the
definition; evidence when required; a tenant row only for a platform+tenant key; only the decision columns change;
maker_checker needs a second person or a recorded self-approval; a platform row is never deleted, a tenant row goes
with its tenant), one open proposal per key and scope, RLS (ENABLE + FORCE, the X1 parity policies) and cis_app's
grants. The service and the reader are tested in the code PR. Invented values only; every test runs in a transaction
that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from scripts.settings import gen_compiled_defaults as gen  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "00000000-0000-4000-8000-00000000a279"
KEY = "cis.publish.daily_limit_default"          # int 1..1000, maker_checker, platform
TKEY = "cis.zz_s279.tenant_cap"                   # a test definition with per-tenant values (inserted here)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _propose(c, key=KEY, value=25, *, version=1, tenant=None, evidence=None, by="admin:zz-a"):
    return await c.fetchval(
        "INSERT INTO shared.setting_value (key, tenant_id, value, version, proposed_by, reason, evidence_ref) "
        "VALUES ($1, $2::uuid, $3::jsonb, $4, $5, $6, $7) RETURNING id", key, tenant, json.dumps(value), version, by,
        "zz test: a reason of at least twenty characters", evidence)


async def _refused(c, sql, *args, exc=asyncpg.exceptions.CheckViolationError):
    with pytest.raises(exc):
        async with c.transaction():                        # a savepoint: the test goes on after the error
            await c.execute(sql, *args)


async def _tenant_definition(c):
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) "
                    "VALUES ($1::uuid, 'Zz Settings Travel', 'zz-settings-travel', 3)", TENANT)
    await c.execute(
        "INSERT INTO shared.setting_definition (key, app, area, label, description, value_type, unit, min_value, "
        "max_value, default_value, scope_kind, impact, approval) VALUES ($1, 'cis', 'zz', 'Zz tenant cap', "
        "'A test definition with per-tenant values.', 'int', 'calls', 0, 10, '5', 'platform+tenant', 'low', "
        "'single')", TKEY)


@pytest.mark.asyncio
async def test_407_registered_and_seeded_like_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '407'") == "407"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value::text AS d, value_type, min_value::float AS lo, max_value::float AS hi, enforced, "
        "env_override FROM shared.setting_definition")}
    seed = {r["key"]: r for r in gen.seed_rows()[0]}
    assert set(rows) == set(seed)
    for k, r in rows.items():
        assert json.loads(r["d"]) == seed[k]["default_value"], k
        assert r["enforced"] is seed[k]["enforced"] and r["env_override"] == seed[k]["env_override"], k
        assert r["lo"] == seed[k]["min_value"] and r["hi"] == seed[k]["max_value"], k
    assert await conn.fetchval("SELECT count(*) AS ok FROM shared.setting_value") == 0


@pytest.mark.asyncio
async def test_definition_checks(conn):
    base = ("INSERT INTO shared.setting_definition (key, app, area, label, description, value_type, min_value, "
            "max_value, allowed_values, default_value, impact, approval) VALUES ($1, 'cis', 'zz', 'Zz label', "
            "'A test definition description.', $2, $3, $4, $5::jsonb, $6::jsonb, 'low', 'single')")
    await _refused(conn, base, "cis.zz.out", "int", 0, 10, None, "11")          # default above the maximum
    await _refused(conn, base, "cis.zz.whole", "numeric", 5, 1, None, "3")       # min above max
    await _refused(conn, base, "cis.zz.enum", "enum", None, None, '["a", "b"]', '"c"')
    await _refused(conn, base, "booking.zz.app", "int", None, None, None, "1")   # key prefix is not the app
    await _refused(conn, base, "cis.Bad", "int", None, None, None, "1")
    await conn.execute(base, "cis.zz.ok", "int", 0, 10, None, "10")


@pytest.mark.asyncio
async def test_a_value_must_fit_its_definition(conn):
    ins = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) VALUES ($1, $2::jsonb, 1, "
           "'admin:zz', 'zz test: a reason of at least twenty characters')")
    await _refused(conn, ins, KEY, "0")                       # below the minimum 1
    await _refused(conn, ins, KEY, "1001")                    # above the maximum 1000
    await _refused(conn, ins, KEY, "2.5")                     # not a whole number
    await _refused(conn, ins, KEY, '"20"')                    # not a number
    await _refused(conn, ins, "cis.billing.rewrite_over_quota_mode", '"block"')
    await _refused(conn, ins, "cis.settings.two_person_approval", "1")
    await _refused(conn, ins, "cis.spend.dfs_call_estimate_usd", '{"serp_advanced": 2}')   # an entry above 1 USD
    await _refused(conn, ins, "cis.zz.unknown", "1", exc=asyncpg.exceptions.ForeignKeyViolationError)
    await _refused(conn, "INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) VALUES ($1, "
                         "'5', 1, 'admin:zz', 'too short')", KEY)
    await _refused(conn, "INSERT INTO shared.setting_value (key, value, version, status, proposed_by, reason, "
                         "decided_by, decided_at) VALUES ($1, '5', 1, 'approved', 'admin:zz', "
                         "'zz test: a reason of at least twenty characters', 'admin:zz', now())", KEY)
    # evidence_required: the measured ratio needs an eval / replay reference
    await _refused(conn, ins, "cis.t7.blog_chars_per_seed_word", "3.364")
    assert await _propose(conn, "cis.t7.blog_chars_per_seed_word", 3.364, evidence="replay:zz-s279") is not None
    assert await _propose(conn, KEY, 25) is not None


@pytest.mark.asyncio
async def test_decisions_and_immutability(conn):
    vid = await _propose(conn)
    # one open proposal per key and scope
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():
            await _propose(conn, version=2)
    # maker_checker: the proposer may not approve unless the self-approval is recorded
    await _refused(conn, "UPDATE shared.setting_value SET status = 'approved', decided_by = proposed_by, "
                         "decided_at = now() WHERE id = $1", vid)
    await conn.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = proposed_by, "
                       "decided_at = now(), self_approved = true WHERE id = $1", vid)
    # only the decision columns change; a decided row is not decided again
    await _refused(conn, "UPDATE shared.setting_value SET value = '30' WHERE id = $1", vid)
    await _refused(conn, "UPDATE shared.setting_value SET reason = 'zz another reason, long enough' WHERE id = $1",
                   vid)
    await _refused(conn, "UPDATE shared.setting_value SET status = 'rejected', decision_note = 'zz no' "
                         "WHERE id = $1", vid)
    v2 = await _propose(conn, value=30, version=2, by="admin:zz-b")
    await conn.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-a', "
                       "decided_at = now() WHERE id = $1", v2)
    await conn.execute("UPDATE shared.setting_value SET status = 'superseded' WHERE id = $1", vid)
    await _refused(conn, "UPDATE shared.setting_value SET status = 'approved' WHERE id = $1", vid)
    # a rejection says why
    v3 = await _propose(conn, value=40, version=3)
    await _refused(conn, "UPDATE shared.setting_value SET status = 'rejected', decided_by = 'admin:zz-b', "
                         "decided_at = now() WHERE id = $1", v3)
    await conn.execute("UPDATE shared.setting_value SET status = 'rejected', decided_by = 'admin:zz-b', "
                       "decided_at = now(), decision_note = 'zz not now' WHERE id = $1", v3)
    # the version is unique per key and scope
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():
            await _propose(conn, value=41, version=3)
    # a platform row is never deleted
    await _refused(conn, "DELETE FROM shared.setting_value WHERE id = $1", vid)


@pytest.mark.asyncio
async def test_tenant_rows_only_for_tenant_scoped_keys_and_go_with_the_tenant(conn):
    await _tenant_definition(conn)
    await _refused(conn, "INSERT INTO shared.setting_value (key, tenant_id, value, version, proposed_by, reason) "
                         "VALUES ($1, $2::uuid, '5', 1, 'admin:zz', 'zz test: a reason of at least twenty "
                         "characters')", KEY, TENANT)
    tid = await _propose(conn, TKEY, 7, tenant=TENANT)
    pid = await _propose(conn, TKEY, 6)                      # the platform row of the same key is its own scope
    assert tid and pid
    await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", TENANT)
    assert await conn.fetchval("SELECT count(*) AS ok FROM shared.setting_value WHERE key = $1", TKEY) == 1


@pytest.mark.asyncio
async def test_rls_and_grants(conn):
    rows = await conn.fetch(
        "SELECT c.relname AS name, c.relrowsecurity AS rls, c.relforcerowsecurity AS force FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'shared' "
        "AND c.relname IN ('setting_definition', 'setting_value')")
    assert {r["name"]: (r["rls"], r["force"]) for r in rows} == {
        "setting_definition": (True, True), "setting_value": (True, True)}
    policies = {r["policyname"] for r in await conn.fetch(
        "SELECT policyname FROM pg_policies WHERE schemaname = 'shared' AND tablename LIKE 'setting_%'")}
    assert policies == {"setting_definition_cis_app_all", "setting_definition_cis_owner_all",
                        "setting_value_cis_app_all", "setting_value_cis_owner_all"}
    grants = {(r["table_name"], r["privilege_type"]) for r in await conn.fetch(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = 'cis_app' "
        "AND table_schema = 'shared' AND table_name LIKE 'setting_%'")}
    assert grants == {("setting_definition", "SELECT"), ("setting_value", "SELECT"), ("setting_value", "INSERT"),
                      ("setting_value", "UPDATE"), ("setting_value", "DELETE")}
