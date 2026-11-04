"""AA-1028 R1 slice 3 on real Postgres (CIS 353-357 replayed): the plan catalogue, Tenant 360 overrides, the saga's
trial / cancel / Dev pilot, the hourly lifecycle tick, the readers that replaced PLAN_LIMITS and the AI budget read
from the entitlement.

Everything runs on one test connection inside an outer transaction that is rolled back (a pool shim hands it out, so
each module's own transaction is a savepoint). Names are invented.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.tenant_account import lifecycle  # noqa: E402
from services.tenant_account import plans as pl  # noqa: E402
from services.tenant_account import provision as pv  # noqa: E402
from services.tenant_account import subscription as sub  # noqa: E402
from shared import cost_guard  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ACTOR = "admin:zz-aa1028-s3"


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


async def _tenant(conn, tier="starter", add_ons=(), **kw) -> str:
    body = {"request_key": f"zz-s3-{uuid.uuid4().hex[:12]}", "mode": "create", "name": "Quillmere Voyages synthetic",
            "slug": f"zz1028s3-{uuid.uuid4().hex[:10]}", "plan_tier": tier, "posts_per_week": 3,
            "add_ons": list(add_ons), **kw}
    out = await pv.provision(_Pool(conn), pv.parse(body), job_id=str(uuid.uuid4()))
    return out["tenant_id"]


async def _ents(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.v_tenant_entitlement_resolved "
                            "WHERE tenant_id = $1::uuid", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


async def _stored(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.tenant_entitlement "
                            "WHERE tenant_id = $1::uuid", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


# ── the readers that replaced PLAN_LIMITS ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tier_rpm_comes_from_the_plans_with_the_old_numbers(conn):
    # the CIS 355 seed carries PLAN_LIMITS' values: behaviour is the same for every existing tier
    assert {t: await pl.tier_rate_limit_rpm(conn, t) for t in ("starter", "growth", "business", "internal")} == \
        {"starter": 60, "growth": 300, "business": 1000, "internal": 60}
    # Enterprise was not in PLAN_LIMITS; its plan sets an RPM (the Business value), so the admin may now assign it
    assert await pl.tier_rate_limit_rpm(conn, "enterprise") == 1000
    assert await pl.tier_rate_limit_rpm(conn, "platinum") is None
    assert set(await pl.assignable_tiers(conn)) == {"starter", "growth", "business", "enterprise", "internal"}
    await conn.execute("DELETE FROM shared.plan_feature WHERE plan_key = 'content.enterprise' "
                       "AND feature_key = 'content.rate_limit_rpm'")
    assert "enterprise" not in await pl.assignable_tiers(conn)
    assert (await pl.rate_limits_by_tier(conn))["growth"] == 300
    # a plan edit moves the number (the Plans page, not code)
    await conn.execute("UPDATE shared.plan_feature SET limit_value = 450, updated_by = 'zz' "
                       "WHERE plan_key = 'content.growth' AND feature_key = 'content.rate_limit_rpm'")
    assert await pl.tier_rate_limit_rpm(conn, "growth") == 450


@pytest.mark.asyncio
async def test_rewrite_quota_reads_the_entitlement(conn):
    from api.routers import v1_tours

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS shared.membership_plans (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(), plan_name text NOT NULL UNIQUE,
            tours_quota_monthly integer NOT NULL, api_calls_quota_monthly integer NOT NULL,
            price_usd_monthly numeric NOT NULL, overage_rate_usd_per_tour numeric NOT NULL,
            overage_rate_usd_per_1k_calls numeric NOT NULL, is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now())""")
    tid = await _tenant(conn, "growth")
    assert await v1_tours._get_tenant_plan_limit(conn, tid) == ("growth", 200)       # content.growth, as before
    await sub.set_override(conn, tid, "content.rewrite", enabled=True, limit_value="7", reason="zz test",
                           expires_at=None, actor=ACTOR)
    assert await v1_tours._get_tenant_plan_limit(conn, tid) == ("growth", 7)
    # an override of 0 is honoured (COALESCE must not skip it)
    await sub.set_override(conn, tid, "content.rewrite", enabled=True, limit_value="0", reason="zz test",
                           expires_at=None, actor=ACTOR)
    assert await v1_tours._get_tenant_plan_limit(conn, tid) == ("growth", 0)
    # a disabled override written before the S266 rule (set_override refuses it now) counts as 0, never the plan's
    await conn.execute("UPDATE shared.tenant_entitlement_override SET enabled = false, limit_value = NULL "
                       "WHERE tenant_id = $1::uuid AND feature_key = 'content.rewrite'", tid)
    assert await v1_tours._get_tenant_plan_limit(conn, tid) == ("growth", 0)


