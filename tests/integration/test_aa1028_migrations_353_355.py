"""AA-1028 R1 slice 1 — CIS 353 / 354 / 355 (the tenant control plane) on real Postgres.

conftest replays the three files like every migration. Each test runs in one transaction that is rolled back.

  * the files applied, a re-run of each is a no-op, and the grants are exact (cis_app only; nothing for PUBLIC,
    aa_app_user or catalog_owner; the AA-859 invariant v3 finds nothing on these objects);
  * the resolver: plan default, trial source and end, override > legacy shared.tenant_ai_budget > plan, a disabled
    override hides a key, an expired override is ignored, seats from the subscription, an add-on override needs the
    add-on's live subscription while a platform key does not;
  * the guards: add-on without a live content row, content leaving the live set under a live add-on, a retired plan,
    a feature of another product, a limit on a flag, direct writes to the materialised table and to the feed;
  * the materialisation and the feed: subscription.changed then one entitlement.changed per key, a plan_feature or
    a legacy AI budget change re-syncs the tenants it touches, sync_due() picks up an expired override, the
    erasure of an offboarded tenant deletes and emits nothing;
  * shared.tenants: every legacy admin write path keeps is_active / cancelled_at as before and derives
    account_status; a status write leads the legacy columns and still feeds the 276 membership feed; the
    vocabularies, the time zone, the single pilot; as cis_app with its runtime grants.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import KEY_HASH_STANDIN_SQL, NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
OBJECTS = ("entitlement_feature", "plan", "plan_feature", "tenant_subscription", "tenant_entitlement_override",
           "tenant_entitlement", "tenant_account_event", "v_tenant_entitlement_resolved", "v_tenant_entitlement_v1")


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


async def _tenant(conn, *, tier="starter", active=True, offboarded=False, canary=False) -> uuid.UUID:
    """The INSERT of POST /admin/tenants (api/routers/admin.py), with an offboarded / canary variant."""
    tid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, "
        "is_active, cancelled_at, is_canary) VALUES ($1, 'AA-1028 synthetic', $2, $3::plan_tier_enum, 3, 60, $4, "
        "CASE WHEN $5 THEN now() END, $6)", tid, f"zz1028-{tid.hex[:12]}", tier, active, offboarded, canary)
    return tid


async def _subscribe(conn, tid, product="content", plan="content.starter", status="active", **kw):
    """Set the tenant's subscription of a product (the content row exists from the create: the 355 bridge)."""
    trial = kw.get("trial_ends_at") or (datetime.now(timezone.utc) + timedelta(days=14) if status == "trialing"
                                        else None)
    await conn.execute(
        "INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, trial_ends_at, seats, "
        "canceled_at, updated_by) VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $4 = 'canceled' THEN now() END, "
        "'admin:test') ON CONFLICT (tenant_id, product) DO UPDATE SET plan_key = EXCLUDED.plan_key, "
        "status = EXCLUDED.status, trial_ends_at = EXCLUDED.trial_ends_at, seats = EXCLUDED.seats, "
        "canceled_at = EXCLUDED.canceled_at, updated_by = EXCLUDED.updated_by",
        tid, product, plan, status, trial, kw.get("seats"))


async def _sub(conn, tid, product="content"):
    r = await conn.fetchrow("SELECT plan_key, status, canceled_at IS NOT NULL AS canceled FROM "
                            "shared.tenant_subscription WHERE tenant_id = $1 AND product = $2", tid, product)
    return None if r is None else (r["plan_key"], r["status"], r["canceled"])


