"""AA-1174 — the Obligations tab on a real database (CIS 466 + 471 replayed by conftest): the loader writes an invented
format-2 register (the format-1 example plus an ex- law, its obligations and matrix rows), evidence rows give the
expected obligation states (met on fresh passing evidence, not checked without an automatic check), the standard
scores are unchanged by the law rows, a law no longer listed is deactivated and its obligations set not applicable
(never deleted), and the cis_db source reads shared.job as written by the retention kinds. Runs in one transaction
that is rolled back."""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import compliance, compliance_db, compliance_obligations  # noqa: E402
from scripts.compliance import load_register as lr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
EXAMPLE = Path(__file__).resolve().parents[2] / "scripts" / "compliance" / "register.example.json"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


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


def register() -> dict:
    reg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    reg["format"] = 2
    reg["controls"] += [
        {"control_id": "EX-PRV-01", "title": "Records of processing kept current", "domain": "privacy",
         "owner_role": "tech lead", "frequency": "quarterly", "test_type": "hybrid",
         "checks": [{"check_id": "legal_ropa_reviewed", "required": True}], "scope": "all", "active": True},
        {"control_id": "EX-PRV-09", "title": "Impact assessment done", "domain": "privacy",
         "owner_role": "business owner", "frequency": "annual", "test_type": "manual", "checks": [], "scope": "all",
         "active": True}]
    reg["frameworks"] = [{"framework": "ex-law:2025", "title": "Example personal data law", "jurisdiction": "VN",
                          "citation": "Example Law 1/2025", "source_url": "https://vbpl.vn/example",
                          "in_force_on": "2026-01-01", "sort_order": 20}]
    reg["requirements"] = [
        {"framework": "ex-law:2025", "ref": "Art.30", "section": "Example law", "title": "Keep records of processing",
         "sort_order": 1, "counsel_review": False, "applies_as": "both",
         "doc_url": "https://github.com/ExampleOrg/repo/blob/master/docs/legal/register.md"},
        {"framework": "ex-law:2025", "ref": "Art.21", "section": "Example law", "title": "Assess processing impact",
         "sort_order": 2, "counsel_review": True, "applies_as": "controller"}]
    reg["applicability"] = [{"market": "VN", "segment": "staff", "framework": "ex-law:2025",
                             "test": "Processes staff data in Vietnam", "verdict": "applies",
                             "source_url": "https://vbpl.vn/example", "source_title": "Example Law 1/2025",
                             "assessed_on": "2026-11-23"}]
    reg["mappings"] += [{"control_id": "EX-PRV-01", "framework": "ex-law:2025", "ref": "Art.30", "coverage": "full"},
                        {"control_id": "EX-PRV-09", "framework": "ex-law:2025", "ref": "Art.21", "coverage": "full"}]
    return reg


async def _load(conn, reg):
    cat = {(r["framework"], r["ref"]) for r in await conn.fetch("SELECT framework, ref FROM compliance.requirement")}
    kinds = {r["framework"]: r["kind"] for r in await conn.fetch("SELECT framework, kind FROM compliance.framework")}
    assert lr.validate(reg, cat, kinds) == []
    await lr.apply(conn, reg)


@pytest.mark.asyncio
async def test_obligation_states_and_unchanged_standard_scores(conn):
    base = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    await lr.apply(conn, base)
    before = (await compliance.overview(OneConnPool(conn), now=NOW, write=False))["frameworks"]
    await _load(conn, register())
    await conn.execute(
        "INSERT INTO compliance.evidence (source, check_id, status, collected_at, collector, detail, link) "
        "VALUES ('root_daily', 'legal_ropa_reviewed', 'pass', $1, 'test', '{\"overdue\": 0, \"pages\": 1}', "
        "'https://github.com/ExampleOrg/x')", NOW - timedelta(hours=1))
    out = await compliance_obligations.obligations(OneConnPool(conn), now=NOW)
    items = {i["ref"]: i for i in out["items"] if i["framework"] == "ex-law:2025"}
    assert items["Art.30"]["state"] == "met" and items["Art.30"]["evidence"][0]["link"].startswith("https://github")
    assert items["Art.21"]["state"] == "not_checked" and items["Art.21"]["counsel_review"] is True
    laws = {law["framework"]: law for law in out["laws"]}
    assert laws["ex-law:2025"]["counts"]["met"] == 1 and "eu-ai-act:2024" in laws
    assert out["applicability"][0]["review_due"] is False
    after = (await compliance.overview(OneConnPool(conn), now=NOW, write=False))["frameworks"]
    strip = [(f["framework"], f["strict_pct"], f["weighted_pct"]) for f in before]
    assert strip == [(f["framework"], f["strict_pct"], f["weighted_pct"]) for f in after]
    assert "ex-law:2025" not in {f["framework"] for f in after}                  # laws are not a percentage


@pytest.mark.asyncio
async def test_law_dropped_from_the_file_is_deactivated_not_deleted(conn):
    reg = register()
    await _load(conn, reg)
    smaller = dict(reg, frameworks=[], requirements=[], applicability=[],
                   mappings=[m for m in reg["mappings"] if m["framework"] != "ex-law:2025"])
    await lr.apply(conn, smaller)
    assert await conn.fetchval("SELECT active FROM compliance.framework WHERE framework = 'ex-law:2025'") is False
    assert await conn.fetchval("SELECT count(*) FROM compliance.requirement "
                               "WHERE framework = 'ex-law:2025' AND applicable") == 0
    assert await conn.fetchval("SELECT count(*) FROM compliance.requirement WHERE framework = 'ex-law:2025'") == 2
    assert await conn.fetchval("SELECT count(*) FROM compliance.applicability") == 0
    assert await conn.fetchval("SELECT count(*) FROM compliance.requirement "
                               "WHERE framework = 'eu-ai-act:2024' AND applicable") == 7   # seeded rows untouched


@pytest.mark.asyncio
async def test_cis_db_reads_the_retention_runs(conn):
    await conn.execute("DELETE FROM shared.job WHERE kind = ANY($1::text[])", list(compliance_db.KINDS))
    for i, kind in enumerate(compliance_db.KINDS):
        mode = "live" if i else "dry_run"
        await conn.execute(
            "INSERT INTO shared.job (kind, status, payload, result, created_at, finished_at) "
            "VALUES ($1, 'succeeded', '{}'::jsonb, $2::jsonb, now() - interval '1 day', now() - interval '1 day')",
            kind, json.dumps({"mode": mode}))
    rows = {r["check_id"]: r for r in await compliance_db.collect(OneConnPool(conn), NOW)}
    assert rows["retention_runs_recent"]["status"] == "pass" and rows["retention_runs_recent"]["detail"]["last_date"]
    assert rows["retention_live_enabled"]["detail"]["never_live"] == 1
    new = await compliance.ingest(OneConnPool(conn), list(rows.values()), 72)
    assert new == 2
