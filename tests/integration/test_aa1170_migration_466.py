"""AA-1170 (S287) — CIS 466: schema compliance (requirement catalogue, controls, mapping, evidence, score snapshots) and
the cis.compliance.* settings (conftest replays 466 with the other files). Registered; the five tables with their keys;
cis_app DML only, nothing for aa_app_user or PUBLIC; the default privileges FOR ROLE cis_owner the isolation probe
expects; the 253 catalogue rows; evidence append-only (UPDATE / DELETE / TRUNCATE refused, a DELETE only under the
retention purge flag) and ingested once per (source, check, time); a mapping needs a known requirement and goes with its
control; the seven settings equal the compiled defaults with a complete guide; a re-run changes nothing. Invented
control ids (EX- prefix). Runs in a transaction that is rolled back."""
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance_settings as keys  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M466 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "466_aa1170_compliance_schema.sql")
TABLES = ("requirement", "control", "requirement_mapping", "evidence", "score_snapshot")


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


async def _refused(conn, error, sql, *args):
    """Run sql in a savepoint and expect `error`; the outer (rolled-back) transaction stays usable."""
    with pytest.raises(error):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _control(conn, control_id="EX-IAM-01"):
    await conn.execute(
        "INSERT INTO compliance.control (control_id, title, domain, owner_role, frequency, test_type, checks) "
        "VALUES ($1, 'Example control', 'access', 'tech lead', 'daily', 'auto', $2::jsonb)",
        control_id, json.dumps([{"check_id": "gh_default_branch_rules", "required": True}]))


async def _evidence(conn, status="pass", at=datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)):
    return await conn.fetchval(
        "INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector, collector_version, detail) "
        "VALUES ('root_daily', 'gh_default_branch_rules', $1, $2::timestamptz, 'compliance-evidence.yml', "
        "'0123456789abcdef0123456789abcdef01234567', '{\"repos\": 6, \"passing\": 5}') RETURNING evidence_id",
        status, at)


@pytest.mark.asyncio
async def test_466_registered_with_the_five_tables(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '466'") == "466"
    rows = await conn.fetch(
        "SELECT c.relname AS t, c.relrowsecurity AS rls FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'compliance' AND c.relkind = 'r'")
    assert {r["t"] for r in rows} == set(TABLES)
    assert not any(r["rls"] for r in rows)                                  # platform data, no tenant column
    pks = {r["t"]: list(r["cols"]) for r in await conn.fetch(
        "SELECT c.conrelid::regclass::text AS t, array_agg(a.attname::text ORDER BY k.ord) AS cols "
        "FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
        "WHERE c.contype = 'p' AND c.connamespace = 'compliance'::regnamespace GROUP BY 1")}
    assert pks["compliance.requirement"] == ["framework", "ref"]
    assert pks["compliance.requirement_mapping"] == ["control_id", "framework", "ref"]
    assert pks["compliance.score_snapshot"] == ["snapshot_date", "framework"]
    assert pks["compliance.evidence"] == ["evidence_id"]


@pytest.mark.asyncio
async def test_grants_and_default_privileges(conn):
    for t in TABLES:
        rel = f"compliance.{t}"
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT, INSERT, UPDATE, DELETE') AS ok",
                                   rel) is True, t
        assert await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT') AS ok", rel) is False, t
    assert await conn.fetchval("SELECT has_schema_privilege('cis_app', 'compliance', 'USAGE') AS ok") is True
    assert await conn.fetchval("SELECT has_schema_privilege('aa_app_user', 'compliance', 'USAGE') AS ok") is False
    assert await conn.fetchval(
        "SELECT count(*) AS n FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'compliance' AND a.grantee = 0") == 0
    defacl = {r["objtype"]: set(r["privs"]) for r in await conn.fetch(
        "SELECT d.defaclobjtype::text AS objtype, array_agg(a.privilege_type::text) AS privs "
        "FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "WHERE n.nspname = 'compliance' AND d.defaclrole = 'cis_owner'::regrole AND a.grantee = 'cis_app'::regrole "
        "GROUP BY 1")}
    assert defacl == {"r": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "S": {"USAGE", "SELECT", "UPDATE"},
                      "f": {"EXECUTE"}}