# ── the plan catalogue ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_create_edit_retire_with_versions_and_audit(conn):
    created = await pl.create_plan(conn, {
        "plan_key": "crm.zz_trailhead", "product": "crm", "name": "Trailhead CRM", "price_usd_monthly": "12.50",
        "seats_included": 3, "is_public": False,
        "features": [{"feature_key": "crm.core"}, {"feature_key": "crm.seats", "limit_value": 3}]}, actor=ACTOR)
    assert created["version"] == 1 and created["price_unit"] == "seat" and created["price_usd_monthly"] == "12.50"
    assert {f["feature_key"]: f["limit_value"] for f in created["features"]} == {"crm.core": None, "crm.seats": "3.00"}

    tid = await _tenant(conn, add_ons=[{"product": "crm", "plan_key": "crm.zz_trailhead"}])
    assert (await _ents(conn, tid))["crm.seats"] == (3.0, "plan")
    assert "crm.ai_assist" not in await _ents(conn, tid)

    # an edit made from version 1: the tenant's entitlements follow at once (materialised too)
    edited = await pl.update_plan(conn, "crm.zz_trailhead", {
        "version": 1, "name": "Trailhead CRM Plus",
        "features": [{"feature_key": "crm.core"}, {"feature_key": "crm.seats", "limit_value": 8},
                     {"feature_key": "crm.ai_assist"}]}, actor=ACTOR)
    assert edited["version"] == 2 and edited["name"] == "Trailhead CRM Plus"
    assert sorted(edited["changed"]) == sorted(["name", "feature:crm.ai_assist", "feature:crm.seats"])
    assert edited["live_subscriptions"] == 1
    assert (await _stored(conn, tid))["crm.seats"] == (8.0, "plan") and "crm.ai_assist" in await _stored(conn, tid)
    events = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                                 "AND type = 'entitlement.changed' AND actor = $2", tid, ACTOR)
    assert events >= 2

    with pytest.raises(pl.PlanError) as stale:
        await pl.update_plan(conn, "crm.zz_trailhead", {"version": 1, "name": "Lost edit"}, actor=ACTOR)
    assert stale.value.code == "version_conflict" and stale.value.status == 409
    unchanged = await pl.update_plan(conn, "crm.zz_trailhead", {"version": 2, "name": "Trailhead CRM Plus"},
                                     actor=ACTOR)
    assert unchanged["changed"] == [] and unchanged["version"] == 2

    retired = await pl.retire_plan(conn, "crm.zz_trailhead", version=2, reason="zz replaced", actor=ACTOR)
    assert retired["retired_at"] is not None and retired["version"] == 3
    assert (await _ents(conn, tid))["crm.seats"] == (8.0, "plan")              # current subscriber keeps it
    other = await _tenant(conn)
    with pytest.raises(pv.ProvisionError) as refused:
        await pv.provision(_Pool(conn), pv.parse({"request_key": f"zz-s3-{uuid.uuid4().hex[:10]}", "mode": "attach",
                                                  "tenant_id": other,
                                                  "add_ons": [{"product": "crm", "plan_key": "crm.zz_trailhead"}]}),
                           job_id=str(uuid.uuid4()))
    assert refused.value.code == "unknown_plan"
    with pytest.raises(pl.PlanError) as frozen:
        await pl.update_plan(conn, "crm.zz_trailhead", {"version": 3, "name": "x"}, actor=ACTOR)
    assert frozen.value.code == "plan_retired"
    assert [p["plan_key"] for p in await pl.list_plans(conn, product="crm")].count("crm.zz_trailhead") == 0
    assert "crm.zz_trailhead" in [p["plan_key"] for p in await pl.list_plans(conn, include_retired=True)]

    audit = await conn.fetch("SELECT action, actor, details FROM acp_shared.audit_log WHERE resource_type = 'plan' "
                             "AND resource_id = 'crm.zz_trailhead' ORDER BY created_at")
    assert [a["action"] for a in audit] == [pl.CREATED, pl.UPDATED, pl.RETIRED]
    upd = json.loads(audit[1]["details"])
    assert upd["before"]["features"]["crm.seats"] == "3.00" and upd["after"]["features"]["crm.seats"] == "8.00"
    assert all(a["actor"] == ACTOR for a in audit)


