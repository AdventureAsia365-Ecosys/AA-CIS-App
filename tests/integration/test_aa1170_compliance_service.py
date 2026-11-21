"""AA-1170 — Platform › Compliance on a real database (CIS 466 replayed by conftest): the register loader writes the
invented example register (scripts/compliance/register.example.json, EX- ids), the published evidence is ingested once,
the overview stores today's score per framework and lists what needs attention, a shorter validity window lowers the
score, the controls list and the drawer read the mapping and the evidence history. Runs in one transaction that is
rolled back."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance  # noqa: E402
from api.services import compliance_score as cs  # noqa: E402
from scripts.compliance import load_register as lr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
EXAMPLE = Path(__file__).resolve().parents[2] / "scripts" / "compliance" / "register.example.json"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class OneConnPool:
    """A pool stand-in over one connection, so every write stays in the test's rolled-back transaction."""

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


def _bundle(at: datetime, **status) -> bytes:
    checks = [{"check_id": "repo_branch_protection", "status": status.get("repo_branch_protection", "pass"),
               "summary": "6/6 protected", "detail": {"repos": 6}, "link": "https://github.com/ExampleOrg"},
              {"check_id": "codeql_enabled", "status": "pass", "summary": "6/6", "detail": {"repos": 6}},
              {"check_id": "ci_image_scan_sbom", "status": status.get("ci_image_scan_sbom", "fail"),
               "summary": "2/3", "detail": {"ok": 2, "last_date": "2026-11-20"}},
              {"check_id": "vuln_exceptions_valid", "status": "pass", "summary": "0/0", "detail": {}},
              {"check_id": "last_restore_drill", "status": "pass", "detail": {"last_date": "2026-11-02"}},
              {"check_id": "last_access_review", "status": "pass", "detail": {"last_date": "2026-05-01"}},
              {"check_id": "docs_review_overdue", "status": "pass", "detail": {"overdue": 0}}]
    return json.dumps({"format": 1, "source": "root_daily", "collector": "root compliance-evidence",
                       "collector_version": "abcdef1", "run_ref": "compliance-evidence #1",
                       "generated_at": at.isoformat().replace("+00:00", "Z"), "checks": checks}).encode()


async def _load(conn):
    reg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cat = {(r["framework"], r["ref"]) for r in await conn.fetch("SELECT framework, ref FROM compliance.requirement")}
    assert lr.validate(reg, cat) == []
    await lr.apply(conn, reg)
    return reg


async def _ingest(conn, at=NOW - timedelta(hours=2), **status):
    rows, dropped = compliance.parse_bundle(_bundle(at, **status), NOW)
    assert dropped == []
    return await compliance.ingest(OneConnPool(conn), rows, 72)


@pytest.mark.asyncio
async def test_loader_upserts_deactivates_and_replaces_mappings(conn):
    reg = await _load(conn)
    assert await conn.fetchval("SELECT count(*) FROM compliance.control WHERE active") == len(reg["controls"])
    assert await conn.fetchval("SELECT count(*) FROM compliance.requirement_mapping") == len(reg["mappings"])
    smaller = dict(reg, controls=reg["controls"][1:],
                   mappings=[m for m in reg["mappings"] if m["control_id"] != reg["controls"][0]["control_id"]])
    await lr.apply(conn, smaller)
    gone = reg["controls"][0]["control_id"]
    assert await conn.fetchval("SELECT active FROM compliance.control WHERE control_id = $1", gone) is False
    assert await conn.fetchval("SELECT count(*) FROM compliance.requirement_mapping WHERE control_id = $1", gone) == 0


@pytest.mark.asyncio
async def test_ingest_is_idempotent_and_sets_valid_until(conn):
    assert await _ingest(conn) == 7
    assert await _ingest(conn) == 0
    row = await conn.fetchrow("SELECT collected_at, valid_until FROM compliance.evidence "
                              "WHERE check_id = 'repo_branch_protection'")
    assert row["valid_until"] - row["collected_at"] == timedelta(hours=72)


