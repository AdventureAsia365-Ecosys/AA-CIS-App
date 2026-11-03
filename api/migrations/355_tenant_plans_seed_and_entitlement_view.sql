-- Migration 355: AA-1028 R1 slice 1 (root ADR 0019 d2, b2-b4, b19, BO-7) — the starting policy data (feature keys,
-- plans, plan features), the content subscription of every existing tenant, the re-sync trigger on
-- shared.tenant_ai_budget, and the contract view shared.v_tenant_entitlement_v1.
--
-- Why. ADR 0019 d2: apps check feature keys, never plan names, and the plan values are policy data that change
-- without code (b2: content tiers 299 / 799 / 1,499 USD a month, Enterprise by contract; b3: CRM per seat; b4:
-- TripPlanner per site, hosted + launcher, then custom domain + API; b19: AI included 15 / 40 / 80 USD a month,
-- Enterprise by contract; BO-7: policy.notice_days = 7). The values below are STARTING values: the Plans page (slice
-- 3) edits them; nothing in code reads a plan name.
--
-- What.
--   1. shared.entitlement_feature rows (26 keys): content.rewrite (count_month), api.calls_monthly (count_month),
--      content.rate_limit_rpm (rpm), content.social, content.article, channel.wordpress, channel.facebook,
--      api.access, trade.book (flags); crm.core, crm.ai_assist, crm.whatsapp_sms, crm.stripe_connect,
--      crm.campaigns, crm.custom_domain, crm.insights_export (flags), crm.seats (seats); tp.hosted, tp.launcher,
--      tp.embed, tp.custom_domain, tp.api, tp.branding (flags), tp.ai_sessions (count_month); ai.budget_usd_monthly
--      (usd_month, platform), policy.notice_days (days, platform).
--   2. shared.plan rows: content.starter / growth / business (299 / 799 / 1,499 USD, public, membership_plan_id =
--      the CIS 223 rows), content.enterprise (no price: contract, not public), content.internal (AA itself, no price,
--      not public); crm.core, crm.pro (per seat, no price yet: b3 says "below the main competitor"; the Plans page
--      sets it); tripplanner.hosted, tripplanner.pro (per site, no price yet).
--   3. shared.plan_feature rows: every content plan gives the content flags, trade.book and policy.notice_days 7;
--      the numbers carry today's values (content.rewrite = membership_plans.tours_quota_monthly 50 / 200 / 500,
--      api.calls_monthly = api_calls_quota_monthly 5,000 / 20,000 / 100,000, content.rate_limit_rpm = PLAN_LIMITS
--      60 / 300 / 1,000) and ai.budget_usd_monthly 15 / 40 / 80; enterprise and internal: no limit on the counts
--      (internal keeps rpm 60, PLAN_LIMITS), enterprise AI 80 (the Business floor until the contract override),
--      internal AI unlimited. crm.core: crm.core, crm.ai_assist, crm.seats 10 (today's crm member_limit default);
--      crm.pro: every crm key, seats 10. tripplanner.hosted: tp.hosted, tp.launcher, tp.ai_sessions (no limit:
--      ai.budget_usd_monthly and the AA-1015 route limiter cap it); tripplanner.pro: + tp.embed, tp.custom_domain,
--      tp.api. tp.branding is in no plan (BO-8: off by default, an override turns it on).
--      All inserts are ON CONFLICT DO NOTHING: a re-run never overwrites a value staff changed.
--   4. Backfill: one content subscription per existing tenant, plan 'content.' || plan_tier, status canceled for an
--      offboarding / closed tenant (canceled_at = cancelled_at), else active; started_at = created_at. The
--      subscription trigger (353) writes subscription.changed and materialises the tenant's entitlements
--      (entitlement.changed per key). actor 'migration:355'. No crm / tripplanner subscription is backfilled: CIS
--      does not read crm.* (the CRM projection pull of slice 2 starts from the CRM's own agencies).
--   5. Trigger tenant_ai_budget_resync on shared.tenant_ai_budget (AFTER INSERT / UPDATE / DELETE, the 353
--      function): a budget change re-syncs ai.budget_usd_monthly of that tenant. The table stays the gateway's
--      source (shared/llm_client/tenant_budget.py) until slice 3.
--   6. View shared.v_tenant_entitlement_v1 (contract tenant.entitlements.v1, part 1 of 3): one row per tenant and
--      resolved feature key, with the tenant's account_status, kind, data_region and is_pilot; computed live from
--      the resolver (shared.v_tenant_entitlement_resolved), so an expired override or trial is served correctly
--      before the next sync. SELECT to cis_app only in this slice; the consumer grants (tripplanner, R5) come with
--      their consumers (schema-ownership rule 5).
--   7. Register 355.
--
-- Behaviour. After apply, every tenant has a content subscription and its entitlements (about 15 rows per tenant);
-- the account event feed holds the creation events. Nothing enforces an entitlement yet (slices 2-3); the gateway
-- budget check still reads shared.tenant_ai_budget only, so no call changes. The trigger on tenant_ai_budget runs as
-- the budget writer (cis_app, the AI providers › Budgets PUT), which holds the 353 grants.
--
-- Class: contract (INSERT into tables created by an earlier file, a trigger on a pre-existing table). MIG-4; on Dev
-- applied by Infra Migration Apply when every job table is idle (ADR 0017a).
--
-- Rollback (a NEW forward migration, after the code is reverted): DROP VIEW shared.v_tenant_entitlement_v1; DROP
-- TRIGGER tenant_ai_budget_resync ON shared.tenant_ai_budget; the seed and backfill rows are deleted with the tables
-- (353's rollback) or by a DRY RUN destructive file.
--
-- X1. One BEGIN … COMMIT block, no role switch. DB Migrate runs cis/ files as cis_owner.
--
-- The SQL that is applied to Dev is the copy in AA-CIS-Infra db/migrations/cis/; keep the two byte-identical.

BEGIN;
SET LOCAL lock_timeout = '5s';
SELECT set_config('aa.actor', 'migration:355', true);

DO $$
BEGIN
    IF to_regclass('shared.tenant_subscription') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = 'shared.tenants'::regclass
                         AND a.attname = 'account_status' AND NOT a.attisdropped) THEN
        RAISE EXCEPTION 'migration 355: needs 353 and 354';
    END IF;
END $$;

-- ── 1. Feature keys ─────────────────────────────────────────────────────────────────────────────────────────────────
INSERT INTO shared.entitlement_feature (feature_key, product, value_kind, unit, description) VALUES
    ('content.rewrite',        'content',     'limit',  'count_month', 'Tour rewrites (T2) per calendar month'),
    ('api.calls_monthly',      'content',     'limit',  'count_month', 'Content API calls per calendar month'),
    ('content.rate_limit_rpm', 'content',     'limit',  'rpm',         'Content API requests per minute'),
    ('content.social',         'content',     'flag',   NULL,          'Social posts (T5-T11)'),
    ('content.article',        'content',     'flag',   NULL,          'Articles'),
    ('channel.wordpress',      'content',     'flag',   NULL,          'Publish to WordPress'),
    ('channel.facebook',       'content',     'flag',   NULL,          'Publish to Facebook'),
    ('api.access',             'content',     'flag',   NULL,          'Content API keys'),
    ('trade.book',             'content',     'flag',   NULL,          'Book AA tours at the B2B price (Book with AA)'),
    ('crm.core',               'crm',         'flag',   NULL,          'The partner CRM'),
    ('crm.seats',              'crm',         'seats',  'seats',       'CRM members (seats)'),
    ('crm.ai_assist',          'crm',         'flag',   NULL,          'CRM AI drafts'),
    ('crm.whatsapp_sms',       'crm',         'flag',   NULL,          'CRM WhatsApp and SMS channels'),
    ('crm.stripe_connect',     'crm',         'flag',   NULL,          'CRM card payments on the agency''s Stripe'),
    ('crm.campaigns',          'crm',         'flag',   NULL,          'CRM campaigns'),
    ('crm.custom_domain',      'crm',         'flag',   NULL,          'CRM client portal on the agency''s domain'),
    ('crm.insights_export',    'crm',         'flag',   NULL,          'CRM insights export'),
    ('tp.hosted',              'tripplanner', 'flag',   NULL,          'Hosted TripPlanner site'),
    ('tp.launcher',            'tripplanner', 'flag',   NULL,          'TripPlanner launcher snippet'),
    ('tp.embed',               'tripplanner', 'flag',   NULL,          'TripPlanner embed'),
    ('tp.custom_domain',       'tripplanner', 'flag',   NULL,          'TripPlanner on the agency''s domain'),
    ('tp.api',                 'tripplanner', 'flag',   NULL,          'TripPlanner API'),
    ('tp.branding',            'tripplanner', 'flag',   NULL,          'Powered by AA badge on TripPlanner pages'),
    ('tp.ai_sessions',         'tripplanner', 'limit',  'count_month', 'TripPlanner AI sessions per calendar month'),
    ('ai.budget_usd_monthly',  'platform',    'limit',  'usd_month',   'AI spend included per calendar month (USD)'),
    ('policy.notice_days',     'platform',    'policy', 'days',        'Days of notice before a resale suspension')
ON CONFLICT (feature_key) DO NOTHING;

-- ── 2. Plans ────────────────────────────────────────────────────────────────────────────────────────────────────────
INSERT INTO shared.plan (plan_key, product, name, tier, price_usd_monthly, price_unit, seats_included, is_public,
                         membership_plan_id, notes, updated_by)
SELECT v.plan_key, v.product, v.name, v.tier::public.plan_tier_enum, v.price, v.unit, v.seats, v.is_public,
       NULL::uuid, v.notes, 'migration:355'
  FROM (VALUES
    ('content.starter',    'content',     'Starter',    'starter',    299.00::numeric, 'tenant', NULL::int, true,
     'ADR 0019 b2 starting price'),
    ('content.growth',     'content',     'Growth',     'growth',     799.00,  'tenant', NULL, true, 'ADR 0019 b2 starting price'),
    ('content.business',   'content',     'Business',   'business',   1499.00, 'tenant', NULL, true, 'ADR 0019 b2 starting price'),
    ('content.enterprise', 'content',     'Enterprise', 'enterprise', NULL,    'tenant', NULL, false,
     'Contact sales: contract price, no lower than Business (ADR 0019 b2)'),
    ('content.internal',   'content',     'AA internal', 'internal',  NULL,    'tenant', NULL, false, 'Adventure Asia itself'),
    ('crm.core',           'crm',         'CRM Core',   NULL,         NULL,    'seat',   10,   false,
     'Per seat; price to set below the main competitor (ADR 0019 b3)'),
    ('crm.pro',            'crm',         'CRM Pro',    NULL,         NULL,    'seat',   10,   false,
     'Per seat; price to set (ADR 0019 b3)'),
    ('tripplanner.hosted', 'tripplanner', 'TripPlanner Hosted', NULL, NULL,    'site',   NULL, false,
     'Per site: hosted page and launcher (ADR 0019 b4); price to set'),
    ('tripplanner.pro',    'tripplanner', 'TripPlanner Pro',    NULL, NULL,    'site',   NULL, false,
     'Per site: custom domain and API (ADR 0019 b4); price to set')
  ) AS v (plan_key, product, name, tier, price, unit, seats, is_public, notes)
ON CONFLICT (plan_key) DO NOTHING;

-- The soft link to the CIS 223 rows, where that table exists (it was made by hand on Dev; the CI replay lacks it).
DO $$
BEGIN
    IF to_regclass('shared.membership_plans') IS NOT NULL THEN
        UPDATE shared.plan p SET membership_plan_id = m.id
          FROM shared.membership_plans m
         WHERE p.product = 'content' AND m.plan_name = p.tier::text AND p.membership_plan_id IS NULL;
    END IF;
END $$;

-- ── 3. Plan features ────────────────────────────────────────────────────────────────────────────────────────────────
INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by)
SELECT v.plan_key, v.feature_key, v.limit_value, 'migration:355'
  FROM (
    -- the content flags, trade.book and the notice policy on every content plan
    SELECT p.plan_key, f.feature_key, CASE WHEN f.feature_key = 'policy.notice_days' THEN 7 END::numeric AS limit_value
      FROM (VALUES ('content.starter'), ('content.growth'), ('content.business'), ('content.enterprise'),
                   ('content.internal')) AS p (plan_key)
     CROSS JOIN (VALUES ('content.social'), ('content.article'), ('channel.wordpress'), ('channel.facebook'),
                        ('api.access'), ('trade.book'), ('policy.notice_days')) AS f (feature_key)
    UNION ALL
    SELECT * FROM (VALUES
        ('content.starter',    'content.rewrite',        50::numeric),
        ('content.starter',    'api.calls_monthly',      5000),
        ('content.starter',    'content.rate_limit_rpm', 60),
        ('content.starter',    'ai.budget_usd_monthly',  15),
        ('content.growth',     'content.rewrite',        200),
        ('content.growth',     'api.calls_monthly',      20000),
        ('content.growth',     'content.rate_limit_rpm', 300),
        ('content.growth',     'ai.budget_usd_monthly',  40),
        ('content.business',   'content.rewrite',        500),
        ('content.business',   'api.calls_monthly',      100000),
        ('content.business',   'content.rate_limit_rpm', 1000),
        ('content.business',   'ai.budget_usd_monthly',  80),
        ('content.enterprise', 'content.rewrite',        NULL),
        ('content.enterprise', 'api.calls_monthly',      NULL),
        ('content.enterprise', 'content.rate_limit_rpm', 1000),
        ('content.enterprise', 'ai.budget_usd_monthly',  80),
        ('content.internal',   'content.rewrite',        NULL),
        ('content.internal',   'api.calls_monthly',      NULL),
        ('content.internal',   'content.rate_limit_rpm', 60),
        ('content.internal',   'ai.budget_usd_monthly',  NULL),
        ('crm.core',           'crm.core',               NULL),
        ('crm.core',           'crm.ai_assist',          NULL),
        ('crm.core',           'crm.seats',              10),
        ('crm.pro',            'crm.core',               NULL),
        ('crm.pro',            'crm.ai_assist',          NULL),
        ('crm.pro',            'crm.seats',              10),
        ('crm.pro',            'crm.whatsapp_sms',       NULL),
        ('crm.pro',            'crm.stripe_connect',     NULL),
        ('crm.pro',            'crm.campaigns',          NULL),
        ('crm.pro',            'crm.custom_domain',      NULL),
        ('crm.pro',            'crm.insights_export',    NULL),
        ('tripplanner.hosted', 'tp.hosted',              NULL),
        ('tripplanner.hosted', 'tp.launcher',            NULL),
        ('tripplanner.hosted', 'tp.ai_sessions',         NULL),
        ('tripplanner.pro',    'tp.hosted',              NULL),
        ('tripplanner.pro',    'tp.launcher',            NULL),
        ('tripplanner.pro',    'tp.ai_sessions',         NULL),
        ('tripplanner.pro',    'tp.embed',               NULL),
        ('tripplanner.pro',    'tp.custom_domain',       NULL),
        ('tripplanner.pro',    'tp.api',                 NULL)
    ) AS n (plan_key, feature_key, limit_value)
  ) AS v
ON CONFLICT (plan_key, feature_key) DO NOTHING;

-- ── 4. The content subscription of every existing tenant ────────────────────────────────────────────────────────────
INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, started_at, canceled_at, updated_by)
SELECT t.tenant_id, 'content', 'content.' || t.plan_tier::text,
       CASE WHEN t.account_status IN ('offboarding', 'closed') THEN 'canceled' ELSE 'active' END,
       t.created_at,
       CASE WHEN t.account_status IN ('offboarding', 'closed') THEN coalesce(t.cancelled_at, now()) END,
       'migration:355'
  FROM shared.tenants t
 ORDER BY t.created_at, t.tenant_id