@pytest.mark.asyncio
async def test_plan_rules(conn):
    async def refused(payload, code):
        with pytest.raises(pl.PlanError) as e:
            await pl.create_plan(conn, payload, actor=ACTOR)
        assert e.value.code == code, e.value.message

    base = {"plan_key": "content.zz_custom", "product": "content", "name": "Custom content"}
    tiered = [{"feature_key": "content.rate_limit_rpm", "limit_value": 100},
              {"feature_key": "content.rewrite", "limit_value": 100},
              {"feature_key": "ai.budget_usd_monthly", "limit_value": 20}]
    await refused({**base, "tier": "growth", "features": tiered}, "tier_taken")
    # S266 review of slice 3: enforced limits listed, platform keys on content plans, bounded counts
    await refused({**base, "tier": "growth", "features": tiered[:2]}, "enforced_limit_required")
    await refused({**base, "tier": "growth", "features": tiered[:2] + [{"feature_key": "ai.budget_usd_monthly"}]},
                  "enforced_limit_required")
    await refused({**base, "plan_key": "crm.zz_ai", "product": "crm",
                   "features": [{"feature_key": "crm.core"}, {"feature_key": "ai.budget_usd_monthly",
                                                               "limit_value": 5}]}, "platform_key_content_only")
    await refused({**base, "features": [{"feature_key": "content.rewrite", "limit_value": 10_000_000_000}]},
                  "invalid_number")
    await refused({**base, "features": [{"feature_key": "content.rate_limit_rpm", "limit_value": 10001}]},
                  "invalid_number")
    await refused({**base, "plan_key": "crm.zz_x"}, "invalid_plan_key")
    await refused({**base, "features": [{"feature_key": "crm.core"}]}, "feature_product_mismatch")
    await refused({**base, "features": [{"feature_key": "content.social", "limit_value": 1}]}, "flag_takes_no_limit")
    await refused({**base, "features": [{"feature_key": "content.nope"}]}, "unknown_feature")
    await refused({**base, "features": [{"feature_key": "content.rewrite", "limit_value": "1.234"}]},
                  "invalid_number")
    await refused({**base, "plan_key": "content.growth"}, "plan_exists")
    await refused({**base, "plan_key": "crm.zz_t", "product": "crm", "tier": "starter"}, "invalid_tier")
    # a tier plan must set an RPM; retiring the tier's plan frees the tier for a new one
    await refused({**base, "tier": "enterprise", "features": []}, "rate_limit_required")
    await conn.execute("UPDATE shared.plan SET retired_at = now() WHERE plan_key = 'content.enterprise'")
    made = await pl.create_plan(conn, {**base, "tier": "enterprise",
                                       "features": [{"feature_key": "content.rate_limit_rpm", "limit_value": 2000},
                                                    *tiered[1:]]},
                                actor=ACTOR)
    assert made["tier"] == "enterprise" and await pl.tier_rate_limit_rpm(conn, "enterprise") == 2000
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "content.zz_custom", {"version": 1, "tier": "growth"}, actor=ACTOR)
    assert e.value.code == "immutable_field"
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "content.zz_custom", {"version": 1, "features": []}, actor=ACTOR)
    assert e.value.code == "rate_limit_required"
    # an edit cannot drop a limit the plan has ("no row" = no limit for every subscriber): set a value instead
    crm = await pl.create_plan(conn, {"plan_key": "crm.zz_seats", "product": "crm", "name": "Seats",
                                      "features": [{"feature_key": "crm.core"},
                                                   {"feature_key": "crm.seats", "limit_value": 4}]}, actor=ACTOR)
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "crm.zz_seats", {"version": crm["version"],
                                                    "features": [{"feature_key": "crm.core"}]}, actor=ACTOR)
    assert e.value.code == "limit_removal_refused"
    growth = await pl.get_plan(conn, "content.growth")
    with pytest.raises(pl.PlanError) as e:
        await pl.update_plan(conn, "content.growth", {"version": growth["version"], "features": [
            f for f in growth["features"] if f["feature_key"] != "ai.budget_usd_monthly"]}, actor=ACTOR)
    assert e.value.code == "enforced_limit_required"
    # a flag can be taken off; a limit set to "no limit" explicitly is accepted
    out = await pl.update_plan(conn, "crm.zz_seats", {"version": crm["version"],
                                                      "features": [{"feature_key": "crm.seats"}]}, actor=ACTOR)
    assert sorted(out["changed"]) == ["feature:crm.core", "feature:crm.seats"]


