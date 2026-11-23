-- Migration 475: AA-1160 (S289, the S285 live check of the agency app's Trip planner module) — CIS learns which tenants
-- have a CRM agency: the table shared.tenant_crm_agency, and catalog.v_tenant_tripplanner_v1 appends crm_agency and the
-- agency's own fallback route (contract tenant.entitlements.v1 1.11; columns are only appended).
--
-- Why. The lead route of a TripPlanner tenant resolves to crm as soon as the CRM add-on (crm.core) is live (CIS 390).
-- A CRM agency (AA-Partner crm.tenant) is created later, by the CIS CRM onboarding (AA-1116) or the CRM's own console,
-- and may never be: S285 found a synthetic tenant with crm.core and no CRM agency whose planner leads went to the CRM,
-- which skipped them (skipped_agency), so they reached neither the CRM nor the agency's e-mail / webhook route. CIS may
-- not read CRM tables (root ADR 0008a), so it records what the CRM itself proves: the CRM's entitlement feed reads the
-- tenant.entitlements.v1 snapshot (GET /tenant/v1/tenants/{id}/account) of every agency it holds, on each account
-- event and in an hourly pass over all of them, and the CIS CRM onboarding sees the agency exist or be created. The
-- CIS code PR of AA-1160 writes a row here on each of those; this file only adds the table and the view columns.
--
-- What:
--   Part A (cis_owner, the owner of shared since 245; the conftest replay runs this part only, _replay_parts.py):
--   1. shared.tenant_crm_agency — one row per tenant the CRM holds an agency for:
--        tenant_id      PK, FK shared.tenants ON DELETE CASCADE;
--        first_seen_at  when CIS first saw the agency;
--        last_seen_at   the latest sighting (the CIS code refreshes it at most every few minutes: a write throttle,
--                       not a policy value);
--        source         the latest sighting's source: crm_pull (the CRM read the snapshot with its M2M client) or
--                       crm_onboarding (the CIS onboarding found or created the agency).
--      No personal data (ids and times). No RLS, like the other control-plane tables of shared (tenant_tripplanner_site
--      379, tenant_tripplanner_domain 428): read and written only by the CIS API as cis_app, read by TripPlanner only
--      through the catalog view, never by a tenant role (rls catalogue rule a allowlist). Grants: REVOKE ALL first
--      (the Dev default ACL of cis_owner in shared, CIS 245), then cis_app SELECT, INSERT, UPDATE, DELETE (the
--      sightings and the AA-839 tenant erasure). Retention: root DM-7.1 row shared.tenant_crm_agency (kept while the
--      tenant exists; erased with it, FK cascade as the backstop).
--   Part B (the X1 S22 catalog recipe, as 379 / 390 / 429 / 438):
--   2. catalog_owner: column SELECT (tenant_id) on shared.tenant_crm_agency.
--   3. catalog.v_tenant_tripplanner_v1 appends (the 438 body unchanged before them, same rows):
--        crm_agency            boolean — the CRM holds an agency for this tenant (a row above);
--        fallback_channel      text    — the agency's own route, as if it had no CRM add-on: webhook / email when the
--                                         site stores that channel with its target, else none;
--        fallback_webhook_url  text    — the target of a webhook fallback, else NULL;
--        fallback_email        text    — the target of an e-mail fallback, else NULL.
--      lead_channel keeps its 390 meaning (crm while crm.core is live). TripPlanner (AA-1160 code) sends a lead to the
--      CRM only when crm_agency is true or the CRM pulled the tenant lately (its own beacon), else to the fallback, and
--      re-routes the tenant's pending CRM leads once to that fallback.
--   4. Register 475.
--
-- Behaviour on apply. An empty table: crm_agency is false for every tenant until the CIS code PR is deployed and the
-- CRM's next entitlement pull (2-minute slots, every held agency within the hour) has read the snapshots. Nothing reads
-- the new columns before the TripPlanner code PR of AA-1160, which keeps a tenant the CRM pulls on crm through its own
-- beacon, so no tenant with a CRM agency loses its CRM route in between. The fallback columns equal 390's
-- lead_webhook_url / lead_email for every tenant whose lead_channel is not crm.
--
-- Class: CONTRACT (root ADR 0017 decision 3; no migrate directive line): a new table with a tenant_id and RLS off on
-- purpose (rule a allowlist), REVOKE, a column GRANT on it and a replaced view in the hot schema catalog. MIG-4: a
-- migration-only PR, applied on Dev by Infra Migration Apply when every job table is idle (ADR 0017a), then the CIS
-- code PR and the TripPlanner code PR.
--
-- Guards (RAISE rolls the whole file back): before — 438 registered, schema catalog owned by cis_owner. After Part A —
-- RLS off and the grants as designed, the source CHECK refuses an unknown source. After Part B, as catalog_owner — the
-- view answers. As cis_owner — the view's exact columns and types, owner catalog_owner, security_barrier, tripplanner
-- can read it and booking / aa_app_user / cis_app / crm_app cannot, tripplanner holds no privilege on the new table,
-- catalog_owner reads only its tenant_id and writes nothing, and catalog_owner has no CREATE on schema catalog any more.
--
-- Recipe. Part A as cis_owner. Part B: the grant as cis_owner, then GRANT CREATE ON SCHEMA catalog TO catalog_owner;
-- SET LOCAL ROLE catalog_owner; the view DDL; SET LOCAL ROLE cis_owner; REVOKE CREATE. DB Migrate runs the file as
-- cis_owner (login db_migrator). Never RESET ROLE, no SECURITY DEFINER, no role DDL, no dynamic SQL. The conftest replay
-- runs Part A only (tests/integration/_replay_parts.py); tests/integration/test_aa1160_migration_475.py applies 357,
-- 377, 379, 390, 428's grants, 429, 438 and this file the DB Migrate way and undoes them.
--
-- Number. Owner main max (473) + 2: 474 is reserved by another S289 lane. If the owner main max + 1 differs at merge,
-- renumber (git grep '475_' and '475' here, in tests/integration/test_aa1160_migration_475.py,
-- tests/integration/_replay_parts.py, tests/integration/conftest.py and docs/implementation-notes/AA-1160.md).
--
-- Locks. One CREATE TABLE (new, empty), a column GRANT and one CREATE OR REPLACE VIEW. lock_timeout 5s,
-- statement_timeout 1min. Idempotent (IF NOT EXISTS, CREATE OR REPLACE with the same column list, re-runnable grants),
-- one transaction, self-registering.
--
-- Rollback (a NEW forward migration under the same recipe, after the CIS and TripPlanner code is reverted): restate
-- the 438 view (a view loses appended columns only by DROP + CREATE), REVOKE the column grant, DROP the table.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '1min';
SELECT set_config('aa.actor', 'migration:475', true);

-- ── Preconditions (as the file's role: cis_owner on Dev) ─────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = '438') THEN
        RAISE EXCEPTION 'migration 475: apply 438 (the 438 body of catalog.v_tenant_tripplanner_v1) first';
    END IF;
    IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'catalog') <> 'cis_owner' THEN
        RAISE EXCEPTION 'migration 475: schema catalog is not owned by cis_owner (apply 245 first)';
    END IF;
END $$;

-- ── Part A: the CRM agency marker (cis_owner) ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shared.tenant_crm_agency (
    tenant_id      uuid        NOT NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    source         text        NOT NULL,
    CONSTRAINT tenant_crm_agency_pkey PRIMARY KEY (tenant_id),
    CONSTRAINT tenant_crm_agency_tenant_id_fkey FOREIGN KEY (tenant_id)
        REFERENCES shared.tenants (tenant_id) ON DELETE CASCADE,
    CONSTRAINT tenant_crm_agency_source_chk CHECK (source IN ('crm_pull', 'crm_onboarding')),
    CONSTRAINT tenant_crm_agency_seen_chk CHECK (last_seen_at >= first_seen_at)
);

COMMENT ON TABLE shared.tenant_crm_agency IS
    'AA-1160 (CIS 475): the tenants the CRM (AA-Partner) holds an agency for, as CIS sees it: the CRM read the '
    'tenant.entitlements.v1 snapshot with its M2M client (the CRM reads it only for an agency it holds), or the CIS '
    'CRM onboarding found or created the agency. catalog.v_tenant_tripplanner_v1.crm_agency reads it: a TripPlanner '
    'lead goes to the CRM only for such a tenant (or one the CRM pulls lately), else to the agency''s fallback route. '
    'No personal data.';
COMMENT ON COLUMN shared.tenant_crm_agency.source IS
    'AA-1160 (475): the latest sighting: crm_pull (CRM snapshot read) or crm_onboarding (CIS onboarding).';

REVOKE ALL ON shared.tenant_crm_agency FROM PUBLIC, cis_app, aa_app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON shared.tenant_crm_agency TO cis_app;

DO $$
DECLARE
    t constant text := 'shared.tenant_crm_agency';
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE oid = t::regclass AND relrowsecurity) THEN
        RAISE EXCEPTION 'migration 475: % has row-level security on (the 244 guard keeps the shared RLS set fixed)', t;
    END IF;
    IF NOT pg_catalog.has_table_privilege('cis_app', t, 'SELECT, INSERT, UPDATE, DELETE')
       OR pg_catalog.has_table_privilege('aa_app_user', t, 'SELECT')
       OR pg_catalog.has_table_privilege('public', t, 'SELECT') THEN
        RAISE EXCEPTION 'migration 475: grants on % are not as designed', t;
    END IF;
    BEGIN
        INSERT INTO shared.tenant_crm_agency (tenant_id, source)
        VALUES ('00000000-0000-0000-0000-000000000000'::uuid, 'crm_guess');
        RAISE EXCEPTION 'migration 475: the source CHECK let an unknown source through';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;