async def _ents(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source, expires_at FROM shared.v_tenant_entitlement_v1 "
                            "WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


async def _materialised(conn, tid) -> dict:
    rows = await conn.fetch("SELECT feature_key, limit_value, source FROM shared.tenant_entitlement "
                            "WHERE tenant_id = $1", tid)
    return {r["feature_key"]: (None if r["limit_value"] is None else float(r["limit_value"]), r["source"])
            for r in rows}


async def _events(conn, tid, type_=None) -> list:
    rows = await conn.fetch("SELECT type, change FROM shared.tenant_account_event WHERE tenant_id = $1 "
                            "AND ($2::text IS NULL OR type = $2) ORDER BY txid, seq", tid, type_)
    return [(r["type"], json.loads(r["change"])) for r in rows]


async def _refused(conn, sqlstate, sql, *args):
    with pytest.raises(asyncpg.PostgresError) as info:
        async with conn.transaction():
            await conn.execute(sql, *args)
    assert info.value.sqlstate == sqlstate, info.value


# ── the files ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_files_applied_and_rerun_is_a_noop(conn):
    for v in ("353", "354", "355"):
        assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = $1", v), \
            f"conftest swallowed an error in {v} (see its log)"
    before = await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event")
    ents = await conn.fetchval("SELECT count(*) FROM shared.tenant_entitlement")
    await conn.execute(KEY_HASH_STANDIN_SQL)   # AA-1036: 354 / 355's probe INSERT names the column CIS 369 dropped
    for v in (353, 354, 355):
        await conn.execute(migration_body(v))
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event") == before
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_entitlement") == ents


@pytest.mark.asyncio
async def test_grants_are_exact(conn):
    await conn.execute("GRANT SELECT ON shared.plan, shared.tenant_account_event TO aa_app_user, catalog_owner, PUBLIC")
    await conn.execute("GRANT UPDATE ON shared.tenant_account_event TO cis_app")
    await conn.execute("GRANT USAGE ON SEQUENCE shared.tenant_account_event_seq_seq TO cis_app")
    await conn.execute(KEY_HASH_STANDIN_SQL)   # AA-1036: 355's probe INSERT names the column CIS 369 dropped
    for v in (353, 355):
        await conn.execute(migration_body(v))
    rows = await conn.fetch("""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, c.relname AS o,
               a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.relnamespace = 'shared'::regnamespace AND c.relname = ANY($1::text[])
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole, 'catalog_owner'::regrole))
    """, list(OBJECTS))
    got = sorted((r["r"], r["o"], r["p"]) for r in rows)
    rw = ("SELECT", "INSERT", "UPDATE", "DELETE")
    want = [("cis_app", "entitlement_feature", "SELECT"), ("cis_app", "v_tenant_entitlement_resolved", "SELECT"),
            ("cis_app", "v_tenant_entitlement_v1", "SELECT")]
    want += [("cis_app", "plan", p) for p in ("SELECT", "INSERT", "UPDATE")]
    want += [("cis_app", o, p) for o in ("plan_feature", "tenant_subscription", "tenant_entitlement_override",
                                         "tenant_entitlement") for p in rw]
    want += [("cis_app", "tenant_account_event", p) for p in ("SELECT", "INSERT", "DELETE")]
    assert got == sorted(want)
    assert await conn.fetchval(
        "SELECT relacl IS NULL OR NOT EXISTS (SELECT 1 FROM aclexplode(relacl) a WHERE a.grantee = 0 OR a.grantee IN "
        "('cis_app'::regrole, 'aa_app_user'::regrole, 'catalog_owner'::regrole)) AS ok FROM pg_class "
        "WHERE oid = 'shared.tenant_account_event_seq_seq'::regclass")
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3)
           if any(o in r["rel"] for o in ("tenant_entitlement", "tenant_subscription", "tenant_account_event"))]
    assert bad == []


@pytest.mark.asyncio
async def test_seed_policy_values(conn):
    prices = {r["plan_key"]: r["price_usd_monthly"] for r in await conn.fetch(
        "SELECT plan_key, price_usd_monthly FROM shared.plan")}
    assert {k: (None if v is None else float(v)) for k, v in prices.items()} == {
        "content.starter": 299.0, "content.growth": 799.0, "content.business": 1499.0, "content.enterprise": None,
        "content.internal": None, "crm.core": None, "crm.pro": None, "tripplanner.hosted": None,
        "tripplanner.pro": None}
    ai = {r["plan_key"]: r["limit_value"] for r in await conn.fetch(
        "SELECT plan_key, limit_value FROM shared.plan_feature WHERE feature_key = 'ai.budget_usd_monthly'")}
    assert {k: (None if v is None else float(v)) for k, v in ai.items()} == {
        "content.starter": 15.0, "content.growth": 40.0, "content.business": 80.0, "content.enterprise": 80.0,
        "content.internal": None}
    units = {r["plan_key"]: r["price_unit"] for r in await conn.fetch("SELECT plan_key, price_unit FROM shared.plan")}
    assert units["crm.core"] == "seat" and units["tripplanner.hosted"] == "site" and units["content.growth"] == "tenant"
    # every existing tenant got its content subscription (355 backfill)
    assert await conn.fetchval("SELECT count(*) FROM shared.tenants t WHERE NOT EXISTS (SELECT 1 FROM "
                               "shared.tenant_subscription s WHERE s.tenant_id = t.tenant_id)") == 0