@pytest.mark.asyncio
async def test_a_plan_rpm_edit_moves_the_tenants_that_follow_it(conn):
    follower = await _tenant(conn, "growth")
    own = await _tenant(conn, "growth")
    await conn.execute("UPDATE shared.tenants SET rate_limit_rpm = 999 WHERE tenant_id = $1::uuid", own)  # PATCH
    assert (await _ents(conn, own))["content.rate_limit_rpm"] == (999.0, "override")
    version = (await pl.get_plan(conn, "content.growth"))["version"]
    features = [{"feature_key": f["feature_key"], "limit_value": f["limit_value"]}
                for f in (await pl.get_plan(conn, "content.growth"))["features"]
                if f["feature_key"] != pl.RPM_KEY] + [{"feature_key": pl.RPM_KEY, "limit_value": 450}]
    out = await pl.update_plan(conn, "content.growth", {"version": version, "features": features}, actor=ACTOR)
    assert out["changed"] == [f"feature:{pl.RPM_KEY}"] and out["rate_limit_followed"] >= 1
    rpm = {r["tenant_id"]: r["rate_limit_rpm"] for r in await conn.fetch(
        "SELECT tenant_id::text AS tenant_id, rate_limit_rpm FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])",
        [follower, own])}
    assert rpm == {follower: 450, own: 999}                     # the limiter's column; a real per-tenant RPM stays
    assert (await _ents(conn, follower))["content.rate_limit_rpm"] == (450.0, "plan")
    assert (await _stored(conn, follower))["content.rate_limit_rpm"] == (450.0, "plan")
    assert (await _ents(conn, own))["content.rate_limit_rpm"] == (999.0, "override")
    audit = await conn.fetchval("SELECT details FROM acp_shared.audit_log WHERE action = $1 AND resource_id = "
                                "'content.growth' ORDER BY created_at DESC LIMIT 1", pl.UPDATED)
    assert json.loads(audit)["rate_limit_followed"] == out["rate_limit_followed"]
    with pytest.raises(pl.PlanError) as e:
        features[-1]["limit_value"] = "450.5"
        await pl.update_plan(conn, "content.growth", {"version": version + 1, "features": features}, actor=ACTOR)
    assert e.value.code == "rate_limit_whole"