END $$;

-- ── Part B: catalog.v_tenant_tripplanner_v1 (X1 S22 catalog recipe) ──────────────────────────────────────────────
GRANT SELECT (tenant_id) ON shared.tenant_crm_agency TO catalog_owner;

GRANT CREATE ON SCHEMA catalog TO catalog_owner;
SET LOCAL ROLE catalog_owner;

CREATE OR REPLACE VIEW catalog.v_tenant_tripplanner_v1 WITH (security_barrier = true) AS
SELECT t.tenant_id,
       t.slug::text AS slug,
       t.name::text AS name,
       t.kind,
       t.account_status,
       t.account_status IN ('trial', 'active', 'past_due') AS in_service,
       t.account_status IN ('trial', 'active', 'past_due')
           AND EXISTS (SELECT 1 FROM shared.v_tenant_entitlement_resolved r
                        WHERE r.tenant_id = t.tenant_id AND r.feature_key = 'tp.hosted') AS served,
       EXISTS (SELECT 1 FROM shared.v_tenant_entitlement_resolved r
                WHERE r.tenant_id = t.tenant_id AND r.feature_key = 'tp.launcher') AS tp_launcher,
       EXISTS (SELECT 1 FROM shared.v_tenant_entitlement_resolved r
                WHERE r.tenant_id = t.tenant_id AND r.feature_key = 'tp.branding') AS tp_branding,
       COALESCE(site.theme, '{}'::jsonb) AS theme,
       COALESCE(site.locale, 'en') AS locale,
       crm.live AS crm_core,
       route.channel AS lead_channel,
       CASE WHEN route.channel = 'webhook' THEN site.lead_webhook_url END AS lead_webhook_url,
       CASE WHEN route.channel = 'email' THEN site.lead_email END AS lead_email,
       ARRAY(SELECT d.domain FROM shared.tenant_tripplanner_domain d
              WHERE d.tenant_id = t.tenant_id AND d.status = 'active'
              ORDER BY d.domain) AS custom_domains,
       (SELECT r.limit_value FROM shared.v_tenant_entitlement_resolved r
         WHERE r.tenant_id = t.tenant_id AND r.feature_key = 'tp.ai_sessions')::integer AS ai_sessions_monthly,
       -- 475: the CRM holds an agency for this tenant, and the agency's own route as if it had no CRM add-on
       EXISTS (SELECT 1 FROM shared.tenant_crm_agency a WHERE a.tenant_id = t.tenant_id) AS crm_agency,
       fallback.channel AS fallback_channel,
       CASE WHEN fallback.channel = 'webhook' THEN site.lead_webhook_url END AS fallback_webhook_url,
       CASE WHEN fallback.channel = 'email' THEN site.lead_email END AS fallback_email
  FROM shared.tenants t
  LEFT JOIN shared.tenant_tripplanner_site site ON site.tenant_id = t.tenant_id
 CROSS JOIN LATERAL (
        SELECT EXISTS (SELECT 1 FROM shared.v_tenant_entitlement_resolved r
                        WHERE r.tenant_id = t.tenant_id AND r.feature_key = 'crm.core') AS live
       ) crm
 CROSS JOIN LATERAL (
        SELECT CASE WHEN site.lead_channel = 'webhook' AND site.lead_webhook_url IS NOT NULL THEN 'webhook'
                    WHEN site.lead_channel = 'email' AND site.lead_email IS NOT NULL THEN 'email'
                    ELSE 'none' END AS channel
       ) fallback
 CROSS JOIN LATERAL (
        SELECT CASE WHEN crm.live THEN 'crm' ELSE fallback.channel END AS channel
       ) route
 WHERE t.kind <> 'internal'
   AND EXISTS (SELECT 1 FROM shared.tenant_subscription s
                WHERE s.tenant_id = t.tenant_id AND s.product = 'tripplanner');

