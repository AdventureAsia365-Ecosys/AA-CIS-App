-- Migration 413: AA-1095 (S278) — Bedrock connection `acc1` (the payer account of acc3's organization) and its 13
-- chat-model deployments, seeded SWITCHED OFF; the route order is set afterwards by one route-control version.
--
-- Why. AWS refuses 7 chat models on acc3 (the LLM account) at account level since 29/10 (AccessDeniedException "not
-- available for this account", TPM quota 0 on five; AWS cases open), so every Claude 5.x / GPT stage falls to the
-- vendor APIs. On 12/11/2026 every chat model the gateway uses answered a real Converse call on acc1 through its
-- global. inference profile (tech lead's SSO session, us-west-1). Tech lead decision S278: serve Bedrock from acc1
-- while acc3 is blocked; acc3 stays configured behind it. Root ADR 0001a, Note S278 (acc1 holds the Bedrock invoker
-- role only); AA-CIS-Infra accounts/acc1-bedrock (role AA1-Bedrock-Invoker, ExternalId below).
--
-- What.
--   1. llm_provider_connection_account_id_env_check: + 'BEDROCK_ACCT_PAYER' (the env var AA-CIS-Infra sets on the
--      api and worker task definitions with acc1's ID; this public repo writes no account ID down). Values added only.
--   2. Connection acc1: bedrock / assume_role, role AA1-Bedrock-Invoker, ExternalId aa1095-satellite-bedrock-acc1,
--      us-west-1, account from env BEDROCK_ACCT_PAYER, aws_only, served_roles writer / judge / validate (no embedding
--      deployment), balance_source none (acc1 has no Cost Explorer reader; spend comes from the ledger), failure
--      domain bedrock:BEDROCK_ACCT_PAYER:aws_only. Timeout, retries, concurrency, rpm and the monthly cap are copied
--      from acc3 (cap 100 USD when acc3 has none: migration 366's value). ENABLED = false: the code that resolves
--      BEDROCK_ACCT_PAYER (AA-CIS-App PR "acc1 Bedrock connection", REQUIRED_CIS_SCHEMA not raised) and the Infra env
--      must be live first; the admin Test connection works on a switched-off connection, so the 13 models are probed
--      before the switch. ON CONFLICT DO NOTHING: an admin's later edit is kept.
--   3. Its shared.llm_connection_cost_policy row, copied from acc3's (alert 80 % / hard stop when acc3 has none).
--   4. 13 deployments on acc1 (origin seed, enabled, priority 100 = behind every existing deployment of the model,
--      the 306 recipe): wire id = global.<Bedrock model id>, the profiles that answered on 12/11:
--        sonnet-5-5 opus-5-5 haiku-5-5 sonnet-5 sonnet-4-6 opus-4-6 haiku-4-5 (Claude)
--        gpt-6-luna gpt-6-astra gpt-6-sol gpt-5.6-luna gpt-6.1-sol gpt-6.1-terra (GPT; the catalog's enabled flag of
--        gpt-6-sol / gpt-6.1-sol / gpt-6.1-terra stays the model switch, off today)
--      A model with no catalog row is skipped (NOTICE).
--   5. shared.llm_model_catalog.bedrock_profile_ids gains "acc1" = the same wire id on those 13 rows (TripPlanner's
--      legacy path and the wire-drift check read it; TripPlanner never calls acc1: its gateway holds credentials for
--      BEDROCK_ACCT_PRIMARY only, and it reads the deployment rows first).
--   6. Register 413.
--   The route order (tech lead S278, 15:45: Claude = Anthropic API, then acc1, then acc3; GPT = acc1, then acc3, then
--   the OpenAI API twin on the chain) is NOT set here: AA-CIS-App scripts/llm_ops/aa1095_acc1_route_order.py switches
--   acc1 on and saves the provider order as one route-control version (audited, one-click revert) after the deploy.
--   Chains, scopes, account_route and every existing deployment / connection row are not touched.
--
-- Behaviour on Dev. None until the switch: the connection is off, so deployment_plan never picks an acc1 row. The
-- static attempt count of the route rules (llm_route_chains attempt_cap) counts every deployment row, on or off: some
-- validate / writer chains then count 7 possible attempts (NOTICE list below; the route control reports them as
-- existing warnings). At run time refused acc3 rows are skipped by the refusal circuit at 0 USD and are not attempts.
--
-- Guards (RAISE rolls the whole file back):
--   before: the registry tables (304, 317, 322); connection acc3 is a bedrock assume_role row; no acc1 row of another
--           kind / role / ExternalId (a key collision).
--   after:  the acc1 row is bedrock / assume_role / aws_only with the role, ExternalId, region and env above and is
--           off unless an admin had switched it on; every seeded deployment's wire id is global.<id> and equals the
--           catalog's acc1 profile; an acc3 deployment of the same model with another wire id is a NOTICE.
--
-- Order with the code. Before the AA-CIS-App code PR (MIG-4: this migration-only PR, then the code). Old code never
-- calls acc1 (connection off). Contract file (it changes a CHECK and inserts config rows): applied on Dev by the Infra
-- Migration Apply once every job table is idle (root ADR 0017a).
--
-- Preflight (read-only, as cis_app; expected: acc3_rows = 1, acc1_rows = 0, models = 13):
--   SELECT (SELECT count(*) FROM shared.llm_provider_connection WHERE connection_key = 'acc3') AS acc3_rows,
--          (SELECT count(*) FROM shared.llm_provider_connection WHERE connection_key = 'acc1') AS acc1_rows,
--          (SELECT count(*) FROM shared.llm_model_catalog WHERE model_key IN ('sonnet-5-5', 'opus-5-5', 'haiku-5-5',
--             'sonnet-5', 'sonnet-4-6', 'opus-4-6', 'haiku-4-5', 'gpt-6-luna', 'gpt-6-astra', 'gpt-6-sol',
--             'gpt-5.6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra')) AS models;
--
-- Rollback (a NEW forward migration): switch acc1 off (PATCH /admin/llm-connections/acc1 {"enabled": false}) first,
-- revert the route-control version, then DELETE the 13 acc1 deployments, the cost policy row and the connection, and
-- remove the acc1 key of bedrock_profile_ids.
--
-- X1. One BEGIN … COMMIT block, no role switch. DB Migrate runs cis/ files as cis_owner, the owner of these tables.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    actor     constant text := 'migration-413-aa1095';
    ext_id    constant text := 'aa1095-satellite-bedrock-acc1';
    models    constant jsonb := '{
        "sonnet-5-5":    "global.anthropic.claude-sonnet-5-5",
        "opus-5-5":      "global.anthropic.claude-opus-5-5",
        "haiku-5-5":     "global.anthropic.claude-haiku-5-5",
        "sonnet-5":      "global.anthropic.claude-sonnet-5",
        "sonnet-4-6":    "global.anthropic.claude-sonnet-4-6",
        "opus-4-6":      "global.anthropic.claude-opus-4-6-v1",
        "haiku-4-5":     "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "gpt-6-luna":    "global.openai.gpt-6-luna",
        "gpt-6-astra":   "global.openai.gpt-6-astra",
        "gpt-6-sol":     "global.openai.gpt-6-sol",
        "gpt-5.6-luna":  "global.openai.gpt-5.6-luna",
        "gpt-6.1-sol":   "global.openai.gpt-6.1-sol",
        "gpt-6.1-terra": "global.openai.gpt-6.1-terra"
    }'::jsonb;
    acc3      record;
    pol       record;
    c         record;
    missing   text;
    bad       text;
    seeded    int;
BEGIN
    -- ── 0. Guards: predecessors ─────────────────────────────────────────────────────────────────────────────────
    IF to_regclass('shared.llm_provider_connection') IS NULL OR to_regclass('shared.llm_model_deployment') IS NULL
       OR to_regclass('shared.llm_connection_cost_policy') IS NULL THEN
        RAISE EXCEPTION 'migration 413: the connection registry (304), the cost policy (317) is missing';
    END IF;
    SELECT * INTO acc3 FROM shared.llm_provider_connection WHERE connection_key = 'acc3';
    IF NOT FOUND OR acc3.kind <> 'bedrock' OR acc3.auth_mode <> 'assume_role' THEN
        RAISE EXCEPTION 'migration 413: connection acc3 is missing or not a bedrock assume_role row';
    END IF;
    SELECT * INTO c FROM shared.llm_provider_connection WHERE connection_key = 'acc1';
    IF FOUND AND (c.kind <> 'bedrock' OR c.auth_mode <> 'assume_role' OR c.external_id IS DISTINCT FROM ext_id
                  OR c.role_name IS DISTINCT FROM 'AA1-Bedrock-Invoker') THEN
        RAISE EXCEPTION 'migration 413: a connection acc1 exists with another kind / role / ExternalId';
    END IF;

    -- ── 1. The account env allowlist: + BEDROCK_ACCT_PAYER ────────────────────────────────────────────────────
    ALTER TABLE shared.llm_provider_connection DROP CONSTRAINT IF EXISTS llm_provider_connection_account_id_env_check;
    ALTER TABLE shared.llm_provider_connection
        ADD CONSTRAINT llm_provider_connection_account_id_env_check
        CHECK (account_id_env IS NULL OR account_id_env IN ('BEDROCK_ACCT_PRIMARY', 'BEDROCK_ACCT_PAYER'));
    COMMENT ON CONSTRAINT llm_provider_connection_account_id_env_check ON shared.llm_provider_connection IS
        'AA-1095 (CIS 413): + BEDROCK_ACCT_PAYER (acc1, Bedrock serving while acc3 is blocked); was '
        'BEDROCK_ACCT_PRIMARY only (304). Same list as shared/llm_client/connections.ACCOUNT_ID_ENVS.';

    -- ── 2. The connection (off) ───────────────────────────────────────────────────────────────────────────────
    INSERT INTO shared.llm_provider_connection
        (connection_key, kind, auth_mode, label, account_id, account_id_env, region, role_name, external_id,
         secret_name, credential_env, base_url, data_scope, timeout_s, max_retries, max_concurrency, rpm_limit,
         monthly_budget_usd, enabled, notes, updated_by, failure_domain, served_roles, balance_source)
    VALUES
        ('acc1', 'bedrock', 'assume_role', 'Bedrock, payer account (assume role)', NULL, 'BEDROCK_ACCT_PAYER',
         'us-west-1', 'AA1-Bedrock-Invoker', ext_id, NULL, NULL, NULL, 'aws_only', acc3.timeout_s, acc3.max_retries,
         acc3.max_concurrency, acc3.rpm_limit, coalesce(acc3.monthly_budget_usd, 100), false,
         'AA-1095 (S278): Bedrock chat models while acc3 is blocked (root ADR 0001a, Note S278). Seeded off; '
         'scripts/llm_ops/aa1095_acc1_route_order.py switches it on and sets the provider order.',
         actor, 'bedrock:BEDROCK_ACCT_PAYER:aws_only', ARRAY['writer', 'judge', 'validate']::text[], 'none')
    ON CONFLICT (connection_key) DO NOTHING;

    -- ── 3. Its cost policy (acc3's) ───────────────────────────────────────────────────────────────────────────
    SELECT alert_pct, hard_stop INTO pol FROM shared.llm_connection_cost_policy WHERE connection_key = 'acc3';
    INSERT INTO shared.llm_connection_cost_policy (connection_key, alert_pct, hard_stop, notes, updated_by)
    VALUES ('acc1', coalesce(pol.alert_pct, 80), coalesce(pol.hard_stop, true),
            'AA-1095 (S278): copied from acc3 (migration 413).', actor)
    ON CONFLICT (connection_key) DO NOTHING;

    -- ── 4. The 13 deployments, behind every existing deployment of the model ─────────────────────────────────
    SELECT string_agg(m.key, ', ' ORDER BY m.key) INTO missing
      FROM jsonb_each_text(models) AS m (key, value)
     WHERE NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog k WHERE k.model_key = m.key);
    IF missing IS NOT NULL THEN
        RAISE NOTICE 'migration 413: no catalog row, no acc1 deployment: %', missing;
    END IF;

    INSERT INTO shared.llm_model_deployment
        (model_key, connection_key, wire_id, priority, enabled, origin, notes, updated_by)
    SELECT m.key, 'acc1', m.value, 100, true, 'seed',
           'AA-1095 (S278): answered a Converse call on acc1 through this profile (12/11/2026).', actor
      FROM jsonb_each_text(models) AS m (key, value)
      JOIN shared.llm_model_catalog k ON k.model_key = m.key
    ON CONFLICT (model_key, connection_key) DO NOTHING;

    -- ── 5. The catalog's acc1 profile ─────────────────────────────────────────────────────────────────────────
    UPDATE shared.llm_model_catalog k
       SET bedrock_profile_ids = coalesce(k.bedrock_profile_ids, '{}'::jsonb) || jsonb_build_object('acc1', m.value),
           updated_at          = now(),
           updated_by          = actor
      FROM jsonb_each_text(models) AS m (key, value)
     WHERE k.model_key = m.key AND k.provider = 'bedrock'
       AND (k.bedrock_profile_ids ->> 'acc1') IS DISTINCT FROM m.value;

    -- ── 6. Guards: the rows as decided ────────────────────────────────────────────────────────────────────────
    SELECT * INTO c FROM shared.llm_provider_connection WHERE connection_key = 'acc1';
    IF c.kind <> 'bedrock' OR c.auth_mode <> 'assume_role' OR c.data_scope <> 'aws_only'
       OR c.role_name <> 'AA1-Bedrock-Invoker' OR c.external_id <> ext_id OR c.region <> 'us-west-1'
       OR c.account_id_env IS DISTINCT FROM 'BEDROCK_ACCT_PAYER' OR c.account_id IS NOT NULL THEN
        RAISE EXCEPTION 'migration 413: connection acc1 is not the decided row';
    END IF;
    IF c.enabled THEN
        RAISE NOTICE 'migration 413: connection acc1 is already switched on (kept)';
    END IF;

    SELECT string_agg(d.model_key, ', ' ORDER BY d.model_key) INTO bad
      FROM shared.llm_model_deployment d
      JOIN shared.llm_model_catalog k ON k.model_key = d.model_key
     WHERE d.connection_key = 'acc1'
       AND (d.wire_id IS DISTINCT FROM models ->> d.model_key
            OR (k.bedrock_profile_ids ->> 'acc1') IS DISTINCT FROM d.wire_id);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'migration 413: acc1 deployments whose wire id is not the decided profile or the catalog''s '
                        'acc1 profile: %', bad;
    END IF;

    SELECT string_agg(d.model_key || ' (' || d.wire_id || ')', ', ' ORDER BY d.model_key) INTO bad
      FROM shared.llm_model_deployment d
     WHERE d.connection_key = 'acc3' AND models ? d.model_key AND d.wire_id <> models ->> d.model_key;
    IF bad IS NOT NULL THEN
        RAISE NOTICE 'migration 413: acc3 deployments with another wire id than acc1''s: %', bad;
    END IF;

    SELECT count(*) INTO seeded FROM shared.llm_model_deployment WHERE connection_key = 'acc1';
    RAISE NOTICE 'migration 413: % acc1 deployments, connection acc1 enabled=%, monthly cap % USD', seeded, c.enabled,
        c.monthly_budget_usd;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('413', now(), 'AA-1095: Bedrock connection acc1 (off) + 13 chat-model deployments, account env allowlist '
                      '+ BEDROCK_ACCT_PAYER, catalog acc1 profiles')
ON CONFLICT (version) DO NOTHING;

COMMIT;