# ── Tenant 360 › Subscription ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_overrides_revoke_grant_and_limit(conn):
    tid = await _tenant(conn, add_ons=[{"product": "crm", "plan_key": "crm.core", "seats": 4}])
    assert "crm.ai_assist" in await _ents(conn, tid)

    out = await sub.set_override(conn, tid, "crm.ai_assist", enabled=False, limit_value=None,
                                 reason="zz revoke while reviewed", expires_at=None, actor=ACTOR)
    assert out["applies"] is True and out["resolved"] is None
    assert "crm.ai_assist" not in await _stored(conn, tid)                   # the revocation path

    out = await sub.set_override(conn, tid, "crm.campaigns", enabled=True, limit_value=None, reason="zz trial grant",
                                 expires_at=None, actor=ACTOR)
    assert out["resolved"] == {"limit_value": None, "source": "override"}
    out = await sub.set_override(conn, tid, "ai.budget_usd_monthly", enabled=True, limit_value=0.01,
                                 reason="zz fail-closed proof", expires_at=None, actor=ACTOR)
    assert out["resolved"] == {"limit_value": "0.01", "source": "override"}

    snap = await sub.snapshot(conn, tid)
    assert [s["product"] for s in snap["subscriptions"]] == ["content", "crm"]
    assert {o["feature_key"] for o in snap["overrides"]} == {"crm.ai_assist", "crm.campaigns", "ai.budget_usd_monthly"}
    ents = {e["feature_key"]: e for e in snap["entitlements"]}
    assert ents["ai.budget_usd_monthly"]["source"] == "override" and ents["crm.seats"]["limit_value"] == "4.00"
    assert ents["content.rewrite"]["description"] and snap["tenant"]["in_service"] is True
    assert {p["plan_key"] for p in snap["add_on_plans"]} >= {"crm.core", "crm.pro", "tripplanner.hosted"}

    removed = await sub.remove_override(conn, tid, "ai.budget_usd_monthly", actor=ACTOR)
    assert removed["resolved"] == {"limit_value": "15.00", "source": "plan"}
    with pytest.raises(pl.PlanError) as e:
        await sub.remove_override(conn, tid, "ai.budget_usd_monthly", actor=ACTOR)
    assert e.value.code == "override_not_found"
    for kw, code in (({"feature_key": "crm.core", "limit_value": 2}, "flag_takes_no_limit"),
                     ({"feature_key": "crm.seats", "limit_value": 2, "enabled": False}, "disabled_takes_no_limit"),
                     ({"feature_key": "crm.nope"}, "unknown_feature"),
                     ({"feature_key": "crm.core", "reason": " "}, "reason_required")):
        args = {"enabled": True, "limit_value": None, "reason": "zz", "expires_at": None, **kw}
        key = args.pop("feature_key")
        with pytest.raises(pl.PlanError) as e:
            await sub.set_override(conn, tid, key, actor=ACTOR, **args)
        assert e.value.code == code
    # no reader would enforce these (S266 review): refused, nothing written
    for key, kw, code in (("content.rate_limit_rpm", {"limit_value": 450}, "use_rate_limit_patch"),
                          ("ai.budget_usd_monthly", {"enabled": False}, "limit_cannot_be_disabled"),
                          ("content.rewrite", {"enabled": False}, "limit_cannot_be_disabled"),
                          ("content.rewrite", {}, "limit_required")):
        args = {"enabled": True, "limit_value": None, "reason": "zz", "expires_at": None, **kw}
        with pytest.raises(pl.PlanError) as e:
            await sub.set_override(conn, tid, key, actor=ACTOR, **args)
        assert e.value.code == code
    assert (await sub.snapshot(conn, tid))["override_rules"]["not_overridable"] == ["content.rate_limit_rpm"]
    actions = [r["action"] for r in await conn.fetch("SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1 "
                                                     "AND action LIKE 'tenant.entitlement_override%'", tid)]
    assert actions.count(sub.OVERRIDE_SET) == 3 and actions.count(sub.OVERRIDE_REMOVED) == 1


@pytest.mark.asyncio
async def test_cancel_add_on_through_the_saga(conn):
    tid = await _tenant(conn, add_ons=[{"product": "crm", "plan_key": "crm.core"},
                                       {"product": "tripplanner", "plan_key": "tripplanner.hosted"}])
    req = pv.parse({"request_key": f"zz-det-{uuid.uuid4().hex[:10]}", "mode": "detach", "tenant_id": tid,
                    "products": ["crm"]})
    out = await pv.provision(_Pool(conn), req, job_id=str(uuid.uuid4()))
    assert out["steps"] == ["crm_canceled"]
    status = {r["product"]: r["status"] for r in await conn.fetch(
        "SELECT product, status FROM shared.tenant_subscription WHERE tenant_id = $1::uuid", tid)}
    assert status == {"content": "active", "crm": "canceled", "tripplanner": "active"}
    ents = await _stored(conn, tid)
    assert not any(k.startswith("crm.") for k in ents) and "tp.hosted" in ents
    again = pv.parse({**req.to_payload(), "request_key": f"zz-det-{uuid.uuid4().hex[:10]}"})
    assert (await pv.provision(_Pool(conn), again, job_id=str(uuid.uuid4())))["steps"] == ["crm_unchanged"]