COMMENT ON VIEW catalog.v_tenant_tripplanner_v1 IS
    'AA-1038 R5 slices 2-3, AA-1090 R8, AA-1116, AA-1160 (379, 390, 429, 438, 475; contract tenant.entitlements.v1 '
    '1.11, root ADR 0019 d10, d11, b4): one row per tenant with a TripPlanner subscription (kind internal excluded) for '
    'the /{slug} site: the slug, the trading name (brand label), kind, the lifecycle status, in_service (trial, active, '
    'past_due), served (in service and tp.hosted live: answer /{slug} only then), tp_launcher, tp_branding (BO-8 badge), '
    'theme (kit token name -> #rrggbb) and locale; 390: crm_core and the resolved lead route with its target (crm while '
    'crm.core is live); 429: custom_domains, the active custom hosts of the site (served only when served is true); '
    '438: ai_sessions_monthly (resolved tp.ai_sessions, NULL = no limit); 475: crm_agency (the CRM holds an agency for '
    'the tenant, shared.tenant_crm_agency) and the fallback route (fallback_channel webhook / email / none with its '
    'target: the route without the CRM add-on). A lead goes to the CRM only with crm_agency (or a fresh CRM pull), else '
    'to the fallback. A bound tenant reads AA master catalogue rows only. TripPlanner only. Contract v1: columns are '
    'only appended.';

