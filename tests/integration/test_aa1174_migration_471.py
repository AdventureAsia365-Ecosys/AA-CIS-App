"""AA-1174 (S288) — CIS 471: compliance.framework and compliance.applicability, law metadata on requirements, framework
foreign keys instead of the hard-coded CHECKs of 466, ten new cis.compliance.* settings (conftest replays 471 with the
other files). Registered; the six scored frameworks of 466; the two framework CHECKs gone and the FKs present; a law
framework and its requirements load, an unknown framework is refused; the new column and matrix CHECKs; cis_app DML
only, nothing for aa_app_user; the ten settings equal the compiled defaults with a complete guide; a re-run changes
nothing. Invented ids (ex- framework, EX- control). Runs in a transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance_settings as keys  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M471 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "471_aa1174_compliance_obligations.sql")
SCORED = {"iso27001:2022", "soc2:2017", "iso42001:2023", "owasp-top10:2021", "owasp-llm:2025", "eu-ai-act:2024"}


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
    with pytest.raises(error):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _law(conn, fid="ex-law:2025"):
    await conn.execute(
        "INSERT INTO compliance.framework (framework, kind, title, jurisdiction, citation, source_url, in_force_on, "
        "scored, sort_order) VALUES ($1, 'law', 'Example law', 'VN', 'Example 1/2025', "
        "'https://example.invalid/law', '2026-01-01', false, 10)", fid)


@pytest.mark.asyncio
async def test_471_registered_with_the_framework_rows(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '471'") == "471"
    rows = {r["framework"]: r for r in await conn.fetch("SELECT * FROM compliance.framework")}
    assert {f for f, r in rows.items() if r["scored"]} >= SCORED
    assert rows["eu-ai-act:2024"]["kind"] == "law" and rows["iso27001:2022"]["kind"] == "standard"
    # every 466 requirement and snapshot names a framework row
    assert await conn.fetchval("SELECT count(*) AS n FROM compliance.requirement r "
                               "WHERE NOT EXISTS (SELECT 1 FROM compliance.framework f "
                               "WHERE f.framework = r.framework)") == 0


@pytest.mark.asyncio
async def test_checks_replaced_by_foreign_keys(conn):
    cons = {r["conname"]: r["contype"] for r in await conn.fetch(
        "SELECT conname, contype::text AS contype FROM pg_constraint WHERE connamespace = 'compliance'::regnamespace")}
    assert "requirement_framework_check" not in cons and "score_snapshot_framework_check" not in cons
    assert cons["requirement_framework_fkey"] == "f" and cons["score_snapshot_framework_fkey"] == "f"
    await _refused(conn, asyncpg.ForeignKeyViolationError,
                   "INSERT INTO compliance.requirement (framework, ref, section, title, sort_order) "
                   "VALUES ('nist:2024', 'X.1', 'Invented', 'Invented row', 1)")
    await _law(conn)
    await conn.execute(
        "INSERT INTO compliance.requirement (framework, ref, section, title, sort_order, counsel_review, applies_as, "
        "source_url, doc_url) VALUES ('ex-law:2025', 'Art.20', 'Law', 'File the transfer dossier', 1, true, 'both', "
        "'https://example.invalid/law#a20', 'https://github.com/example/repo/blob/master/docs/x.md')")
    await _refused(conn, asyncpg.ForeignKeyViolationError,                   # a framework in use is never deleted
                   "DELETE FROM compliance.framework WHERE framework = 'ex-law:2025'")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.requirement SET applies_as = 'owner' WHERE framework = 'ex-law:2025'")
    await _refused(conn, asyncpg.CheckViolationError,                        # doc links are GitHub pages only
                   "UPDATE compliance.requirement SET doc_url = 'https://example.invalid/x' "
                   "WHERE framework = 'ex-law:2025'")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.requirement SET source_url = 'http://example.invalid/x' "
                   "WHERE framework = 'ex-law:2025'")
    assert await conn.fetchval("SELECT counsel_review AS ok FROM compliance.requirement "
                               "WHERE framework = 'iso27001:2022' AND ref = '4.1'") is False


@pytest.mark.asyncio
async def test_framework_and_matrix_checks(conn):
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO compliance.framework (framework, kind, title, sort_order) "
                   "VALUES ('Bad Id', 'law', 'Example', 1)")
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO compliance.framework (framework, kind, title, sort_order) "
                   "VALUES ('ex:1', 'rule', 'Example', 1)")
    await _law(conn)
    ins = ("INSERT INTO compliance.applicability (market, segment, framework, test, verdict, source_url, source_title, "
           "assessed_on) VALUES ($1, $2, 'ex-law:2025', 'Offers services to people there', $3, $4, 'Example law', "
           "'2026-11-23')")
    await conn.execute(ins, "VN", "staff", "applies", "https://example.invalid/law")
    await conn.execute(ins, "US-CA", "traveller", "not_applicable", "https://example.invalid/law")
    await _refused(conn, asyncpg.CheckViolationError, ins, "vn", "staff", "applies", "https://example.invalid/law")
    await _refused(conn, asyncpg.CheckViolationError, ins, "DE", "visitor", "applies", "https://example.invalid/law")
    await _refused(conn, asyncpg.CheckViolationError, ins, "DE", "traveller", "maybe", "https://example.invalid/law")
    await _refused(conn, asyncpg.CheckViolationError, ins, "DE", "traveller", "applies", "http://example.invalid/law")
    await _refused(conn, asyncpg.UniqueViolationError, ins, "VN", "staff", "not_yet", "https://example.invalid/law")
    assert await conn.fetchval("SELECT counsel_review AS ok FROM compliance.applicability "
                               "WHERE market = 'VN' AND segment = 'staff'") is True


@pytest.mark.asyncio
async def test_grants(conn):
    for t in ("framework", "applicability"):
        rel = f"compliance.{t}"
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT, INSERT, UPDATE, DELETE') AS ok",
                                   rel) is True, t
        assert await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT') AS ok", rel) is False, t


@pytest.mark.asyncio
async def test_settings_are_the_compiled_defaults(conn):
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, area, label, approval, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = ANY($1::text[])", list(keys.ALL_471))}
    assert set(rows) == set(keys.ALL_471) and len(keys.ALL_471) == 10
    for key in keys.ALL_471:
        assert json.loads(rows[key]["default_value"]) == settings.definition(key).default_value, key
        assert rows[key]["enforced"] is True and rows[key]["area"] == "compliance", key
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label, key
    assert rows[keys.APPLICABILITY_REVIEW_MAX_AGE_DAYS]["approval"] == "single"       # display only


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M471, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    fws = await conn.fetchval("SELECT count(*) AS n FROM compliance.framework")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM compliance.framework") == fws