# ── trial, tick, Dev pilot ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trial_then_the_tick_ends_it(conn):
    tid = await _tenant(conn, "growth", add_ons=[{"product": "crm", "plan_key": "crm.core"}], trial_days=14)
    t = await conn.fetchrow("SELECT account_status, is_active FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    assert (t["account_status"], t["is_active"]) == ("trial", True)
    subs = {r["product"]: r for r in await conn.fetch(
        "SELECT product, status, trial_ends_at FROM shared.tenant_subscription WHERE tenant_id = $1::uuid", tid)}
    assert subs["content"]["status"] == subs["crm"]["status"] == "trialing"
    assert subs["content"]["trial_ends_at"] == subs["crm"]["trial_ends_at"]
    assert (await _stored(conn, tid))["content.rewrite"] == (200.0, "trial")

    # nothing is due yet
    assert (await lifecycle.end_trials(_Pool(conn), actor="job:tenant_account_tick:zz"))["tenants"] == 0
    # the trial ends (no payment recorded): past_due, service goes on, entitlements from the plan
    await conn.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '1 minute' "
                       "WHERE tenant_id = $1::uuid", tid)
    out = await lifecycle.tick(_Pool(conn), job_id="zz-job")
    assert out["trials"]["tenants"] == 1 and out["trials"]["subscriptions"] == {"past_due": 2}
    assert out["trials"]["accounts"] == {"past_due": 1} and out["trials"]["failed"] == 0
    t = await conn.fetchrow("SELECT account_status, is_active FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
    assert (t["account_status"], t["is_active"]) == ("past_due", True)
    assert (await _stored(conn, tid))["content.rewrite"] == (200.0, "plan")
    actor = lifecycle.actor_for("zz-job")
    types = {r["type"] for r in await conn.fetch("SELECT type FROM shared.tenant_account_event "
                                                 "WHERE tenant_id = $1::uuid AND actor = $2", tid, actor)}
    assert types >= {"subscription.changed", "account.status_changed", "entitlement.changed"}


@pytest.mark.asyncio
async def test_trial_with_payment_recorded_ends_active(conn):
    tid = await _tenant(conn, trial_days=7)
    await conn.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '1 minute', "
                       "paid_through = current_date + 30 WHERE tenant_id = $1::uuid", tid)
    out = await lifecycle.end_trials(_Pool(conn), actor="job:tenant_account_tick:zz")
    assert out["subscriptions"] == {"active": 1} and out["accounts"] == {"active": 1}
    assert await conn.fetchval("SELECT account_status FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == "active"


@pytest.mark.asyncio
async def test_dev_pilot_is_one_agency(conn, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    demo = await _tenant(conn)
    assert await conn.fetchval("SELECT kind FROM shared.tenants WHERE tenant_id = $1::uuid", demo) == "demo"
    with pytest.raises(pv.ProvisionError) as e:
        pv.parse({"request_key": "zz-s3-agency-01", "mode": "create", "name": "Real Agency", "slug": "zz-real",
                  "plan_tier": "starter", "posts_per_week": 3, "kind": "agency"})
    assert e.value.code == "dev_real_tenant_refused"
    await conn.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    pilot = await _tenant(conn, kind="agency", pilot_weeks=8)
    row = await conn.fetchrow("SELECT kind, is_pilot, pilot_ends_at - now() AS left FROM shared.tenants "
                              "WHERE tenant_id = $1::uuid", pilot)
    assert row["kind"] == "agency" and row["is_pilot"] is True and row["left"].days in (55, 56)
    with pytest.raises(pv.ProvisionError) as e:
        await _tenant(conn, kind="agency", pilot_weeks=2)
    assert e.value.code == "pilot_taken"
    await conn.execute("UPDATE shared.tenants SET pilot_ends_at = now() - interval '1 day' WHERE tenant_id = $1::uuid",
                       pilot)
    assert pilot in [p["tenant_id"] for p in await lifecycle.ended_pilots(_Pool(conn))]


# ── the gateway's tenant AI budget ───────────────────────────────────────────────────────────────────────────────

async def _spend(conn, tid, usd):
    await conn.execute("INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal) "
                       "VALUES ($1::uuid, 'zz_aa1028', 'writer', 'zz-model', $2, '{}'::jsonb)", tid, usd)


@pytest.mark.asyncio
async def test_the_ai_budget_is_the_entitlement(conn):
    tid = await _tenant(conn)                                              # starter: 15 USD included
    assert tid in await cost_guard.budgeted_tenants(conn)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.alert_pct, b.approval_pct, b.hard_stop) == (15.0, 80, 100, True)
    assert not b.needs_approval
    # the fail-closed proof's override: 0.01 USD, spend 0.02 -> new tenant-spend calls wait
    await sub.set_override(conn, tid, "ai.budget_usd_monthly", enabled=True, limit_value="0.01", reason="zz",
                           expires_at=None, actor=ACTOR)
    await _spend(conn, tid, 0.004)
    assert not (await cost_guard.tenant_budget_check(conn, tid)).needs_approval
    await _spend(conn, tid, 0.007)
    assert (await cost_guard.tenant_budget_check(conn, tid)).needs_approval
    # an AA-1016 row keeps its own thresholds (here: the bell only)
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, 50, 100, 150, false, 'zz')", tid)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.hard_stop, b.needs_approval) == (0.01, False, False)    # the override amount wins
    # AA itself (content.internal): no limit, not budgeted
    internal = await conn.fetchval("SELECT tenant_id::text FROM shared.tenants WHERE plan_tier = 'internal' LIMIT 1")
    if internal is None:
        internal = await _tenant(conn, "starter")
        await conn.execute("UPDATE shared.tenants SET plan_tier = 'internal' WHERE tenant_id = $1::uuid", internal)
    assert await cost_guard.tenant_budget_check(conn, internal) is None
    assert internal not in await cost_guard.budgeted_tenants(conn)


