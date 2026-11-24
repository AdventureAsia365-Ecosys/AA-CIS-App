"""AA-1173 — exceptions, monthly reviews and the read-only DB checks of Platform › Compliance on a real database (CIS
479 replayed by conftest; skipped while this branch's main has no 479, the MIG-4 chain: the migration PR lands first).
Maker-checker (a second person approves; the proposer self-approves only while two-person approval is off, recorded),
the expiry window from the setting, revoke and withdraw, the approved exception on the control rows, one review a
month with the page's numbers, and compliance_db.collect: the catalogue checks read the live catalog and the review
check follows the newest review. Invented ids (EX- controls). Runs in one transaction that is rolled back."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance  # noqa: E402
from api.services import compliance_db  # noqa: E402
from api.services import compliance_exceptions as ce  # noqa: E402
from scripts.compliance import load_register as lr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
EXAMPLE = Path(__file__).resolve().parents[2] / "scripts" / "compliance" / "register.example.json"
NOW = datetime.now(timezone.utc)
REASON = "An invented reason that is long enough to pass the check."


class OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    if not await c.fetchval("SELECT to_regclass('compliance.exception') IS NOT NULL AS ok"):
        await c.close()
        pytest.skip("CIS 479 is not applied on this branch's database (MIG-4: the migration PR lands first)")
    tr = c.transaction()
    await tr.start()
    try:
        reg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        await lr.apply(c, reg)
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _body(ref: str, days: int = 30) -> dict:
    expires = (NOW.date().toordinal() + days)
    return {"subject_kind": "control", "subject_ref": ref, "title": "Invented exception", "reason": REASON,
            "risk_level": "medium", "expires_on": datetime.fromordinal(expires).date().isoformat()}


async def _control(conn) -> str:
    return await conn.fetchval("SELECT control_id FROM compliance.control ORDER BY control_id LIMIT 1")


@pytest.mark.asyncio
async def test_maker_checker_and_the_control_row(conn):
    pool = OneConnPool(conn)
    ref = await _control(conn)
    with patch.object(ce, "max_days", AsyncMock(return_value=90)), \
            patch.object(ce, "_two_person", AsyncMock(return_value=True)):
        row = await ce.propose(pool, _body(ref), "admin:maker")
        assert row["state"] == "proposed" and row["proposed_by"] == "admin:maker"
        with pytest.raises(ce.ComplianceRefused) as e:
            await ce.propose(pool, _body(ref), "admin:maker")
        assert e.value.code == "open_proposal"
        with pytest.raises(ce.ComplianceRefused) as e:
            await ce.decide(pool, row["exception_id"], "admin:maker", approve=True)
        assert e.value.code == "second_person_required"
        out = await ce.decide(pool, row["exception_id"], "admin:checker", approve=True, note="Looked at it")
        assert out["state"] == "approved" and out["self_approved"] is False
    rows = {r["control_id"]: r for r in (await compliance.controls(pool))["controls"]}
    assert rows[ref]["exception"]["exception_id"] == row["exception_id"]
    assert rows[ref]["state"] != "accepted"   # the real state is kept
    audit = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE resource_type = "
                                "'compliance_exception' AND resource_id = $1", str(row["exception_id"]))
    assert audit == 2
    out = await ce.revoke(pool, row["exception_id"], "admin:maker", "Fixed for real")
    assert out["state"] == "revoked"
    with pytest.raises(ce.ComplianceRefused):
        await ce.revoke(pool, row["exception_id"], "admin:maker", "Again")


@pytest.mark.asyncio
async def test_self_approval_when_two_person_is_off_and_the_expiry_window(conn):
    pool = OneConnPool(conn)
    ref = await _control(conn)
    with patch.object(ce, "max_days", AsyncMock(return_value=10)), \
            patch.object(ce, "_two_person", AsyncMock(return_value=False)):
        with pytest.raises(ce.ComplianceRefused) as e:
            await ce.propose(pool, _body(ref, days=30), "admin:solo")
        assert e.value.code == "expires_on_too_far"
        row = await ce.propose(pool, _body(ref, days=5), "admin:solo")
        with pytest.raises(ce.ComplianceRefused) as e:
            await ce.decide(pool, row["exception_id"], "admin:solo", approve=False, note="Not now")
        assert e.value.code == "withdraw_instead"
        out = await ce.decide(pool, row["exception_id"], "admin:solo", approve=True)
        assert out["state"] == "approved" and out["self_approved"] is True
        listed = await ce.list_exceptions(pool)
    assert listed["counts"]["approved"] == 1 and listed["max_days"] == 10


@pytest.mark.asyncio
async def test_unknown_control_is_refused(conn):
    with patch.object(ce, "max_days", AsyncMock(return_value=90)):
        with pytest.raises(ce.ComplianceRefused) as e:
            await ce.propose(OneConnPool(conn), _body("EX-NONE-99"), "admin:maker")
    assert e.value.status == 404


@pytest.mark.asyncio
async def test_one_review_a_month_and_the_review_check(conn):
    pool = OneConnPool(conn)
    before = compliance_db.evaluate_review(await conn.fetchval(compliance_db.SQL_LAST_REVIEW), NOW)
    summary = {"frameworks": [{"framework": "iso27001:2022", "weighted_pct": 9.3}], "controls": {"met": 1}}
    out = await ce.record_review(pool, {"notes": REASON}, "admin:reviewer", summary)
    assert out["period"] == NOW.date().replace(day=1).isoformat() and out["summary"]["controls"] == {"met": 1}
    with pytest.raises(ce.ComplianceRefused) as e:
        await ce.record_review(pool, {"notes": REASON}, "admin:reviewer", summary)
    assert e.value.code == "review_exists"
    listed = await ce.list_reviews(pool)
    assert listed["current_recorded"] is True and len(listed["reviews"]) >= 1
    after = compliance_db.evaluate_review(await conn.fetchval(compliance_db.SQL_LAST_REVIEW), NOW)
    assert after["status"] == "pass" and after["detail"]["last_date"] == NOW.date().isoformat()
    assert before["status"] in ("fail", "pass")


@pytest.mark.asyncio
async def test_collect_reads_the_live_catalog(conn):
    rows = {r["check_id"]: r for r in await compliance_db.collect(OneConnPool(conn), NOW)}
    assert {"db_rls_policies", "db_grants", "monthly_review_recent", "retention_runs_recent"} <= set(rows)
    for cid in compliance_db.CATALOG_CHECKS:
        assert rows[cid]["status"] in ("pass", "fail"), rows[cid]
        assert rows[cid]["detail"]["tables"] > 100   # every app schema of the replay was read
    # the rows are what the table accepts
    assert await compliance.ingest(OneConnPool(conn), list(rows.values()), 72) == len(rows)


def _evidence(source: str, check_id: str, status: str, hours_ago: int, sealed: bool) -> dict:
    key = f"evidence/{source}/bundles/2026/11/24/{hours_ago:02d}Z-1.json"
    return {"source": source, "check_id": check_id, "status": status,
            "collected_at": NOW.replace(microsecond=0) - timedelta(hours=hours_ago), "collector": "invented collector",
            "collector_version": "abc1234", "run_ref": f"invented run {hours_ago}", "summary": None, "detail": {},
            "link": None, "object_key": key if sealed else None, "object_sha256": "a" * 64 if sealed else None}


@pytest.mark.asyncio
async def test_latest_prefers_a_sealed_row_over_a_newer_unsealed_one(conn):
    # S290 review: an unsealed row of a sealed source (a rewritten phase 1 docs reader copy) must not override the
    # sealed evidence, even when it is newer; an unsealed root_daily row still counts while nothing of it is sealed,
    # and an unsealed source (cis_db) keeps "newest wins"
    pool = OneConnPool(conn)
    await compliance.ingest(pool, [_evidence("aws_nightly", "ex_sealed_probe", "fail", 5, True),
                                   _evidence("aws_nightly", "ex_sealed_probe", "pass", 1, False),
                                   _evidence("root_daily", "ex_unsealed_probe", "pass", 3, False),
                                   _evidence("cis_db", "ex_live_probe", "fail", 4, False),
                                   _evidence("cis_db", "ex_live_probe", "pass", 2, False)], 72)
    latest = (await compliance._state(conn))["latest"]
    assert latest["ex_sealed_probe"]["status"] == "fail" and latest["ex_sealed_probe"]["object_key"]
    assert latest["ex_unsealed_probe"]["status"] == "pass" and latest["ex_unsealed_probe"]["object_key"] is None
    assert latest["ex_live_probe"]["status"] == "pass"
    await compliance.ingest(pool, [_evidence("root_daily", "ex_unsealed_probe", "fail", 2, True)], 72)
    assert (await compliance._state(conn))["latest"]["ex_unsealed_probe"]["status"] == "fail"