ON CONFLICT (tenant_id, product) DO NOTHING;

-- ── 5. The legacy AI budget re-syncs its override ───────────────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS tenant_ai_budget_resync ON shared.tenant_ai_budget;
CREATE TRIGGER tenant_ai_budget_resync
    AFTER INSERT OR UPDATE OR DELETE ON shared.tenant_ai_budget
    FOR EACH ROW EXECUTE FUNCTION shared.tenant_entitlement_resync_row();

-- ── 6. The contract view ────────────────────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW shared.v_tenant_entitlement_v1 AS
SELECT r.tenant_id, t.account_status, t.kind, t.data_region, t.is_pilot,
       r.feature_key, r.product, r.value_kind, r.unit, r.limit_value, r.source, r.expires_at
  FROM shared.v_tenant_entitlement_resolved r
  JOIN shared.tenants t ON t.tenant_id = r.tenant_id;
COMMENT ON VIEW shared.v_tenant_entitlement_v1 IS
    'AA-1028 R1 (CIS 355): contract tenant.entitlements.v1 (root docs/contracts/tenant.entitlements.v1.openapi.yaml), '
    'the view part: one row per tenant and entitled feature key (limit NULL = no limit), with the account status a '
    'consumer must also check (serve only trial, active, past_due). Apps check feature keys, never plan names.';