@pytest.mark.asyncio
async def test_shadow_spend_does_not_hold_the_tenant(conn):
    # S266 re-review: a shadow call carries the tenant but is AA's own A/B cost; the gateway, the AI budget tile and
    # Tenant 360 › Cost must read the same spend
    from api.services import tenant_ai_budget as tb

    tid = await _tenant(conn)                                              # starter: 15 USD, hard stop at 100 %
    await _spend(conn, tid, 7.5)
    for is_shadow, signal in ((True, "{}"), (None, '{"shadow": true}')):   # ledger v2 flag, then the older flag
        await conn.execute("INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, "
                           "is_shadow) VALUES ($1::uuid, 'zz_aa1028', 'writer', 'zz-model', 7.5, $2::jsonb, $3)",
                           tid, signal, is_shadow)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.month_spent_usd, b.month_calls, b.used_pct, b.needs_approval) == (7.5, 1, 50.0, False)
    out = await tb.read_one(_Pool(conn), tid)
    assert out["effective"]["state"] == "ok" and out["effective"]["used_pct"] == 50.0
    await _spend(conn, tid, 7.5)                                           # real spend reaches the 15 USD ceiling
    assert (await cost_guard.tenant_budget_check(conn, tid)).needs_approval


@pytest.mark.asyncio
async def test_a_disabled_or_zero_ai_budget_fails_closed(conn):
    tid = await _tenant(conn)                                              # starter, no AA-1016 row
    # a disabled override (written before set_override refused it): the resolver drops the key, cost_guard serves 0
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, enabled, reason, "
                       "granted_by) VALUES ($1::uuid, 'ai.budget_usd_monthly', false, 'zz', 'zz')", tid)
    assert "ai.budget_usd_monthly" not in await _ents(conn, tid)
    assert tid in await cost_guard.budgeted_tenants(conn)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.hard_stop, b.needs_approval) == (0.0, True, True)    # before any spend
    # even an AA-1016 row with the bell only does not reopen it
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, 50, 100, 150, false, 'zz')", tid)
    assert (await cost_guard.tenant_budget_check(conn, tid)).needs_approval
    # an expired one no longer counts
    await conn.execute("UPDATE shared.tenant_entitlement_override SET expires_at = now() - interval '1 minute' "
                       "WHERE tenant_id = $1::uuid", tid)
    assert (await cost_guard.tenant_budget_check(conn, tid)).monthly_usd == 50.0
    # "0 = no AI" through the Subscription tab: the first call already waits
    await conn.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = $1::uuid", tid)
    await sub.set_override(conn, tid, "ai.budget_usd_monthly", enabled=True, limit_value="0", reason="zz no AI",
                           expires_at=None, actor=ACTOR)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.needs_approval) == (0.0, True)
    # ... also when the tenant's AA-1016 row has the hard stop off (S266 review: 0 always stops)
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, 50, 100, 1000, false, 'zz')", tid)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.hard_stop, b.needs_approval) == (0.0, True, True)


@pytest.mark.asyncio
async def test_an_ended_trial_keeps_its_budget_until_the_tick(conn):
    # S266 review of slice 3: between trial_ends_at and the hourly tick the tenant keeps every key (CIS 361) and the
    # AI budget never reads as unlimited
    tid = await _tenant(conn, "starter", add_ons=[{"product": "crm", "plan_key": "crm.core"}], trial_days=3)
    before = await _ents(conn, tid)
    assert before["ai.budget_usd_monthly"] == (15.0, "trial") and "crm.core" in before
    await conn.execute("UPDATE shared.tenant_subscription SET trial_ends_at = now() - interval '5 minutes' "
                       "WHERE tenant_id = $1::uuid", tid)
    assert await _ents(conn, tid) == before                        # nothing changes at the trial end ...
    assert await _stored(conn, tid) == before
    assert tid in await cost_guard.budgeted_tenants(conn)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.hard_stop) == (15.0, True)
    await lifecycle.tick(_Pool(conn), job_id="zz-grace")          # ... everything follows the tick
    after = await _ents(conn, tid)
    assert after["ai.budget_usd_monthly"] == (15.0, "plan") and after["crm.core"][1] == "plan"