# ── the resolver ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_defaults_and_trial(conn):
    tid = await _tenant(conn)
    assert await _sub(conn, tid) == ("content.starter", "active", False)      # the 355 bridge at create
    ents = await _ents(conn, tid)
    assert ents["content.rewrite"] == (50.0, "plan") and ents["content.rate_limit_rpm"] == (60.0, "plan")
    assert await _materialised(conn, tid) == ents
    await _subscribe(conn, tid, status="trialing")
    ents = await _ents(conn, tid)
    assert ents["ai.budget_usd_monthly"] == (15.0, "trial")
    assert ents["content.rewrite"] == (50.0, "trial")
    assert ents["content.social"] == (None, "trial")
    assert ents["policy.notice_days"] == (7.0, "trial")
    assert "crm.core" not in ents
    exp = await conn.fetchval("SELECT expires_at FROM shared.tenant_entitlement WHERE tenant_id = $1 "
                              "AND feature_key = 'content.rewrite'", tid)
    assert exp is not None
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'active', plan_key = 'content.business' "
                       "WHERE tenant_id = $1", tid)
    ents = await _ents(conn, tid)
    # CIS 356: the tenant's own rate_limit_rpm (60) differs from the business plan's 1,000, so it is the override
    assert ents["ai.budget_usd_monthly"] == (80.0, "plan") and ents["content.rate_limit_rpm"] == (60.0, "override")
    await conn.execute("UPDATE shared.tenants SET rate_limit_rpm = 1000 WHERE tenant_id = $1", tid)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (1000.0, "plan")
    ents = await _ents(conn, tid)
    assert await _materialised(conn, tid) == ents
    await conn.execute("UPDATE shared.tenant_subscription SET status = 'unpaid' WHERE tenant_id = $1", tid)
    assert await _ents(conn, tid) == {} and await _materialised(conn, tid) == {}


