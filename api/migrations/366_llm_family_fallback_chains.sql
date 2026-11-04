-- Migration 366: AA-1026 (AI gateway v2; tech lead request S267, 04/11) — every active stage route becomes an ordered
-- chain by model family, so a refused or failing model falls to the next: Claude Sonnet 4.6 / Haiku 4.5 come back as
-- Converse catalog rows (sonnet-4-6, haiku-4-5) and sit right behind their 5.5 siblings.
--
-- Why. Since 29/10 acc3 refuses the 5.x Claude models, GPT-6 Luna and GPT-5.6 Luna ("not available for this account",
-- AWS case open), while Claude Sonnet 4.6 and Claude Haiku 4.5 still answer there (probe of 04/11 05:02Z, test 07:42Z).
-- No route names them (327 T-4 moved every route off the legacy keys `sonnet` / `haiku`), so today: the writers answer
-- on GPT-6 Luna through the OpenAI API, tp_compose / segment_research / segment_research_batch have no fallback at
-- all, and the validate stages answer on acc2 only.
--
-- What.
--   1. Two catalog rows, the Converse twins of the legacy keys (same model, same acc3 profile; a legacy key keeps its
--      hard-coded InvokeModel chain, no data scope / budget / breaker, and drops json_schema: 327 T-4, lessons S251 /
--      S260). Each copies label base, vendor, prices, cache prices, max_output_tokens, supports_temperature and
--      capabilities from its legacy row; api_style converse, family anthropic, callable_via {llm_client}, enabled:
--        sonnet-4-6  <- sonnet   "Claude Sonnet 4.6"
--        haiku-4-5   <- haiku    "Claude Haiku 4.5"
--      and ONE deployment each, on acc3, with the wire id of the legacy row's acc3 deployment (migration 304:
--      global.anthropic.claude-sonnet-4-6 / global.anthropic.claude-haiku-4-5-...): priority 10, enabled, origin
--      bedrock_profile, i.e. the very row 304's seeding rule derives from the catalog row's acc3 profile (a 304 replay
--      gives the same row). No Anthropic API deployment (306 seeded those for the 5.x rows only; the 4.x rows stay
--      inside AWS, which also keeps every new chain within the gateway's 6 attempts per request).
--   2. The routes (fallback_model_ids only; primary, shadow, scopes, role, is_active untouched). A listed stage changes
--      only when its live row still has the primary and the old fallbacks below (an admin's later change is kept, NOTICE):
--
--      stage                    primary       old fallbacks                        new fallbacks
--      s1_generate              sonnet-5-5    gpt-6-luna-openai                    sonnet-4-6, haiku-4-5, gpt-6-luna-openai
--      supply_content_writer    sonnet-5-5    gpt-6-luna-openai                    sonnet-4-6, haiku-4-5, gpt-6-luna-openai
--      t2_generate              sonnet-5-5    gpt-6-luna-openai                    sonnet-4-6, haiku-4-5, gpt-6-luna-openai
--      t8_angle_gen             sonnet-5-5    gpt-6-luna-openai                    sonnet-4-6, haiku-4-5, gpt-6-luna-openai
--      t9_write                 sonnet-5-5    gpt-6-luna-openai                    sonnet-4-6, haiku-4-5, gpt-6-luna-openai
--      tp_compose               sonnet-5-5    (none)                               sonnet-4-6, haiku-4-5
--      a0_column_map            haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      s1_flag_fix              haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      s1_itinerary_nudge       haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      t2_flag_fix              haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      t2_itinerary_nudge       haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      t9_repair                haiku-5-5     gpt-6-luna-openai                    haiku-4-5, sonnet-4-6, gpt-6-luna-openai
--      segment_research         haiku-5-5     (none)                               haiku-4-5, sonnet-4-6
--      segment_research_batch   haiku-5-5     (none)                               haiku-4-5, sonnet-4-6
--      a3_atomize               gpt-6-luna    haiku-5-5, nova-lite                 gpt-5.6-luna, haiku-5-5, haiku-4-5, nova-lite
--      s1_source_facts          gpt-6-luna    haiku-5-5, nova-lite                 gpt-5.6-luna, haiku-5-5, haiku-4-5, nova-lite
--      a3_place_card_check      haiku-5-5     sonnet-5-5, llama4-scout             haiku-4-5, sonnet-5-5, sonnet-4-6, llama4-scout
--      a3_place_days            haiku-5-5     sonnet-5-5, nova-lite                haiku-4-5, sonnet-5-5, sonnet-4-6, nova-lite
--      a3_place_dedupe          gpt-6-luna    llama4-maverick, gpt-6-luna-openai   gpt-5.6-luna, llama4-maverick, gpt-6-luna-openai
--      a3_place_dedupe_check    haiku-5-5     nova-lite                            haiku-4-5, nova-lite
--      a3_place_locate          sonnet-5-5    haiku-5-5, nova-lite                 sonnet-4-6, haiku-5-5, haiku-4-5, nova-lite
--      t9_faq_answer_fit        sonnet-5-5    haiku-5-5, nova-lite                 sonnet-4-6, haiku-5-5, haiku-4-5, nova-lite
--      t9_faq_filter            haiku-5-5     nova-lite                            haiku-4-5, nova-lite
--
--      The order rule: the primary; the working older sibling right behind each family tier (Sonnet 5.5 -> 4.6,
--      Haiku 5.5 -> 4.5, GPT-6 Luna -> GPT-5.6 Luna); a writer then degrades (Sonnet) or climbs (Haiku) one Claude tier
--      (a large writer skips Haiku 5.5: refused with Sonnet 5.5, and the gateway's 6 attempts per request must still
--      reach the last rung with the Anthropic API on); the AWS models of the other failure domain the route already had
--      (acc2: 331 / 332 / 333 / 339); the OpenAI API last, only where the stage already allows vendor_direct.
--      Unchanged: the judges (GPT-5.6 Luna <-> GPT-6 Luna, Nova Lite, the OpenAI API: a judge never uses the writers'
--      family, ADR 0009 decision 12 / TL-10), a3_place_card and t10_claims (GPT + Amazon: they check Claude text or
--      are checked by Claude), the embed stages (two accounts already), and the three routes 336 switched off.
--      The place card and dedupe pairs keep disjoint makers (card: GPT + Amazon, card check: Claude + Meta; dedupe:
--      GPT + Meta, dedupe check: Claude + Amazon).
--   3. Register 366.
--
-- Needs code. A vendor_direct route is run AWS first across all its models only by the gateway of AA-CIS-App PR
-- "gateway tries a vendor_direct route on AWS first" (client.route_attempts, LLM_ROUTE_AWS_FIRST default on). Before
-- that code, sonnet-5-5 -> sonnet-4-6 on a vendor_direct writer would call Sonnet 5.5 on the Anthropic API ahead of
-- Sonnet 4.6 on acc3 once the anthropic-direct connection is switched on (off today: no key). So that PR is merged and
-- deployed BEFORE this file is applied. The admin route PATCH accepts these chains only with that order on.
--
-- Guards (RAISE rolls the whole file back):
--   before: the registry tables (304, 307, 311 allowed_scopes, 320 family / capabilities); 335 and 344 registered (the
--           routes this file reads); connection acc3 is a bedrock aws_only row; family 'anthropic' exists; each legacy
--           row is an enabled Anthropic Bedrock row with prices and an acc3 deployment whose wire id names its model;
--           a pre-existing sonnet-4-6 / haiku-4-5 row (a re-apply) is an Anthropic Converse row; every other model the
--           new chains name is an enabled catalog row callable by llm_client.
--   after:  the two rows and their acc3 deployments exist (enabled, no deployment elsewhere); no row outside the
--           listed stages carries this file's updated_by; every row this file changed has the primary, role, shadow,
--           scopes and is_active it had and the new fallbacks; every other row is byte for byte what it was; and on every row this file
--           changed (RAISE) — every other active row only gets a NOTICE — the chain rules of
--           api/services/llm_route_chains.py: at least one fallback; no model reached only outside AWS on an aws_only
--           stage; the models reached only outside AWS are the END of the chain; a judge names no Anthropic model; a
--           writer names only Anthropic models (but a vendor-only last resort) and no acc2-only chat model; a check
--           stage shares no family with its producer; at most 6 deployments in the stage's scopes (errors.MAX_CHAIN_
--           ATTEMPTS).
--   NOTICE: the stages changed and the stages skipped.
--
-- Behaviour on Dev. While acc3 refuses 5.x and GPT: the refused hops are skipped by their open circuits (0 USD), the
-- writers answer on Sonnet 4.6 / Haiku 4.5 on acc3 (3 / 15 and 1 / 5 USD per 1M tokens, the legacy prices) instead
-- of GPT-6 Luna on the OpenAI API; tp_compose and the segment research stages answer at all; the validate stages
-- answer on acc3 before acc2. The judges are unchanged (Nova Lite on acc2). When AWS reopens the 5.x models, every
-- stage is back on its primary. No contract-smoke order gate (unlike 331-333): the two rows are the Claude models the
-- legacy keys served for months; a reply that does not meet a stage's contract is billed-unusable and the route moves
-- on. Live check after the apply: the admin connection test of both rows on acc3 and the $0 route check
-- (scripts/llm_ops/route_chain_check.py).
--
-- Class: contract (root ADR 0017 d3: INSERT of config rows and UPDATE of routing rows), no "-- migrate:" directive.
-- MIG-4 order: (0) the AA-CIS-App code PR above merges and deploys; (1) this migration-only PR merges; (2) the byte
-- copy in AA-CIS-Infra db/migrations/cis/ merges (Copy Guard green); (3) Infra Migration Apply with the job tables idle
-- (DM-11.4, root ADR 0017a); (4) the bot/db-schema-reference PR merges. After apply: POST /admin/writing-memory/
-- releases/reverify-scan (writer routes changed outside the admin PATCH that triggers it); the root route page
-- (tools/docs/gen_llm_routes.py dump / import). DM-7.1: no new table.
--
-- Rollback (a NEW forward migration): fallback_model_ids back to the "old fallbacks" column on the rows updated_by
-- 'migration-366-aa1026' (or the admin route PATCH per stage, with a break-glass reason); the two catalog rows may be
-- switched off (enabled = false) in the admin catalog, which makes every route skip them at once.
--
-- X1. One BEGIN … COMMIT block, no role switch (DB Migrate runs cis/ files as cis_owner, the owner of every table
-- here). Grants: none (existing tables).

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    actor     constant text := 'migration-366-aa1026';
    hop       constant text := 'gpt-6-luna-openai';
    new_keys  constant text[] := ARRAY['sonnet-4-6', 'haiku-4-5'];
    legacy    constant text[] := ARRAY['sonnet', 'haiku'];
    labels    constant text[] := ARRAY['Claude Sonnet 4.6', 'Claude Haiku 4.5'];
    wire_like constant text[] := ARRAY['^(global|us)\.anthropic\.claude-sonnet-4-6', '^(global|us)\.anthropic\.claude-haiku-4-5'];
    pairs     constant text[] := ARRAY['a3_place_card_check', 'a3_place_card', 'a3_place_dedupe_check', 'a3_place_dedupe'];
    max_attempts constant int := 6;             -- shared/llm_client/errors.py MAX_CHAIN_ATTEMPTS
    plan      jsonb := $plan$[
        ["s1_generate",            "sonnet-5-5", ["gpt-6-luna-openai"], ["sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]],
        ["supply_content_writer",  "sonnet-5-5", ["gpt-6-luna-openai"], ["sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]],
        ["t2_generate",            "sonnet-5-5", ["gpt-6-luna-openai"], ["sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]],
        ["t8_angle_gen",           "sonnet-5-5", ["gpt-6-luna-openai"], ["sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]],
        ["t9_write",               "sonnet-5-5", ["gpt-6-luna-openai"], ["sonnet-4-6", "haiku-4-5", "gpt-6-luna-openai"]],
        ["tp_compose",             "sonnet-5-5", [],                    ["sonnet-4-6", "haiku-4-5"]],
        ["a0_column_map",          "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["s1_flag_fix",            "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["s1_itinerary_nudge",     "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["t2_flag_fix",            "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["t2_itinerary_nudge",     "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["t9_repair",              "haiku-5-5",  ["gpt-6-luna-openai"], ["haiku-4-5", "sonnet-4-6", "gpt-6-luna-openai"]],
        ["segment_research",       "haiku-5-5",  [],                    ["haiku-4-5", "sonnet-4-6"]],
        ["segment_research_batch", "haiku-5-5",  [],                    ["haiku-4-5", "sonnet-4-6"]],
        ["a3_atomize",             "gpt-6-luna", ["haiku-5-5", "nova-lite"],
                                                 ["gpt-5.6-luna", "haiku-5-5", "haiku-4-5", "nova-lite"]],
        ["s1_source_facts",        "gpt-6-luna", ["haiku-5-5", "nova-lite"],
                                                 ["gpt-5.6-luna", "haiku-5-5", "haiku-4-5", "nova-lite"]],
        ["a3_place_card_check",    "haiku-5-5",  ["sonnet-5-5", "llama4-scout"],
                                                 ["haiku-4-5", "sonnet-5-5", "sonnet-4-6", "llama4-scout"]],
        ["a3_place_days",          "haiku-5-5",  ["sonnet-5-5", "nova-lite"],
                                                 ["haiku-4-5", "sonnet-5-5", "sonnet-4-6", "nova-lite"]],
        ["a3_place_dedupe",        "gpt-6-luna", ["llama4-maverick", "gpt-6-luna-openai"],
                                                 ["gpt-5.6-luna", "llama4-maverick", "gpt-6-luna-openai"]],
        ["a3_place_dedupe_check",  "haiku-5-5",  ["nova-lite"],         ["haiku-4-5", "nova-lite"]],
        ["a3_place_locate",        "sonnet-5-5", ["haiku-5-5", "nova-lite"],
                                                 ["sonnet-4-6", "haiku-5-5", "haiku-4-5", "nova-lite"]],
        ["t9_faq_answer_fit",      "sonnet-5-5", ["haiku-5-5", "nova-lite"],
                                                 ["sonnet-4-6", "haiku-5-5", "haiku-4-5", "nova-lite"]],
        ["t9_faq_filter",          "haiku-5-5",  ["nova-lite"],         ["haiku-4-5", "nova-lite"]]
    ]$plan$::jsonb;
    stages    text[];
    before    jsonb;
    others    jsonb;
    outside   text[];                -- models with deployments, none of them inside AWS (reached only outside)
    acc2_only text[];                -- chat models whose every deployment is on connection acc2
    changed   text[] := ARRAY[]::text[];
    skipped   text[] := ARRAY[]::text[];
    p         jsonb;
    r         record;
    old_f     text[];
    new_f     text[];
    wire      text;
    missing   text;
    bad       text;
    i         int;
BEGIN
    -- ── 0. Guard: predecessors ───────────────────────────────────────────────────────────────────────────────────
    IF to_regclass('shared.llm_role_config') IS NULL OR to_regclass('shared.llm_model_catalog') IS NULL
       OR to_regclass('shared.llm_model_deployment') IS NULL OR to_regclass('shared.llm_provider_connection') IS NULL
       OR to_regclass('shared.llm_model_family') IS NULL THEN
        RAISE EXCEPTION 'migration 366: a registry table is missing (llm_role_config, llm_model_catalog, '
                        'llm_model_deployment, llm_provider_connection, llm_model_family): apply 304 and 320';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = '344')
       OR NOT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = '335') THEN
        RAISE EXCEPTION 'migration 366: apply 335 and 344 first (the routes this file reads)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.llm_provider_connection
                    WHERE connection_key = 'acc3' AND kind = 'bedrock' AND data_scope = 'aws_only') THEN
        RAISE EXCEPTION 'migration 366: connection acc3 is not a bedrock aws_only row';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shared.llm_model_family WHERE family_key = 'anthropic') THEN
        RAISE EXCEPTION 'migration 366: family anthropic is missing (migration 320)';
    END IF;
    FOR i IN 1 .. 2 LOOP
        IF NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog
                        WHERE model_key = legacy[i] AND enabled AND vendor = 'anthropic' AND provider = 'bedrock'
                          AND price_in_per_mtok IS NOT NULL AND price_out_per_mtok IS NOT NULL) THEN
            RAISE EXCEPTION 'migration 366: the legacy row % (an enabled Anthropic Bedrock row with prices) is missing',
                            legacy[i];
        END IF;
        SELECT d.wire_id INTO wire FROM shared.llm_model_deployment d
         WHERE d.model_key = legacy[i] AND d.connection_key = 'acc3';
        IF wire IS NULL OR wire !~ wire_like[i] THEN
            RAISE EXCEPTION 'migration 366: the acc3 deployment of % is missing or names another model (%)',
                            legacy[i], coalesce(wire, 'none');
        END IF;
        IF EXISTS (SELECT 1 FROM shared.llm_model_catalog
                    WHERE model_key = new_keys[i]
                      AND (api_style <> 'converse' OR vendor <> 'anthropic' OR provider <> 'bedrock')) THEN
            RAISE EXCEPTION 'migration 366: % exists and is not an Anthropic Bedrock Converse row', new_keys[i];
        END IF;
    END LOOP;
    SELECT string_agg(DISTINCT k, ', ') INTO missing
      FROM jsonb_array_elements(plan) AS e (v)
     CROSS JOIN LATERAL jsonb_array_elements_text(e.v -> 3) AS f (k)
     WHERE NOT (k = ANY (new_keys))
       AND NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog m
                        WHERE m.model_key = k AND m.enabled AND 'llm_client' = ANY (m.callable_via));
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'migration 366: models of the new chains that are not enabled catalog rows callable by '
                        'llm_client: %', missing;
    END IF;

    SELECT array_agg(e.v ->> 0) INTO stages FROM jsonb_array_elements(plan) AS e (v);
    SELECT coalesce(jsonb_object_agg(stage, to_jsonb(rc) - 'updated_at' - 'updated_by' - 'fallback_model_ids'),
                    '{}'::jsonb)
      INTO before
      FROM shared.llm_role_config rc WHERE stage = ANY (stages);
    SELECT coalesce(jsonb_object_agg(stage, to_jsonb(rc) - 'updated_at'), '{}'::jsonb)
      INTO others
      FROM shared.llm_role_config rc WHERE NOT (stage = ANY (stages));

    -- ── 1. The Converse twins of the legacy keys, on acc3 only ──────────────────────────────────────────────────
    FOR i IN 1 .. 2 LOOP
        INSERT INTO shared.llm_model_catalog
            (model_key, label, vendor, family, provider, api_style, bedrock_profile_ids, wire_model, callable_via,
             supports_temperature, max_output_tokens, price_in_per_mtok, price_out_per_mtok, price_cache_read_per_mtok,
             price_cache_write_per_mtok, price_source, enabled, blocked_reason, notes, capabilities, updated_by)
        SELECT new_keys[i], labels[i], l.vendor, 'anthropic', 'bedrock', 'converse',
               jsonb_build_object('acc3', d.wire_id), NULL, ARRAY['llm_client'],
               l.supports_temperature, l.max_output_tokens, l.price_in_per_mtok, l.price_out_per_mtok,
               l.price_cache_read_per_mtok, l.price_cache_write_per_mtok,
               'copied from the legacy row ' || legacy[i] || ' (migration 366)', true, NULL,
               'The Converse twin of the legacy key ' || legacy[i] || ' (same model and acc3 profile), so family '
               || 'fallback chains can name it with data scope, budget and breaker (AA-1026 S267, migration 366).',
               coalesce(l.capabilities, '{}'::jsonb), actor
          FROM shared.llm_model_catalog l
          JOIN shared.llm_model_deployment d ON d.model_key = l.model_key AND d.connection_key = 'acc3'
         WHERE l.model_key = legacy[i]
        ON CONFLICT (model_key) DO NOTHING;

        INSERT INTO shared.llm_model_deployment
            (model_key, connection_key, wire_id, priority, enabled, batch_capable, request_defaults, origin, notes,
             updated_by)
        SELECT new_keys[i], 'acc3', d.wire_id, 10, true, false, '{}'::jsonb, 'bedrock_profile',
               'Migration 366: the legacy row ' || legacy[i] || '''s acc3 profile, called through Converse.', actor
          FROM shared.llm_model_deployment d
         WHERE d.model_key = legacy[i] AND d.connection_key = 'acc3'
        ON CONFLICT (model_key, connection_key) DO NOTHING;
    END LOOP;

    -- Reach of every model, after step 1 (every deployment row, enabled or not: the policy, as the admin counts it).
    SELECT coalesce(array_agg(k), ARRAY[]::text[]) INTO outside
      FROM (SELECT d.model_key AS k
              FROM shared.llm_model_deployment d
              JOIN shared.llm_provider_connection c ON c.connection_key = d.connection_key
             GROUP BY d.model_key
            HAVING bool_and(c.data_scope <> 'aws_only')) o;
    SELECT coalesce(array_agg(k), ARRAY[]::text[]) INTO acc2_only
      FROM (SELECT d.model_key AS k
              FROM shared.llm_model_deployment d
              JOIN shared.llm_model_catalog m ON m.model_key = d.model_key AND m.api_style <> 'embed'
             GROUP BY d.model_key
            HAVING bool_and(d.connection_key = 'acc2')) a;

    -- ── 2. The routes ──────────────────────────────────────────────────────────────────────────────────────────
    FOR p IN SELECT v FROM jsonb_array_elements(plan) AS e (v) LOOP
        old_f := ARRAY(SELECT jsonb_array_elements_text(p -> 2));
        new_f := ARRAY(SELECT jsonb_array_elements_text(p -> 3));
        SELECT rc.stage, rc.model_id, rc.is_active, coalesce(rc.fallback_model_ids, ARRAY[]::text[]) AS f
          INTO r
          FROM shared.llm_role_config rc WHERE rc.stage = p ->> 0;
        IF NOT FOUND OR r.model_id <> p ->> 1 OR NOT r.is_active OR r.f = new_f OR r.f <> old_f THEN
            skipped := skipped || (p ->> 0);                 -- no row, re-routed / switched off, or already in place
            CONTINUE;
        END IF;
        UPDATE shared.llm_role_config
           SET fallback_model_ids = new_f,
               updated_at         = now(),
               updated_by         = actor
         WHERE stage = p ->> 0;
        changed := changed || (p ->> 0);
    END LOOP;

    -- ── 3. Guards: the rows and routes as decided ─────────────────────────────────────────────────────────────
    SELECT string_agg(k, ', ') INTO bad
      FROM unnest(new_keys) AS k
     WHERE NOT EXISTS (SELECT 1 FROM shared.llm_model_catalog m
                        WHERE m.model_key = k AND m.enabled AND m.api_style = 'converse'
                          AND coalesce(m.family, m.vendor) = 'anthropic' AND 'llm_client' = ANY (m.callable_via))
        OR NOT EXISTS (SELECT 1 FROM shared.llm_model_deployment d
                        WHERE d.model_key = k AND d.connection_key = 'acc3' AND d.enabled)
        OR EXISTS (SELECT 1 FROM shared.llm_model_deployment d WHERE d.model_key = k AND d.connection_key <> 'acc3');
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'migration 366: catalog rows not an enabled Anthropic Converse row deployed on acc3 only: %', bad;
    END IF;

    SELECT string_agg(rc.stage, ', ' ORDER BY rc.stage) INTO bad
      FROM shared.llm_role_config rc
     WHERE (rc.updated_by = actor AND NOT (rc.stage = ANY (stages)))
        OR (rc.stage = ANY (changed)
            AND (to_jsonb(rc) - 'updated_at' - 'updated_by' - 'fallback_model_ids' IS DISTINCT FROM before -> rc.stage
            OR rc.fallback_model_ids IS DISTINCT FROM ARRAY(
                   SELECT jsonb_array_elements_text(e.v -> 3) FROM jsonb_array_elements(plan) AS e (v)
                    WHERE e.v ->> 0 = rc.stage)));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'migration 366: rows changed beyond their decided fallbacks: %', bad;
    END IF;

    SELECT string_agg(o.key, ', ' ORDER BY o.key) INTO bad
      FROM jsonb_each(others) AS o (key, value)
      LEFT JOIN shared.llm_role_config rc ON rc.stage = o.key
     WHERE rc.stage IS NULL OR to_jsonb(rc) - 'updated_at' IS DISTINCT FROM o.value;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'migration 366: rows outside the listed stages changed: %', bad;
    END IF;

    -- The chain rules (api/services/llm_route_chains.py violations) on every active row: RAISE for a row this file
    -- changed, NOTICE for any other (an admin's route is reported, never rewritten here).
    FOR r IN
        WITH routes AS (
            SELECT rc.stage, rc.role, rc.shadow_model_id,
                   coalesce(rc.allowed_scopes, ARRAY['aws_only']) AS scopes,
                   coalesce(rc.fallback_model_ids, ARRAY[]::text[]) AS f,
                   ARRAY[rc.model_id] || coalesce(rc.fallback_model_ids, ARRAY[]::text[]) AS chain
              FROM shared.llm_role_config rc WHERE rc.is_active
        ), keyed AS (
            SELECT rt.*, k.key, k.ord, k.key = ANY (outside) AS out_only,
                   (SELECT coalesce(m.family, m.vendor) FROM shared.llm_model_catalog m WHERE m.model_key = k.key) AS fam
              FROM routes rt CROSS JOIN LATERAL unnest(rt.chain) WITH ORDINALITY AS k (key, ord)
        )
        SELECT rt.stage, rule FROM routes rt CROSS JOIN LATERAL (
            SELECT 'no_fallback' AS rule WHERE cardinality(rt.f) = 0
            UNION ALL
            SELECT 'vendor_on_aws_only' WHERE NOT ('vendor_direct' = ANY (rt.scopes))
                   AND EXISTS (SELECT 1 FROM keyed WHERE keyed.stage = rt.stage AND keyed.out_only)
            UNION ALL
            SELECT 'vendor_not_last' WHERE EXISTS (
                   SELECT 1 FROM keyed a JOIN keyed b ON a.stage = b.stage AND b.ord > a.ord
                    WHERE a.stage = rt.stage AND a.out_only AND NOT b.out_only)
            UNION ALL
            SELECT 'judge_writer_family' WHERE rt.role = 'judge' AND (
                   EXISTS (SELECT 1 FROM keyed WHERE keyed.stage = rt.stage AND keyed.fam = 'anthropic')
                   OR EXISTS (SELECT 1 FROM shared.llm_model_catalog m WHERE m.model_key = rt.shadow_model_id
                                AND coalesce(m.family, m.vendor) = 'anthropic'))
            UNION ALL
            SELECT 'writer_family' WHERE rt.role = 'writer' AND EXISTS (
                   SELECT 1 FROM keyed WHERE keyed.stage = rt.stage AND keyed.fam IS DISTINCT FROM 'anthropic'
                      AND keyed.fam IS NOT NULL
                      AND NOT (keyed.ord = cardinality(rt.chain) AND keyed.out_only
                               AND 'vendor_direct' = ANY (rt.scopes)))
            UNION ALL
            SELECT 'writer_acc2' WHERE rt.role = 'writer' AND EXISTS (
                   SELECT 1 FROM keyed WHERE keyed.stage = rt.stage AND keyed.key = ANY (acc2_only))
            UNION ALL
            SELECT 'attempt_cap' WHERE (
                   SELECT count(*) FROM (SELECT DISTINCT unnest(rt.chain) AS key) u
                     JOIN shared.llm_model_deployment d ON d.model_key = u.key
                     JOIN shared.llm_provider_connection c ON c.connection_key = d.connection_key
                    WHERE c.data_scope = ANY (rt.scopes)) > max_attempts
            UNION ALL
            SELECT 'check_pair_family' WHERE EXISTS (
                   SELECT 1 FROM generate_series(1, cardinality(pairs) / 2) AS g (n)
                     JOIN routes prod ON prod.stage = pairs[2 * g.n]
                    WHERE pairs[2 * g.n - 1] = rt.stage
                      AND EXISTS (SELECT 1 FROM keyed a JOIN keyed b ON a.fam = b.fam
                                   WHERE a.stage = rt.stage AND b.stage = prod.stage))
        ) rules
        ORDER BY 1, 2
    LOOP
        IF r.stage = ANY (changed) THEN
            RAISE EXCEPTION 'migration 366: the new route of % breaks the chain rule %', r.stage, r.rule;
        END IF;
        RAISE NOTICE 'migration 366: route % (not changed here) breaks the chain rule %', r.stage, r.rule;
    END LOOP;

    RAISE NOTICE 'migration 366: family fallback chains set on: %',
        coalesce(nullif(array_to_string(changed, ', '), ''), '(none)');
    IF cardinality(skipped) > 0 THEN
        RAISE NOTICE 'migration 366: listed stages left as they are (no row, re-routed or switched off since 04/11, or '
                     'already in place): %', array_to_string(skipped, ', ');
    END IF;
END $$;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('366', now(), 'AA-1026 (S267): family fallback chains: Converse rows sonnet-4-6 / haiku-4-5 (acc3) and the '
                      'ordered family chains of 23 stage routes (AWS first, the OpenAI API last on vendor_direct stages)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