REVOKE ALL ON shared.v_tenant_entitlement_v1 FROM PUBLIC, cis_app, aa_app_user, catalog_owner;
GRANT SELECT ON shared.v_tenant_entitlement_v1 TO cis_app;

-- ── Guard: every tenant has its content subscription and every live one its entitlements ────────────────────────────
DO $$
DECLARE
    n integer;
BEGIN
    SELECT count(*) INTO n FROM shared.tenants t
     WHERE NOT EXISTS (SELECT 1 FROM shared.tenant_subscription s
                        WHERE s.tenant_id = t.tenant_id AND s.product = 'content');
    IF n > 0 THEN
        RAISE EXCEPTION 'migration 355: % tenant(s) without a content subscription', n;
    END IF;
    SELECT count(*) INTO n FROM shared.tenant_subscription s
     WHERE s.status = 'active'
       AND NOT EXISTS (SELECT 1 FROM shared.tenant_entitlement e WHERE e.tenant_id = s.tenant_id);
    IF n > 0 THEN
        RAISE EXCEPTION 'migration 355: % live subscription(s) without materialised entitlements', n;
    END IF;
    SELECT count(*) INTO n
      FROM shared.v_tenant_entitlement_resolved r
      FULL JOIN shared.tenant_entitlement e ON e.tenant_id = r.tenant_id AND e.feature_key = r.feature_key
     WHERE r.feature_key IS NULL OR e.feature_key IS NULL OR r.limit_value IS DISTINCT FROM e.limit_value;
    IF n > 0 THEN
        RAISE EXCEPTION 'migration 355: % materialised row(s) differ from the resolver', n;
    END IF;
    SELECT count(*) INTO n FROM shared.tenant_entitlement;
    RAISE NOTICE 'migration 355: materialised entitlements: %', n;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('355', now(), 'AA-1028 R1: plans and feature keys seed, content subscriptions, v_tenant_entitlement_v1')
ON CONFLICT (version) DO NOTHING;

COMMIT;
