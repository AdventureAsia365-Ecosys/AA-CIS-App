"""AA-1184 — the Calendar tab on a real database (CIS 466 / 479 replayed by conftest): a published bundle carrying the
ops calendar check is ingested into compliance.evidence like any other check; the tab reads it back (lines worst first,
the watched line with its check's state), a deliberately past-due line shows the check red, and the next bundle
without it shows it green. Invented rows and titles; one transaction, rolled back."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance  # noqa: E402
from api.services import compliance_calendar as cc  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime(2026, 12, 2, 6, 0, tzinfo=timezone.utc)
CALM = ["OC-201|D|ok|2026-12-20||TL|Renew the widget licence",
        "OC-202|R|watched|||TL|Monthly gadget review|gadget_review_recent",
        "OC-203|A|automated|||AG|Nightly widget probe"]
PAST_DUE = "OC-299|D|overdue|2026-12-01||AG|Drill: a deliberately past-due test line|||Injected for one run"


class OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


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


def _bundle(at: datetime, rows: list[str]) -> bytes:
    overdue = sum(1 for r in rows if "|overdue|" in r)
    checks = [{"check_id": cc.CHECK_ID, "status": "fail" if overdue else "pass",
               "summary": f"{overdue} overdue", "link": "https://github.com/ExampleOrg/Root/blob/master/docs/ops/x.md",
               "detail": {"calendar_updated": "2026-11-28", "today": at.date().isoformat(), "lines": len(rows),
                          "overdue": overdue, "due_soon": 0, "open": 0, "counts": {}, "rows": rows,
                          "drill": bool(overdue)}},
              {"check_id": "gadget_review_recent", "status": "fail", "summary": "too old", "detail": {}}]
    return json.dumps({"format": 1, "source": "root_daily", "collector": "root compliance-evidence",
                       "collector_version": "abcdef1", "run_ref": "compliance-evidence #9",
                       "generated_at": at.isoformat().replace("+00:00", "Z"), "checks": checks}).encode()


async def _ingest(conn, at, rows):
    parsed, dropped = compliance.parse_bundle(_bundle(at, rows), NOW)
    assert dropped == []
    return await compliance.ingest(OneConnPool(conn), parsed, 72)


@pytest.mark.asyncio
async def test_never_published_then_red_then_green(conn):
    # a fresh test database has no row of this check (compliance.evidence is append-only: never deleted here)
    assert await conn.fetchval("SELECT count(*) FROM compliance.evidence WHERE check_id = $1", cc.CHECK_ID) == 0
    empty = await cc.calendar(OneConnPool(conn), NOW)
    assert empty["available"] is False

    await _ingest(conn, NOW - timedelta(hours=3), CALM + [PAST_DUE])
    red = await cc.calendar(OneConnPool(conn), NOW)
    assert red["available"] and red["check"]["state"] == "fail"
    assert [ln["id"] for ln in red["lines"]] == ["OC-299", "OC-201", "OC-202", "OC-203"]
    assert red["lines"][0]["note"] == "Injected for one run" and red["calendar"]["drill"] is True
    watched = next(ln for ln in red["lines"] if ln["id"] == "OC-202")
    assert watched["check_state"] == "fail"
    overview = await compliance.overview(OneConnPool(conn), NOW, write=False)
    assert cc.CHECK_ID in {a["check_id"] for a in overview["attention"]}   # red on the Overview tab too

    await _ingest(conn, NOW - timedelta(hours=1), CALM)
    green = await cc.calendar(OneConnPool(conn), NOW)
    assert green["check"]["state"] == "pass" and green["calendar"]["overdue"] == 0
    overview = await compliance.overview(OneConnPool(conn), NOW, write=False)
    assert cc.CHECK_ID not in {a["check_id"] for a in overview["attention"]}
