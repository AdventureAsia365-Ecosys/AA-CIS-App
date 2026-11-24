"""AA-1173 (S290) — CIS 479: compliance.exception (maker-checker, expiry, transition-only), compliance.review
(append-only, one per month), the sealed-object columns of compliance.evidence and five cis.compliance.* settings
(conftest replays 479 with the other files). Registered; the exception CHECKs and the guard trigger (no delete, no edit of
the proposal, only valid moves, the proposer approves only as a recorded self-approval); the review is append-only and
unique per month; the evidence object CHECKs; cis_app DML only, nothing for aa_app_user; the five settings equal the
compiled defaults with a complete guide; a re-run changes nothing. Invented ids (EX- control, ex_ check). Runs in a
transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M479 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "479_aa1173_compliance_exceptions_reviews.sql")
KEYS_479 = ("cis.compliance.validity_hours.aws_nightly", "cis.compliance.validity_hours.prowler_weekly",
            "cis.compliance.prowler_critical_high_max", "cis.compliance.exception_max_days",
            "cis.compliance.review_max_age_days")
REASON = "An invented reason that is long enough to pass the check."
INSERT_EXC = ("INSERT INTO compliance.exception (subject_kind, subject_ref, title, reason, risk_level, proposed_by, "
              "expires_on) VALUES ($1, $2, 'Invented exception', $3, 'low', $4, current_date + 30) "
              "RETURNING exception_id")


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


async def _exception(conn, ref="EX-IAM-01", by="admin:maker"):
    return await conn.fetchval(INSERT_EXC, "control", ref, REASON, by)


@pytest.mark.asyncio
async def test_479_registered(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '479'") == "479"
    tables = {r["relname"] for r in await conn.fetch(
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'compliance'::regnamespace AND c.relkind = 'r'")}
    assert {"exception", "review"} <= tables


@pytest.mark.asyncio
async def test_exception_checks(conn):
    await _refused(conn, asyncpg.CheckViolationError, INSERT_EXC, "server", "EX-IAM-01", REASON, "admin:maker")
    await _refused(conn, asyncpg.CheckViolationError, INSERT_EXC, "control", "bad ref!", REASON, "admin:maker")
    await _refused(conn, asyncpg.CheckViolationError, INSERT_EXC, "control", "EX-IAM-01", "too short", "admin:maker")
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO compliance.exception (subject_kind, subject_ref, title, reason, risk_level, "
                   "proposed_by, expires_on) VALUES ('check', 'ex_check', 'Invented', $1, 'low', 'admin:maker', "
                   "current_date)", REASON)
    eid = await _exception(conn)
    row = await conn.fetchrow("SELECT status, self_approved FROM compliance.exception WHERE exception_id = $1", eid)
    assert (row["status"], row["self_approved"]) == ("proposed", False)


@pytest.mark.asyncio
async def test_exception_maker_checker(conn):
    eid = await _exception(conn)
    approve = ("UPDATE compliance.exception SET status = 'approved', decided_by = $2, decided_at = now(), "
               "self_approved = $3 WHERE exception_id = $1")
    # the proposer cannot approve unless it is recorded as a self-approval
    await _refused(conn, asyncpg.CheckViolationError, approve, eid, "admin:maker", False)
    # a self-approved flag on another person's approval is refused too
    await _refused(conn, asyncpg.CheckViolationError, approve, eid, "admin:checker", True)
    await conn.execute(approve, eid, "admin:checker", False)
    # the approval cannot change once made
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.exception SET decided_by = 'admin:other' WHERE exception_id = $1", eid)
    # approved -> revoked is a valid move and needs the revoker
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.exception SET status = 'revoked' WHERE exception_id = $1", eid)
    await conn.execute("UPDATE compliance.exception SET status = 'revoked', revoked_by = 'admin:maker', "
                       "revoked_at = now(), revoke_note = 'Fixed' WHERE exception_id = $1", eid)
    # revoked is final
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.exception SET status = 'approved' WHERE exception_id = $1", eid)
    # self-approval while recorded as such passes
    own = await _exception(conn, ref="EX-IAM-02")
    await conn.execute(approve, own, "admin:maker", True)


@pytest.mark.asyncio
async def test_exception_is_an_audit_record(conn):
    eid = await _exception(conn)
    await _refused(conn, asyncpg.InsufficientPrivilegeError,
                   "DELETE FROM compliance.exception WHERE exception_id = $1", eid)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "TRUNCATE compliance.exception")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.exception SET expires_on = expires_on + 400 WHERE exception_id = $1", eid)
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE compliance.exception SET reason = $2 WHERE exception_id = $1", eid, REASON + " changed")


@pytest.mark.asyncio
async def test_review_append_only_and_once_a_month(conn):
    ins = ("INSERT INTO compliance.review (kind, period, summary, notes, reviewed_by) "
           "VALUES ('monthly_posture', $1::text::date, '{}'::jsonb, $2, 'admin:reviewer') RETURNING review_id")
    await _refused(conn, asyncpg.CheckViolationError, ins, "2026-11-02", REASON)
    await _refused(conn, asyncpg.CheckViolationError, ins, "2026-11-01", "short")
    rid = await conn.fetchval(ins, "2026-11-01", REASON)
    await _refused(conn, asyncpg.UniqueViolationError, ins, "2026-11-01", REASON)
    await _refused(conn, asyncpg.InsufficientPrivilegeError,
                   "UPDATE compliance.review SET notes = $2 WHERE review_id = $1", rid, REASON)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "DELETE FROM compliance.review WHERE review_id = $1", rid)


@pytest.mark.asyncio
async def test_evidence_object_columns(conn):
    ins = ("INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector, object_key, "
           "object_sha256) VALUES ('ex_source', 'ex_check', 'pass', now(), 'invented', $1, $2)")
    await _refused(conn, asyncpg.CheckViolationError, ins, "other/ex_source/a.json", "a" * 64)
    await _refused(conn, asyncpg.CheckViolationError, ins, "evidence/ex_source/../a.json", "a" * 64)
    await _refused(conn, asyncpg.CheckViolationError, ins, "evidence/ex_source/a.json", "XYZ")
    await conn.execute(ins, "evidence/ex_source/bundles/2026/11/24/021500Z-1.json", "a" * 64)


@pytest.mark.asyncio
async def test_grants(conn):
    for t in ("exception", "review"):
        rel = f"compliance.{t}"
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT, INSERT, UPDATE') AS ok",
                                   rel) is True, t
        assert await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT') AS ok", rel) is False, t


@pytest.mark.asyncio
async def test_settings_are_the_compiled_defaults(conn):
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, area, label, approval, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = ANY($1::text[])", list(KEYS_479))}
    assert set(rows) == set(KEYS_479)
    for key in KEYS_479:
        assert json.loads(rows[key]["default_value"]) == settings.definition(key).default_value, key
        assert rows[key]["enforced"] is True and rows[key]["area"] == "compliance", key
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label, key
        assert rows[key]["approval"] == "maker_checker", key


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M479, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