@pytest.mark.asyncio
async def test_overview_scores_snapshots_and_attention(conn):
    await _load(conn)
    await _ingest(conn)
    pool = OneConnPool(conn)
    out = await compliance.overview(pool, now=NOW, ingest_state={"state": "ok"})
    by_fw = {f["framework"]: f for f in out["frameworks"]}
    assert list(by_fw) == list(cs.FRAMEWORKS)
    iso = by_fw["iso27001:2022"]
    assert iso["n_requirements"] == 123 and iso["n_met"] >= 2 and iso["strict_pct"] > 0
    assert iso["reference_pct"] == 45.8
    attention = {a["check_id"]: a for a in out["attention"]}
    scan = attention["ci_image_scan_sbom"]
    assert scan["state"] == "fail" and "EX-VUL-01" in scan["controls"]
    assert attention["last_access_review"]["state"] == "fail"           # older than the 100-day setting
    assert out["heartbeat"][0]["source"] == "root_daily" and out["heartbeat"][0]["ok"] is True
    snap = await conn.fetchval("SELECT count(*) FROM compliance.score_snapshot WHERE snapshot_date = $1", NOW.date())
    assert snap == len(cs.FRAMEWORKS)
    again = await compliance.overview(pool, now=NOW)                    # upsert, not a second row
    assert again["frameworks"][0]["strict_pct"] == iso["strict_pct"]
    assert await conn.fetchval("SELECT count(*) FROM compliance.score_snapshot WHERE snapshot_date = $1",
                               NOW.date()) == len(cs.FRAMEWORKS)


@pytest.mark.asyncio
async def test_a_shorter_validity_window_lowers_the_score(conn):
    await _load(conn)
    await _ingest(conn)
    pool = OneConnPool(conn)
    before = {f["framework"]: f for f in (await compliance.overview(pool, now=NOW))["frameworks"]}
    real = await compliance.score_settings()
    short = cs.ScoreSettings(validity_hours={"root_daily": 1}, heartbeat_hours=real.heartbeat_hours,
                             partial_weight=real.partial_weight, fact_limits=real.fact_limits)

    async def shorter():
        return short
    with patch.object(compliance, "score_settings", shorter):
        after = {f["framework"]: f for f in (await compliance.overview(pool, now=NOW))["frameworks"]}
    iso_b, iso_a = before["iso27001:2022"], after["iso27001:2022"]
    assert iso_a["strict_pct"] < iso_b["strict_pct"] and iso_a["n_met"] == 0 and iso_a["n_stale"] > 0


@pytest.mark.asyncio
async def test_controls_and_the_drawer(conn):
    await _load(conn)
    await _ingest(conn, at=NOW - timedelta(hours=30))
    await _ingest(conn, at=NOW - timedelta(hours=2), ci_image_scan_sbom="pass")
    pool = OneConnPool(conn)
    rows = {r["control_id"]: r for r in (await compliance.controls(pool, now=NOW))["controls"]}
    assert rows["EX-VUL-01"]["state"] == "met" and rows["EX-VUL-01"]["frameworks"] == {"iso27001:2022": 1,
                                                                                       "owasp-top10:2021": 1}
    assert rows["EX-GOV-02"]["state"] == "no_evidence"
    d = await compliance.control_detail(pool, "EX-VUL-01", now=NOW)
    scan = next(c for c in d["checks"] if c["check_id"] == "ci_image_scan_sbom")
    assert [h["status"] for h in scan["history"]] == ["pass", "fail"]   # newest first
    assert {r["ref"] for r in d["requirements"]} == {"A.8.8", "A06"}
    assert d["requirements"][0]["title"]
    with pytest.raises(compliance.ComplianceMissing):
        await compliance.control_detail(pool, "EX-NONE-01", now=NOW)
