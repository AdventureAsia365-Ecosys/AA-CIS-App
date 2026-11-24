"""AA-1173 — the read-only DB checks of Platform › Compliance and their daily job, without a database: the X2 catalogue
rules split into db_rls_policies / db_grants over an invented dump (allowlisted findings pass, others fail with their
keys), the monthly review check, the byte copy of the rules file, and the job's daily schedule (switch, hour,
idempotency key) and run (collect -> ingest with the cis_db window)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from api.services import compliance, compliance_db
from services.jobs import compliance_db_checks_job as job

NOW = datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def _dump(rls=True, policy="NULLIF(current_setting('app.tenant_id', true), '')::uuid = tenant_id", grants=()):
    return {"schemas": [{"schema": "booking", "grants": []}],
            "relations": {"booking": {"ex_table": {
                "kind": "table", "rls": rls, "columns": [{"name": "tenant_id"}, {"name": "id"}],
                "policies": [{"policy": "ex_iso", "permissive": "PERMISSIVE", "roles": ["booking"], "cmd": "ALL",
                              "qual": policy, "with_check": None}] if policy else [],
                "constraints": [], "grants": list(grants), "column_grants": []}}}}


def test_the_rules_file_is_a_byte_copy_of_the_ci_guard():
    assert (ROOT / "shared/compliance/rls_catalogue_rules.py").read_bytes() == \
        (ROOT / "tests/integration/rls_catalogue_rules.py").read_bytes()


def test_clean_catalog_passes_both():
    rows = compliance_db.evaluate_catalog(_dump(), NOW, allowlist={})
    assert {r["check_id"]: r["status"] for r in rows} == {"db_rls_policies": "pass", "db_grants": "pass"}
    assert rows[0]["detail"]["tables"] == 1 and rows[0]["link"].endswith("db-schema-reference.md")


def test_a_tenant_table_without_rls_fails_the_policy_check():
    rows = {r["check_id"]: r for r in compliance_db.evaluate_catalog(_dump(rls=False, policy=None), NOW,
                                                                         allowlist={})}
    assert rows["db_rls_policies"]["status"] == "fail"
    assert rows["db_rls_policies"]["detail"]["keys"] == ["a:booking.ex_table"]
    assert rows["db_grants"]["status"] == "pass"


def test_a_public_grant_fails_the_grants_check_unless_allowlisted():
    dump = _dump(grants=[{"grantee": "PUBLIC", "privileges": "SELECT"}])
    rows = {r["check_id"]: r for r in compliance_db.evaluate_catalog(dump, NOW, allowlist={})}
    assert rows["db_grants"]["status"] == "fail" and rows["db_grants"]["detail"]["by_rule"]["p"] == 1
    ok = {r["check_id"]: r for r in compliance_db.evaluate_catalog(
        dump, NOW, allowlist={"p": {"booking.ex_table": "invented reason for the test"}})}
    assert ok["db_grants"]["status"] == "pass" and ok["db_grants"]["detail"]["allowlisted"] == 1


def test_monthly_review_check():
    assert compliance_db.evaluate_review(None, NOW)["status"] == "fail"
    row = compliance_db.evaluate_review(datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc), NOW)
    assert row["status"] == "pass" and row["detail"] == {"last_date": "2026-11-03"} and row["link"] is None


def test_asyncpg_placeholders():
    assert "$1" in compliance_db._asyncpg_sql(compliance_db.rls.CATALOG_SQL["relations"])
    assert "%(" not in compliance_db._asyncpg_sql(compliance_db.rls.CATALOG_SQL["policies"])


# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────────


class Pool:
    def __init__(self, exists=False):
        self.exists = exists

    async def fetchval(self, sql, *args):
        return self.exists


def test_daily_schedule(monkeypatch):
    monkeypatch.delenv(job.SWITCH_ENV, raising=False)
    with patch.object(job, "enqueue", AsyncMock(return_value=("j1", True))) as enq:
        early = asyncio.run(job.enqueue_daily(Pool(), now=NOW.replace(hour=job.SCHEDULE_HOUR_UTC - 1)))
        assert early[1] is False and not enq.await_count
        _id, created, day = asyncio.run(job.enqueue_daily(Pool(), now=NOW))
        assert created and day.isoformat() == "2026-11-24"
        assert enq.await_args.kwargs["idempotency_key"] == "compliance_db_checks:2026-11-24"
        assert asyncio.run(job.enqueue_daily(Pool(exists=True), now=NOW))[1] is False
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    assert asyncio.run(job.enqueue_daily(Pool(), now=NOW))[1] is False


def test_tick_part_only_when_registered(monkeypatch):
    monkeypatch.delenv(job.SWITCH_ENV, raising=False)
    out: dict = {}
    with patch.object(job, "enqueue", AsyncMock(return_value=("j1", True))):
        asyncio.run(job.tick_part(Pool(), {}, NOW, out))
        assert out == {}
        asyncio.run(job.tick_part(Pool(), {job.KIND: object()}, NOW, out))
    assert out[job.KIND]["day"] == "2026-11-24"


def test_registered_as_a_background_exempt_kind():
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = kinds()[job.KIND]
    assert k.background and k.deploy_gate_exempt and k.max_attempts == 2


def test_run_collects_and_ingests():
    rows = [{"check_id": "db_grants", "status": "pass"}, {"check_id": "db_rls_policies", "status": "fail"}]

    class Ctx:
        pool, payload, job_id = object(), {"trigger": "schedule"}, "j1"

        def set_result(self, r):
            self.result = r
    ctx = Ctx()
    with patch.object(compliance_db, "collect", AsyncMock(return_value=rows)), \
            patch.object(compliance, "ingest", AsyncMock(return_value=2)) as ing, \
            patch.object(compliance, "_validity_hours", AsyncMock(return_value=72)):
        out = asyncio.run(job.run(ctx))
    assert out["new"] == 2 and out["by_status"] == {"pass": 1, "fail": 1} and out["trigger"] == "schedule"
    assert ing.await_args.args[2] == 72
