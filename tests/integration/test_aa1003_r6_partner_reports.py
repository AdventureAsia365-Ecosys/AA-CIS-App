"""AA-1003 R6 against real Postgres: every SQL the EX-03 and EX-07 readers run, as written, on the replayed schema
(shared.tenants, shared.tenant_subscription, shared.plan, shared.tenant_rewrite_usage, the onboarding step subqueries
over shared.tenant_membership_event, shared.tenant_brand_rules, acp_shared.content_piece, acp_shared.publish_log and
shared.tenant_seo_config, and the rewrite quota the API enforces), then each renderer on what the collectors returned.

Everything runs on one connection inside an outer transaction rolled back at the end (a pool shim hands it to the
tenant provisioning saga). Tenants are provisioned with invented names; the thresholds are fixture values (the
settings reader is not involved). No crm.ops.v1 call: the CRM part is the scheduled-run "not read" line.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
import asyncpg

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.reports import money, partners, registry  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
TH = {"activation_days": 14, "stuck_days": 7, "upsell_pct": 80, "low_use_pct": 10, "low_use_months": 2, "missing": []}


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    await c.execute("ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS plan_id uuid")
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _agency(conn) -> str:
    body = {"request_key": f"zz-r6-{uuid.uuid4().hex[:12]}", "mode": "create", "name": "Pellucid Trails synthetic",
            "slug": f"zz1003r6-{uuid.uuid4().hex[:10]}", "plan_tier": "starter", "posts_per_week": 3,
            "kind": "agency"}
    out = await pv.provision(_Pool(conn), pv.parse(body), job_id=str(uuid.uuid4()))
    return out["tenant_id"]


def _period():
    today = datetime.now(UTC).date()
    return today.replace(day=1), today


def _unread(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if money.failed(v)}


@pytest.mark.asyncio
async def test_ex03_reads_subscriptions_plans_usage_quota_and_steps(conn):
    reg = registry.load()
    tid = await _agency(conn)
    first, last = _period()
    await conn.execute("INSERT INTO shared.tenant_rewrite_usage (tenant_id, year_month, rewrite_count, updated_at) "
                       "VALUES ($1::uuid, $2, 3, now()) ON CONFLICT DO NOTHING", tid, last.strftime("%Y-%m"))
    facts = await partners.collect_ex03(conn, first, last, TH)
    assert _unread(facts) == {}, facts
    assert tid in facts["quotas"] and isinstance(facts["quotas"][tid], int)
    assert any(s["tenant_id"] == tid for s in facts["steps"])
    assert any(r["tenant_id"] == tid for r in facts["rewrites"])
    out = partners.render_ex03(reg, reg.report("EX-03"), first, last, facts, TH, None, datetime.now(UTC))
    assert [k["id"] for k in out.output["kpis"]] == list(reg.report("EX-03").kpis)
    assert all(k["status"] != "no_data" for k in out.output["kpis"]), out.output["kpis"]
    dumped = json.dumps(out.output, default=str) + out.markdown
    assert tid not in dumped and "Pellucid" not in dumped and partners.NOT_READ in out.markdown
    assert out.output["growth"]["partners"] >= 1


@pytest.mark.asyncio
async def test_ex07_reads_the_onboarding_steps(conn):
    reg = registry.load()
    tid = await _agency(conn)
    first, last = _period()
    facts = await partners.collect_ex07(conn, first, last)
    assert _unread(facts) == {}, facts
    mine = next(s for s in facts["steps"] if s["tenant_id"] == tid)
    assert mine["publish_at"] is None and mine["has_seo_config"] in (True, False)
    later = datetime.now(UTC) + timedelta(days=30)            # a month later the new agency is stuck at "created"
    fn = partners.funnel(facts["steps"], first, last, TH["stuck_days"], later)
    assert tid[:8] in {r["partner"] for r in fn["stuck"]}
    out = partners.render_ex07(reg, reg.report("EX-07"), first, last, facts, TH, None, datetime.now(UTC))
    assert [k["id"] for k in out.output["kpis"]] == list(reg.report("EX-07").kpis)
    assert tid not in json.dumps(out.output, default=str)
    assert date.fromisoformat(out.output["period"]["end"]) == last