@pytest.mark.asyncio
async def test_override_beats_legacy_budget_beats_plan(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid, plan="content.growth")
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (40.0, "plan")
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1, 5, 'admin:test')", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (5.0, "override")
    await conn.execute("UPDATE shared.tenant_ai_budget SET monthly_usd = NULL WHERE tenant_id = $1", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (None, "override")     # NULL = unlimited (321)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by) VALUES ($1, 'ai.budget_usd_monthly', 0.01, 'fail-closed probe', 'admin:test')", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (0.01, "override")
    await conn.execute("DELETE FROM shared.tenant_entitlement_override WHERE tenant_id = $1", tid)
    await conn.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = $1", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (40.0, "plan")
    assert await _materialised(conn, tid) == await _ents(conn, tid)


@pytest.mark.asyncio
async def test_disabled_expired_and_product_scoped_overrides(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    ins = ("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, enabled, reason, "
           "granted_by, expires_at) VALUES ($1, $2, $3, $4, 'test', 'admin:test', $5)")
    await conn.execute(ins, tid, "content.social", None, False, None)
    assert "content.social" not in await _ents(conn, tid)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await conn.execute(ins, tid, "content.rewrite", 999, True, past)
    assert (await _ents(conn, tid))["content.rewrite"] == (50.0, "plan")
    # an add-on key needs the add-on's live subscription; a platform key does not
    await conn.execute(ins, tid, "tp.branding", None, True, None)
    assert "tp.branding" not in await _ents(conn, tid)
    await _subscribe(conn, tid, product="tripplanner", plan="tripplanner.hosted")
    ents = await _ents(conn, tid)
    assert ents["tp.branding"] == (None, "override") and ents["tp.hosted"] == (None, "plan")
    assert "tp.api" not in ents


@pytest.mark.asyncio
async def test_seats_come_from_the_subscription(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    await _subscribe(conn, tid, product="crm", plan="crm.core")
    assert (await _ents(conn, tid))["crm.seats"] == (10.0, "plan")
    await conn.execute("UPDATE shared.tenant_subscription SET seats = 4 WHERE tenant_id = $1 AND product = 'crm'", tid)
    assert (await _ents(conn, tid))["crm.seats"] == (4.0, "plan")


@pytest.mark.asyncio
async def test_sync_due_picks_up_an_expired_override(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by, expires_at) VALUES ($1, 'content.rewrite', 75, 'promo', 'admin:test', $2)",
                       tid, soon)
    assert (await _materialised(conn, tid))["content.rewrite"] == (75.0, "override")
    await conn.execute("ALTER TABLE shared.tenant_entitlement_override DISABLE TRIGGER tenant_entitlement_override_resync")
    await conn.execute("UPDATE shared.tenant_entitlement_override SET expires_at = now() - interval '1 minute' "
                       "WHERE tenant_id = $1", tid)
    await conn.execute("ALTER TABLE shared.tenant_entitlement_override ENABLE TRIGGER tenant_entitlement_override_resync")
    await conn.execute("SET LOCAL aa.tenant_entitlement_sync = 'on'")
    await conn.execute("UPDATE shared.tenant_entitlement SET expires_at = now() - interval '1 minute' "
                       "WHERE tenant_id = $1 AND feature_key = 'content.rewrite'", tid)
    await conn.execute("SET LOCAL aa.tenant_entitlement_sync = ''")
    assert (await _ents(conn, tid))["content.rewrite"] == (50.0, "plan")            # the view is live
    assert await conn.fetchval("SELECT shared.tenant_entitlement_sync_due()") >= 1
    assert (await _materialised(conn, tid))["content.rewrite"] == (50.0, "plan")


# ── guards ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_and_plan_feature_guards(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid, status="unpaid")
    await _refused(conn, "23514", "INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, "
                   "updated_by) VALUES ($1, 'crm', 'crm.core', 'active', 'admin:test')", tid)
    await _subscribe(conn, tid)
    await _subscribe(conn, tid, product="crm", plan="crm.core")
    await _refused(conn, "23514", "UPDATE shared.tenant_subscription SET status = 'unpaid' "
                   "WHERE tenant_id = $1 AND product = 'content'", tid)
    await _refused(conn, "23503", "UPDATE shared.tenant_subscription SET plan_key = 'crm.core' "
                   "WHERE tenant_id = $1 AND product = 'content'", tid)          # plan of another product (FK)
    await _refused(conn, "23001", "UPDATE shared.tenant_subscription SET tenant_id = gen_random_uuid() "
                   "WHERE tenant_id = $1 AND product = 'crm'", tid)
    await conn.execute("UPDATE shared.plan SET retired_at = now() WHERE plan_key = 'crm.pro'")
    await _refused(conn, "23514", "UPDATE shared.tenant_subscription SET plan_key = 'crm.pro' "
                   "WHERE tenant_id = $1 AND product = 'crm'", tid)
    await _refused(conn, "23514", "INSERT INTO shared.plan_feature (plan_key, feature_key, updated_by) "
                   "VALUES ('content.growth', 'crm.campaigns', 'admin:test')")
    await _refused(conn, "23514", "INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by) "
                   "VALUES ('crm.core', 'crm.campaigns', 3, 'admin:test')")
    await _refused(conn, "23514", "INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, "
                   "updated_by) VALUES ($1, 'tripplanner', 'tripplanner.hosted', 'trialing', 'admin:test')", tid)


@pytest.mark.asyncio
async def test_materialised_table_and_feed_are_written_only_by_their_functions(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    await _refused(conn, "23001", "INSERT INTO shared.tenant_entitlement (tenant_id, feature_key, product, value_kind, "
                   "source) VALUES ($1, 'crm.core', 'crm', 'flag', 'plan')", tid)
    await _refused(conn, "23001", "UPDATE shared.tenant_entitlement SET limit_value = 1 WHERE tenant_id = $1", tid)
    await _refused(conn, "23001", "DELETE FROM shared.tenant_entitlement WHERE tenant_id = $1", tid)
    await _refused(conn, "23001", "INSERT INTO shared.tenant_account_event (type, tenant_id, change) "
                   "VALUES ('entitlement.changed', $1, '{}')", tid)
    await _refused(conn, "23001", "UPDATE shared.tenant_account_event SET change = '{}' WHERE tenant_id = $1", tid)
    await _refused(conn, "23001", "DELETE FROM shared.tenant_account_event WHERE tenant_id = $1", tid)
    await _refused(conn, "23001", "TRUNCATE shared.tenant_account_event")


# ── materialisation and the feed ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_events_of_a_subscription_and_a_plan_change(conn):
    await conn.execute("SET LOCAL aa.actor = 'admin:test-actor'")
    tid = await _tenant(conn)                                     # the create writes its content subscription
    evs = await _events(conn, tid)
    assert evs[0][0] == "account.status_changed"
    assert evs[1] == ("subscription.changed", {"op": "created", "product": "content", "plan_key": "content.starter",
                                               "status": "active", "previous_status": None,
                                               "previous_plan_key": None, "seats": None, "trial_ends_at": None})
    ent = [c for t, c in evs if t == "entitlement.changed"]
    assert len(ent) == len(await _ents(conn, tid)) and all(c["op"] == "added" for c in ent)
    budget = next(c for c in ent if c["feature_key"] == "ai.budget_usd_monthly")
    assert budget == {"op": "added", "feature_key": "ai.budget_usd_monthly", "product": "platform",
                      "limit": "15.00", "source": "plan", "expires_at": None}
    assert await conn.fetchval("SELECT DISTINCT actor FROM shared.tenant_account_event WHERE tenant_id = $1",
                               tid) == "admin:test-actor"
    # a plan value change re-syncs the tenants on that plan: one updated event
    n = len(await _events(conn, tid))
    await conn.execute("UPDATE shared.plan_feature SET limit_value = 20 WHERE plan_key = 'content.starter' "
                       "AND feature_key = 'ai.budget_usd_monthly'")
    new = (await _events(conn, tid))[n:]
    assert new == [("entitlement.changed", {"op": "updated", "feature_key": "ai.budget_usd_monthly",
                                            "product": "platform", "limit": "20.00", "source": "plan",
                                            "expires_at": None})]
    # a no-op sync writes nothing
    assert await conn.fetchval("SELECT shared.tenant_entitlement_sync($1)", tid) == 0
    # a billing-only change writes nothing either
    await conn.execute("UPDATE shared.tenant_subscription SET billing_ref = 'INV-1', paid_through = current_date "
                       "WHERE tenant_id = $1", tid)
    assert len(await _events(conn, tid)) == n + 1


@pytest.mark.asyncio
async def test_erasure_of_an_offboarded_tenant_deletes_and_emits_nothing(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by) VALUES ($1, 'content.rewrite', 1, 'test', 'admin:test')", tid)
    await conn.execute("UPDATE shared.tenants SET cancelled_at = now(), is_active = false WHERE tenant_id = $1", tid)
    n = len(await _events(conn, tid))
    # the AA-839 order: children by default predicate (services/erasure/inventory.py entries)
    for table in ("tenant_entitlement", "tenant_entitlement_override", "tenant_subscription"):
        await conn.execute(f"DELETE FROM shared.{table} WHERE tenant_id = $1", tid)
    assert len(await _events(conn, tid)) == n
    await conn.execute("DELETE FROM shared.tenant_account_event WHERE tenant_id = $1", tid)
    await conn.execute("DELETE FROM shared.tenant_membership_event WHERE tenant_id = $1", tid)
    await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_account_event WHERE tenant_id = $1", tid) == 0


@pytest.mark.asyncio
async def test_deleting_a_tenant_row_cascades_quietly(conn):
    tid = await _tenant(conn)
    await _subscribe(conn, tid)
    await conn.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)     # test clean-ups do this
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_subscription WHERE tenant_id = $1", tid) == 0
    assert await conn.fetchval("SELECT count(*) FROM shared.tenant_entitlement WHERE tenant_id = $1", tid) == 0


# ── shared.tenants ────────────────────────────────────────────────────────────────────────────────────────────────

async def _state(conn, tid):
    r = await conn.fetchrow("SELECT account_status, is_active, cancelled_at IS NOT NULL AS cancelled, suspend_reason "
                            "FROM shared.tenants WHERE tenant_id = $1", tid)
    return r["account_status"], r["is_active"], r["cancelled"], r["suspend_reason"]


@pytest.mark.asyncio
async def test_legacy_write_paths_and_status_writes(conn):
    tid = await _tenant(conn)
    assert await _state(conn, tid) == ("active", True, False, None)
    await conn.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("suspended", False, False, None)
    await conn.execute("UPDATE shared.tenants SET is_active = true WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("active", True, False, None)
    await conn.execute("UPDATE shared.tenants SET is_active = false, cancelled_at = now() WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("offboarding", False, True, None)
    await conn.execute("UPDATE shared.tenants SET is_active = true, cancelled_at = NULL WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("active", True, False, None)
    await conn.execute("UPDATE shared.tenants SET rate_limit_rpm = 300 WHERE tenant_id = $1", tid)   # no status
    await conn.execute("UPDATE shared.tenants SET account_status = 'suspended', suspend_reason = 'compliance' "
                       "WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("suspended", False, False, "compliance")
    await conn.execute("UPDATE shared.tenants SET account_status = 'past_due' WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("past_due", True, False, None)                 # reason cleared
    await conn.execute("UPDATE shared.tenants SET account_status = 'closed' WHERE tenant_id = $1", tid)
    assert await _state(conn, tid) == ("closed", False, True, None)
    statuses = [(c["previous_status"], c["status"]) for _, c in await _events(conn, tid, "account.status_changed")]
    assert statuses == [(None, "active"), ("active", "suspended"), ("suspended", "active"), ("active", "offboarding"),
                        ("offboarding", "active"), ("active", "suspended"), ("suspended", "past_due"),
                        ("past_due", "closed")]
    feed = [r["type"] for r in await conn.fetch("SELECT type FROM shared.tenant_membership_event WHERE tenant_id = $1 "
                                                "ORDER BY txid, seq", tid)]
    # the status-only writes (suspended, past_due, closed) reach the 276 feed through is_active / cancelled_at
    assert feed == ["tenant.created", "tenant.locked", "tenant.unlocked", "tenant.deleted", "tenant.unlocked",
                    "tenant.locked", "tenant.unlocked", "tenant.deleted"]


@pytest.mark.asyncio
async def test_inserts_and_vocabularies(conn):
    off = await _tenant(conn, active=False, offboarded=True)
    assert await _state(conn, off) == ("offboarding", False, True, None)
    locked = await _tenant(conn, active=False)
    assert await _state(conn, locked) == ("suspended", False, False, None)
    tid = await _tenant(conn)
    await _refused(conn, "22023", "UPDATE shared.tenants SET time_zone = 'Mars/Olympus' WHERE tenant_id = $1", tid)
    await conn.execute("UPDATE shared.tenants SET time_zone = 'Asia/Ho_Chi_Minh' WHERE tenant_id = $1", tid)
    await _refused(conn, "23514", "UPDATE shared.tenants SET data_region = 'apac' WHERE tenant_id = $1", tid)
    await _refused(conn, "23514", "UPDATE shared.tenants SET kind = 'partner' WHERE tenant_id = $1", tid)
    await _refused(conn, "23514", "UPDATE shared.tenants SET account_status = 'frozen' WHERE tenant_id = $1", tid)
    await _refused(conn, "23514", "UPDATE shared.tenants SET billing_country = 'vn' WHERE tenant_id = $1", tid)
    await _refused(conn, "23514", "UPDATE shared.tenants SET is_pilot = true WHERE tenant_id = $1", tid)
    await conn.execute("UPDATE shared.tenants SET legal_entity_name = 'Synthetic Lotus Travel Co', billing_country = "
                       "'VN', billing_email = 'billing@synthetic.example', segment = 'tech_touch' WHERE tenant_id = $1",
                       tid)
    other = await _tenant(conn)
    await conn.execute("UPDATE shared.tenants SET is_pilot = true, pilot_ends_at = now() + interval '8 weeks' "
                       "WHERE tenant_id = $1", tid)
    await _refused(conn, "23505", "UPDATE shared.tenants SET is_pilot = true, pilot_ends_at = now() + interval '1 week' "
                   "WHERE tenant_id = $1", other)
    demo = await _tenant(conn, canary=True)
    await conn.execute("UPDATE shared.tenants SET kind = 'demo' WHERE tenant_id = $1", demo)
    await _refused(conn, "23514", "UPDATE shared.tenants SET is_pilot = true, pilot_ends_at = now() "
                   "WHERE tenant_id = $1", demo)


@pytest.mark.asyncio
async def test_cis_app_runtime_writes(conn):
    """As the CIS runtime with the grants of 353 (other modules re-create cis_app, so they are re-applied here): a
    tenant create, a subscription and a legacy AI budget write all reach the feed."""
    await conn.execute(migration_body(353))
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE ON shared.tenants TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.tenant_ai_budget TO cis_app")
    await conn.execute("GRANT INSERT ON shared.tenant_membership_event TO cis_app")
    await conn.execute("GRANT SELECT ON shared.v_tenant_entitlement_v1 TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    tid = await _tenant(conn)
    await _subscribe(conn, tid, plan="content.growth")
    await conn.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, updated_by) "
                       "VALUES ($1, 3, 'admin:test')", tid)
    assert (await _ents(conn, tid))["ai.budget_usd_monthly"] == (3.0, "override")
    assert (await _materialised(conn, tid))["ai.budget_usd_monthly"] == (3.0, "override")
    types = {t for t, _ in await _events(conn, tid)}
    assert types == {"account.status_changed", "subscription.changed", "entitlement.changed"}


# ── the 355 bridge: shared.tenants keeps its content subscription ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_write_shapes_keep_the_content_subscription(conn):
    """Create, PATCH plan_tier, offboard and restore in the shapes of api/routers/admin.py: the contract view follows
    shared.tenants.plan_tier and the lifecycle with no code writing shared.tenant_subscription."""
    tid = await _tenant(conn)
    assert await _sub(conn, tid) == ("content.starter", "active", False)
    assert (await _ents(conn, tid))["content.rate_limit_rpm"] == (60.0, "plan")
    # PATCH plan_tier (admin.py: plan_tier, plan_id, rate_limit_rpm from PLAN_LIMITS)
    await conn.execute("UPDATE shared.tenants SET plan_tier = 'growth'::plan_tier_enum, rate_limit_rpm = 300, "
                       "updated_at = NOW() WHERE tenant_id = $1", tid)
    assert await _sub(conn, tid) == ("content.growth", "active", False)
    ents = await _ents(conn, tid)
    assert ents["content.rate_limit_rpm"] == (300.0, "plan") and ents["content.rewrite"] == (200.0, "plan")
    assert ents["ai.budget_usd_monthly"] == (40.0, "plan")
    assert await _materialised(conn, tid) == ents
    sub_evs = [c for t, c in await _events(conn, tid, "subscription.changed")]
    assert sub_evs[-1]["previous_plan_key"] == "content.starter" and sub_evs[-1]["plan_key"] == "content.growth"
    # an add-on, then offboard (admin.py: cancelled_at, cancellation_reason, is_active false)
    await _subscribe(conn, tid, product="crm", plan="crm.core")
    await conn.execute("UPDATE shared.tenants SET cancelled_at = NOW(), cancellation_reason = 'synthetic test', "
                       "is_active = FALSE, updated_at = NOW() "
                       "WHERE tenant_id = $1::uuid", tid)
    assert await _sub(conn, tid) == ("content.growth", "canceled", True)
    assert await _sub(conn, tid, "crm") == ("crm.core", "canceled", True)
    assert await _ents(conn, tid) == {} and await _materialised(conn, tid) == {}
    # restore (admin.py: cancelled_at NULL, is_active TRUE): content comes back, the add-on does not
    await conn.execute("UPDATE shared.tenants SET cancelled_at = NULL, cancellation_reason = NULL, is_active = TRUE, "
                       "updated_at = NOW() WHERE tenant_id = $1", tid)
    assert await _sub(conn, tid) == ("content.growth", "active", False)
    assert await _sub(conn, tid, "crm") == ("crm.core", "canceled", True)
    ents = await _ents(conn, tid)
    assert ents["content.rewrite"] == (200.0, "plan") and "crm.core" not in ents
    assert await _materialised(conn, tid) == ents
    # a lock / unlock leaves the subscription alone (the consumer reads account_status)
    await conn.execute("UPDATE shared.tenants SET is_active = false WHERE tenant_id = $1", tid)
    assert await _sub(conn, tid) == ("content.growth", "active", False)
    rows = await conn.fetch("SELECT DISTINCT account_status FROM shared.v_tenant_entitlement_v1 WHERE tenant_id = $1",
                            tid)
    assert [r["account_status"] for r in rows] == ["suspended"]


@pytest.mark.asyncio
async def test_bridge_inserts_offboarded_and_refuses_a_tier_without_a_live_plan(conn):
    off = await _tenant(conn, active=False, offboarded=True)
    assert await _sub(conn, off) == ("content.starter", "canceled", True)
    assert await _ents(conn, off) == {}
    internal = await _tenant(conn, tier="internal")
    assert await _sub(conn, internal) == ("content.internal", "active", False)
    await conn.execute("UPDATE shared.plan SET retired_at = now() WHERE plan_key = 'content.enterprise'")
    await _refused(conn, "23514", "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, "
                   "rate_limit_rpm, is_active) VALUES (gen_random_uuid(), 'AA-1028 synthetic', 'zz1028-noplan', "
                   "'enterprise', 3, 60, true)")
    await _refused(conn, "23514", "UPDATE shared.tenants SET plan_tier = 'enterprise' WHERE tenant_id = $1", internal)
    assert await _sub(conn, internal) == ("content.internal", "active", False)


@pytest.mark.asyncio
async def test_retiring_a_feature_key_and_removing_a_plan_feature(conn):
    tid = await _tenant(conn)
    await conn.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                       "granted_by) VALUES ($1, 'content.rewrite', 70, 'test', 'admin:test')", tid)
    n = len(await _events(conn, tid))
    await conn.execute("UPDATE shared.entitlement_feature SET retired_at = now() WHERE feature_key = 'channel.facebook'")
    assert "channel.facebook" not in await _materialised(conn, tid)
    assert (await _events(conn, tid))[n:] == [("entitlement.changed", {
        "op": "removed", "feature_key": "channel.facebook", "product": "content", "limit": None, "source": None,
        "expires_at": None})]
    # an overridden key is retired too (its override row stays, it resolves to nothing)
    await conn.execute("UPDATE shared.entitlement_feature SET retired_at = now() WHERE feature_key = 'content.rewrite'")
    assert "content.rewrite" not in await _materialised(conn, tid)
    await conn.execute("UPDATE shared.entitlement_feature SET retired_at = NULL "
                       "WHERE feature_key IN ('channel.facebook', 'content.rewrite')")
    mat = await _materialised(conn, tid)
    assert mat["channel.facebook"] == (None, "plan") and mat["content.rewrite"] == (70.0, "override")
    # a plan_feature DELETE (OLD only) re-syncs the plan's tenants
    await conn.execute("DELETE FROM shared.plan_feature WHERE plan_key = 'content.starter' "
                       "AND feature_key = 'content.article'")
    assert "content.article" not in await _materialised(conn, tid)
    assert (await _events(conn, tid))[-1] == ("entitlement.changed", {
        "op": "removed", "feature_key": "content.article", "product": "content", "limit": None, "source": None,
        "expires_at": None})


def _guard_2(version: int) -> str:
    """Guard 2 of 354 / 355 (the writers of shared.tenants), as its DO block."""
    body = migration_body(version)
    start = body.index("-- ── Guard 2")
    return body[start:body.index("-- ── Guard 3", start)]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [354, 355])
async def test_writer_guards_count_only_real_grantees(conn, version):
    """On RDS, rds_superuser holds pg_write_all_data (so has_table_privilege says it may write shared.tenants) but is
    no member of cis_owner: a role whose only path is a predefined role is not a writer of the table. A real grantee
    without the event / bridge grants is still refused."""
    await conn.execute("CREATE ROLE zz1028_write_all NOLOGIN IN ROLE pg_write_all_data")
    assert await conn.fetchval("SELECT has_table_privilege('zz1028_write_all', 'shared.tenants', 'UPDATE') AS ok")
    await conn.execute(_guard_2(version))                       # passes: no grant on shared.tenants itself
    await conn.execute("CREATE ROLE zz1028_grantee NOLOGIN")
    await conn.execute("GRANT UPDATE (plan_tier) ON shared.tenants TO zz1028_grantee")
    with pytest.raises(asyncpg.RaiseError) as info:
        async with conn.transaction():
            await conn.execute(_guard_2(version))
    assert "zz1028_grantee" in str(info.value) and "zz1028_write_all" not in str(info.value)
