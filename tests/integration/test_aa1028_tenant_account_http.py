"""AA-1028 R1 — services/tenant_account on real Postgres (CIS 353-355 replayed by conftest): the account snapshot and
the account event feed, end to end through the CIS 353 / 354 triggers, as the CIS runtime (cis_app), and every body
against the contract tenant.entitlements.v1.

The feed only serves committed transactions older than every open one, so this module commits its rows (a synthetic
tenant) and removes them at teardown with the append-only triggers skipped (superuser, session_replication_role =
replica, test DB only), as test_aa978_membership_service does.
"""
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.tenant_account import feed, reader  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DOC = yaml.safe_load((Path(__file__).resolve().parents[2] / "docs" / "contracts"
                      / "tenant.entitlements.v1.openapi.yaml").read_text(encoding="utf-8"))


def _valid(name, body):
    v = Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": DOC["components"]})
    errors = sorted(v.iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]


async def _cleanup(conn, tid):
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        for table in ("tenant_account_event", "tenant_membership_event", "tenant_entitlement",
                      "tenant_entitlement_override", "tenant_ai_budget", "tenant_subscription"):
            await conn.execute(f"DELETE FROM shared.{table} WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)


@pytest_asyncio.fixture
async def world():
    """A committed synthetic tenant on Content Growth, a CRM trial, and cis_app's runtime grants (other modules
    re-create cis_app, so they are re-applied)."""
    admin = await asyncpg.connect(DSN)
    tid = uuid.uuid4()
    await admin.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await admin.execute("GRANT SELECT ON shared.tenants, shared.tenant_subscription, shared.v_tenant_entitlement_v1, "
                        "shared.tenant_account_event TO cis_app")
    try:
        await admin.execute(
            "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
            "time_zone) VALUES ($1, 'AA-1028 synthetic', $2, 'growth', 3, 300, true, 'Asia/Bangkok')",
            tid, f"zz1028h-{tid.hex[:12]}")
        # the content subscription comes from the create (CIS 355 bridge), on the live plan of the tier
        assert await admin.fetchval("SELECT plan_key FROM shared.tenant_subscription WHERE tenant_id = $1 "
                                    "AND product = 'content'", tid) == "content.growth"
        await admin.execute(
            "INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, trial_ends_at, seats, "
            "updated_by) VALUES ($1, 'crm', 'crm.core', 'trialing', now() + interval '14 days', 3, 'admin:test')", tid)
        yield admin, str(tid)
    finally:
        await _cleanup(admin, tid)
        await admin.close()


async def _as_cis_app():
    c = await asyncpg.connect(DSN)
    await c.execute("SET ROLE cis_app")
    return c


async def _all_events(conn, tid, limit=500):
    out, after = [], None
    while True:
        page = await feed.list_events(conn, after, limit)
        _valid("AccountEventPage", page)
        out += [e for e in page["items"] if e["tenant_id"] == tid]
        if not page["items"]:
            return out
        after = page["next_cursor"]


@pytest.mark.asyncio
async def test_snapshot_as_cis_app_conforms(world):
    _, tid = world
    c = await _as_cis_app()
    try:
        async with c.transaction(readonly=True):
            data = await reader.tenant_account(c, tid)
            assert await reader.tenant_account(c, str(uuid.uuid4())) is None
    finally:
        await c.close()
    _valid("TenantAccount", {**data, "as_of": "2026-11-03T00:00:00.000000Z"})
    assert data["account_status"] == "active" and data["in_service"] is True and data["time_zone"] == "Asia/Bangkok"
    assert [(s["product"], s["status"], s["seats"]) for s in data["subscriptions"]] == [
        ("content", "active", None), ("crm", "trialing", 3)]
    ents = {e["feature_key"]: e for e in data["entitlements"]}
    assert ents["ai.budget_usd_monthly"]["limit"] == "40.00" and ents["ai.budget_usd_monthly"]["source"] == "plan"
    assert ents["crm.seats"]["limit"] == "3.00" and ents["crm.seats"]["source"] == "trial"
    assert ents["crm.seats"]["expires_at"].endswith("Z")
    assert ents["content.social"]["limit"] is None and ents["content.social"]["kind"] == "flag"
    assert "crm.campaigns" not in ents                                  # crm.pro only


@pytest.mark.asyncio
async def test_feed_tells_the_story_in_order(world):
    admin, tid = world
    await admin.execute("UPDATE shared.tenants SET account_status = 'suspended', suspend_reason = 'billing' "
                        "WHERE tenant_id = $1::uuid", tid)
    await admin.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                        "VALUES ($1::uuid, 2.5, 'admin:test')", tid)
    c = await _as_cis_app()
    try:
        async with c.transaction(readonly=True):
            events = await _all_events(c, tid)
            data = await reader.tenant_account(c, tid)
    finally:
        await c.close()
    types = [e["type"] for e in events]
    assert types[0] == "account.status_changed" and events[0]["change"]["previous_status"] is None
    assert types[1] == "subscription.changed" and events[1]["change"]["product"] == "content"
    status = [e["change"] for e in events if e["type"] == "account.status_changed"]
    assert status[-1] == {"status": "suspended", "previous_status": "active", "suspend_reason": "billing"}
    last = events[-1]
    assert last["type"] == "entitlement.changed"
    assert last["change"] == {"op": "updated", "feature_key": "ai.budget_usd_monthly", "product": "platform",
                              "limit": "2.50", "source": "override", "expires_at": None}
    assert data["in_service"] is False and data["suspend_reason"] == "billing"
    # the snapshot still lists the entitlements: in_service false is what makes a consumer refuse them
    assert {e["feature_key"]: e["limit"] for e in data["entitlements"]}["ai.budget_usd_monthly"] == "2.50"


@pytest.mark.asyncio
async def test_an_open_transaction_holds_the_feed_back(world):
    admin, tid = world
    other = await asyncpg.connect(DSN)
    try:
        tr = other.transaction()
        await tr.start()
        await other.execute("UPDATE shared.tenants SET account_status = 'past_due' WHERE tenant_id = $1::uuid", tid)
        before = await _all_events(admin, tid)
        assert all(e["change"].get("status") != "past_due" for e in before)
        await tr.commit()
    finally:
        await other.close()
    after = await _all_events(admin, tid)
    assert [e["change"]["status"] for e in after if e["type"] == "account.status_changed"][-1] == "past_due"


@pytest.mark.asyncio
async def test_unavailable_before_the_migrations():
    """The reader turns a missing control-plane object into AccountUnavailable (the router's 503)."""
    conn = await asyncpg.connect(DSN)

    class _Pool:
        async def acquire(self, timeout=None):
            return conn

        async def release(self, c):
            pass
    try:
        with pytest.raises(reader.AccountUnavailable):
            async with reader.account_connection(_Pool()) as c:
                await c.fetch("SELECT * FROM shared.tenant_account_event_missing")
    finally:
        await conn.close()