GRANT SELECT ON catalog.v_tenant_tripplanner_v1 TO tripplanner;

-- ── Closing check as catalog_owner: the view answers ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE 'migration 475: as catalog_owner, % TripPlanner tenant(s), % with a CRM agency',
        (SELECT count(*) FROM catalog.v_tenant_tripplanner_v1),
        (SELECT count(*) FROM catalog.v_tenant_tripplanner_v1 WHERE crm_agency);
END $$;

SET LOCAL ROLE cis_owner;
REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;

-- ── Closing check as cis_owner ───────────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    v constant text := 'catalog.v_tenant_tripplanner_v1';
    t constant text := 'shared.tenant_crm_agency';
    r text;
    cols text;
BEGIN
    IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = v::regclass) <> 'catalog_owner' THEN
        RAISE EXCEPTION 'migration 475: % is not catalog_owner''s', v;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = v::regclass AND 'security_barrier=true' = ANY(reloptions)) THEN
        RAISE EXCEPTION 'migration 475: % is not security_barrier', v;
    END IF;
    IF NOT pg_catalog.has_table_privilege('tripplanner', v, 'SELECT') THEN
        RAISE EXCEPTION 'migration 475: tripplanner cannot read %', v;
    END IF;
    FOREACH r IN ARRAY ARRAY['booking', 'aa_app_user', 'cis_app', 'crm_app'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r)
           AND pg_catalog.has_table_privilege(r, v, 'SELECT') THEN
            RAISE EXCEPTION 'migration 475: % can read % (tripplanner only)', r, v;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'tripplanner')
       AND pg_catalog.has_table_privilege('tripplanner', t, 'SELECT') THEN
        RAISE EXCEPTION 'migration 475: tripplanner can read % (only through the view)', t;
    END IF;
    IF pg_catalog.has_column_privilege('catalog_owner', t, 'source', 'SELECT')
       OR pg_catalog.has_column_privilege('catalog_owner', t, 'last_seen_at', 'SELECT')
       OR pg_catalog.has_table_privilege('catalog_owner', t, 'INSERT, UPDATE, DELETE') THEN
        RAISE EXCEPTION 'migration 475: catalog_owner reads or writes more of % than its tenant_id', t;
    END IF;
    SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) INTO cols
      FROM pg_attribute a WHERE a.attrelid = v::regclass AND a.attnum > 0 AND NOT a.attisdropped;
    IF cols IS DISTINCT FROM 'tenant_id uuid, slug text, name text, kind text, account_status text, '
                             'in_service boolean, served boolean, tp_launcher boolean, tp_branding boolean, '
                             'theme jsonb, locale text, crm_core boolean, lead_channel text, lead_webhook_url text, '
                             'lead_email text, custom_domains text[], ai_sessions_monthly integer, '
                             'crm_agency boolean, fallback_channel text, fallback_webhook_url text, '
                             'fallback_email text' THEN
        RAISE EXCEPTION 'migration 475: % columns are %', v, cols;
    END IF;
    IF pg_catalog.has_schema_privilege('catalog_owner', 'catalog', 'CREATE') THEN
        RAISE EXCEPTION 'migration 475: catalog_owner still has CREATE on schema catalog';
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('475', now(), 'AA-1160: shared.tenant_crm_agency (CRM agency marker) and catalog.v_tenant_tripplanner_v1 '
                      'crm_agency + fallback route columns')
ON CONFLICT (version) DO NOTHING;

COMMIT;