@pytest.mark.asyncio
async def test_the_budget_falls_back_to_the_content_plan(conn):
    # a customer whose resolver rows are absent (an unpaid / paused content subscription) keeps its plan's budget;
    # a canceled one (offboarded) has none
    tid = await _tenant(conn, "growth")
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'unpaid' WHERE tenant_id = $1::uuid "
                       "AND product = 'content'", tid)
    assert "ai.budget_usd_monthly" not in await _ents(conn, tid)
    assert tid in await cost_guard.budgeted_tenants(conn)
    b = await cost_guard.tenant_budget_check(conn, tid)
    assert (b.monthly_usd, b.alert_pct, b.approval_pct, b.hard_stop) == (40.0, 80, 100, True)
    from api.services import tenant_ai_budget as tb

    assert (await tb.read_one(_Pool(conn), tid))["effective"]["source"] == "plan"
    # an AA-1016 row still comes before the plan fallback
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1::uuid, 9, 'zz')", tid)
    assert (await cost_guard.tenant_budget_check(conn, tid)).monthly_usd == 9.0
    await conn.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = $1::uuid", tid)
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'canceled', canceled_at = now() "
                       "WHERE tenant_id = $1::uuid AND product = 'content'", tid)
    assert await cost_guard.tenant_budget_check(conn, tid) is None
    assert tid not in await cost_guard.budgeted_tenants(conn)


@pytest.mark.asyncio
async def test_a_platform_key_comes_from_the_content_plan_only(conn):
    # CIS 361: an add-on plan that carries a platform key (written by hand: the API refuses it) never wins
    tid = await _tenant(conn, "starter", add_ons=[{"product": "crm", "plan_key": "crm.core"}])
    await conn.execute("INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by) "
                       "VALUES ('crm.core', 'ai.budget_usd_monthly', 999, 'zz'), "
                       "('crm.core', 'policy.notice_days', 99, 'zz')")
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"] == (15.0, "plan") and ents["policy.notice_days"] == (7.0, "plan")
    plan_keys = {r["feature_key"]: r["plan_key"] for r in await conn.fetch(
        "SELECT feature_key, plan_key FROM shared.v_tenant_entitlement_resolved WHERE tenant_id = $1::uuid "
        "AND product = 'platform'", tid)}
    assert plan_keys == {"ai.budget_usd_monthly": "content.starter", "policy.notice_days": "content.starter"}


@pytest.mark.asyncio
async def test_the_cost_tile_shows_the_budget_the_gateway_applies(conn):
    from api.services import tenant_ai_budget as tb

    tid = await _tenant(conn)                                              # starter: 15 USD, no AA-1016 row
    out = await tb.read_one(_Pool(conn), tid)
    assert out["budget"] is None
    assert out["effective"] == {"source": "plan", "monthly_usd": 15.0, "alert_pct": 80, "approval_pct": 100,
                                "hard_stop": True, "used_pct": 0.0, "state": "ok"}
    await _spend(conn, tid, 15.0)
    eff = (await tb.read_one(_Pool(conn), tid))["effective"]
    assert (eff["state"], eff["used_pct"]) == ("approval", 100.0)        # not "unlimited": the plan holds at 100 %
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, "
                       "hard_stop, updated_by) VALUES ($1::uuid, 60, 100, 150, false, 'zz')", tid)
    eff = (await tb.read_one(_Pool(conn), tid))["effective"]
    assert (eff["source"], eff["monthly_usd"], eff["state"]) == ("budget_row", 60.0, "ok")
    await sub.set_override(conn, tid, "ai.budget_usd_monthly", enabled=True, limit_value="5", reason="zz",
                           expires_at=None, actor=ACTOR)
    eff = (await tb.read_one(_Pool(conn), tid))["effective"]
    assert (eff["source"], eff["monthly_usd"]) == ("subscription_override", 5.0)  # wins over the tile's row
    await sub.remove_override(conn, tid, "ai.budget_usd_monthly", actor=ACTOR)
    assert (await tb.clear(_Pool(conn), tid, actor=ACTOR))["deleted"] is True
    assert (await tb.read_one(_Pool(conn), tid))["effective"]["source"] == "plan"   # remove = back to the plan