@pytest.mark.asyncio
async def test_catalogue_is_seeded(conn):
    rows = {r["framework"]: r["n"] for r in await conn.fetch(
        "SELECT framework, count(*) AS n FROM compliance.requirement WHERE applicable GROUP BY 1")}
    assert rows == {"iso27001:2022": 123, "soc2:2017": 33, "iso42001:2023": 70, "owasp-top10:2021": 10,
                    "owasp-llm:2025": 10, "eu-ai-act:2024": 7}
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO compliance.requirement (framework, ref, section, title, sort_order) "
                   "VALUES ('nist:2024', 'X.1', 'Invented', 'Invented row', 1)")


@pytest.mark.asyncio
async def test_mapping_rules(conn):
    await _control(conn)
    await conn.execute("INSERT INTO compliance.requirement_mapping (control_id, framework, ref, coverage) "
                       "VALUES ('EX-IAM-01', 'iso27001:2022', 'A.5.15', 'full')")
    await _refused(conn, asyncpg.ForeignKeyViolationError,                   # an unknown requirement
                   "INSERT INTO compliance.requirement_mapping (control_id, framework, ref, coverage) "
                   "VALUES ('EX-IAM-01', 'iso27001:2022', 'A.9.99', 'full')")
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO compliance.requirement_mapping (control_id, framework, ref, coverage) "
                   "VALUES ('EX-IAM-01', 'soc2:2017', 'CC6.1', 'most')")
    await _refused(conn, asyncpg.CheckViolationError,                        # control id shape
                   "INSERT INTO compliance.control (control_id, title, domain, owner_role, frequency, test_type) "
                   "VALUES ('lower-case', 'Example', 'access', 'tech lead', 'daily', 'auto')")
    await conn.execute("DELETE FROM compliance.control WHERE control_id = 'EX-IAM-01'")
    assert await conn.fetchval("SELECT count(*) AS n FROM compliance.requirement_mapping") == 0


@pytest.mark.asyncio
async def test_evidence_is_append_only_and_ingested_once(conn):
    eid = await _evidence(conn)
    await _refused(conn, asyncpg.UniqueViolationError,
                   "INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector) "
                   "VALUES ('root_daily', 'gh_default_branch_rules', 'fail', '2026-11-20T02:00:00Z', 'x')")
    await _refused(conn, asyncpg.InsufficientPrivilegeError,
                   "UPDATE compliance.evidence SET status = 'fail' WHERE evidence_id = $1", eid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError,
                   "DELETE FROM compliance.evidence WHERE evidence_id = $1", eid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "TRUNCATE compliance.evidence")
    await _refused(conn, asyncpg.CheckViolationError,                        # unknown status
                   "INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector) "
                   "VALUES ('root_daily', 'gh_codeql_setup', 'green', now(), 'x')")
    await _refused(conn, asyncpg.CheckViolationError,                        # a plain http link
                   "INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector, link) "
                   "VALUES ('root_daily', 'gh_codeql_setup', 'pass', now(), 'x', 'http://example.invalid')")
    await conn.execute("SET LOCAL compliance.retention_purge = 'on'")
    await conn.execute("DELETE FROM compliance.evidence WHERE evidence_id = $1", eid)
    assert await conn.fetchval("SELECT count(*) AS n FROM compliance.evidence WHERE evidence_id = $1", eid) == 0


@pytest.mark.asyncio
async def test_snapshot_counts_must_add_up(conn):
    ins = ("INSERT INTO compliance.score_snapshot (snapshot_date, framework, n_requirements, n_met, n_partial, n_gap, "
           "n_stale, n_unmapped, strict_pct, weighted_pct) VALUES ('2026-11-21', 'soc2:2017', 33, $1, 2, 3, 0, 1, "
           "$2, 40)")
    await _refused(conn, asyncpg.CheckViolationError, ins, 30, 10)            # 30 + 2 + 3 > 33
    await _refused(conn, asyncpg.CheckViolationError, ins, 10, 120)           # percent above 100
    await conn.execute(ins, 10, 30.3)


@pytest.mark.asyncio
async def test_settings_are_the_compiled_defaults(conn):
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, area, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key LIKE 'cis.compliance.%'")}
    assert set(rows) == set(keys.ALL)
    for key in keys.ALL:
        assert json.loads(rows[key]["default_value"]) == settings.definition(key).default_value, key
        assert rows[key]["enforced"] is True and rows[key]["area"] == "compliance", key
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label, key


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M466, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    reqs = await conn.fetchval("SELECT count(*) AS n FROM compliance.requirement")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM compliance.requirement") == reqs == 253
