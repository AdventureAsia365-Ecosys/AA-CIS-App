-- Migration 387: AA-1047 (S272 Quality W1, root ADR 0020 "policy registry") — register the prompt versions the
-- writing policy registry moves, in shared.prompt_version, so the ledger can tell them from the old ones.
--
-- Why. AA-1047 compiles the writing rules from services/quality/writing_policy.yaml (policy_sha stamped on every
-- artefact). A prompt whose source file reads the policy (it imports the policy module, or a constant taken from it
-- such as SEO_META_MIN: prompt_registry.reads_policy) now hashes the compiled policy_sha too
-- (shared/llm_client/prompt_registry.prompt_hash_of), so these sixteen stages get a new version:
--   t9_write v4             the background line (D1) and the CTA lines (the literal line, word for word): critic fix
--                           F9, the contradiction-resolving lines; the rest renders byte-equal
--   t9_repair v2            the background line of the sentence repair (D1); the SEO fit and FAQ prompts render
--                           byte-equal (bands and pair count from the policy)
--   writing_memory_eval v3  the R2 judge names the policy (version + sha) and reads the policy's label rubric; the
--                           16000/9000 clipping is gone (evidence_view: an item over the cap is excluded, never cut)
--   s1_brand_audit v2, t2_brand_audit v2   the SEO bands rendered from the policy (byte-equal text)
--   a3_place_card v2        the place-card facts scope rendered from the policy (byte-equal text)
--   s1_generate v2, t2_generate v2, s1_judge v2, t2_judge v2, s1_flag_fix v2, t2_flag_fix v2, a3_place_card_check v2,
--   t9_faq_answer_fit v2, t10_claims v2, t10_judge v2
--                           their files render policy values through constants or helpers (the S1 / T2 background
--                           lines, the meta band, the FAQ pair count): a policy edit is now a version of each; the
--                           text they send today is unchanged except where the lines above say so. t10_judge v2 also
--                           hashes f9_user_prompt (the F9 user prompt, which renders the policy's background line).
-- The gateway writes llm_call_log.prompt_version only when the deployed source, the manifest
-- (shared/llm_client/prompt_versions.json) and a row of this table agree (prompt_registry.version_for); without these
-- rows every call of these stages after the code deploy would carry prompt_version NULL.
--
-- What.
--   1. Sixteen rows (sha256 of the hashed symbols' source text plus the policy_sha, never the text; created_by
--      'migration-387'), the output of
--      `python3 scripts/prompt_registry_check.py --sql --unregistered --created-by migration-387` on the code PR's
--      tree. ON CONFLICT (stage, version) DO NOTHING.
--   2. Guard: each row exists with exactly this hash and path (a version registered earlier with another hash would
--      be kept silently by the ON CONFLICT and every call of it would carry prompt_version NULL).
--   3. Register 387.
--
-- Behaviour. Until the code PR deploys, no deployed prompt matches these versions and nothing changes (calls keep
-- their current version). After it deploys, within the registry cache TTL, rows carry the new versions. No route,
-- model, cost or prompt change by this file. Older versions stay registered.
--
-- Class: contract (root ADR 0017 d3: rows into an existing table), so no migrate directive header and MIG-4 order:
-- (1) this migration-only PR merges to AA-CIS-App main; (2) the byte copy in AA-CIS-Infra db/migrations/cis/ merges;
-- (3) Infra Migration Apply applies it once every job table is idle (root ADR 0017a); (4) then the AA-1047 policy
-- registry code PR merges. No readiness bump. DM-7.1: no new table.
--
-- Rollback (a NEW forward migration): DELETE FROM shared.prompt_version WHERE created_by = 'migration-387';
-- (calls of these versions then carry prompt_version NULL; rows already written keep their version).
--
-- The SQL that is applied to Dev is the copy in AA-CIS-Infra db/migrations/cis/; keep the two byte-identical.
--
-- X1. One BEGIN … COMMIT block, no role switch. Runs as cis_owner, the owner of the table. Grants: none (cis_app keeps
-- SELECT from 321).

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF to_regclass('shared.prompt_version') IS NULL THEN
        RAISE EXCEPTION 'migration 387: shared.prompt_version is missing (apply 321 first)';
    END IF;
END $$;

-- ── 1. the manifest rows ─────────────────────────────────────────────────────────────────────────────────────────

INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) VALUES
    ('a3_place_card', 'v2', '588f1e99649190710138baeb6259b46a28818598c0f113dacbc0472ac8682db7', 'services/acp_shared/place_cards.py', 'migration-387'),
    ('a3_place_card_check', 'v2', 'a3187a8d39ec38fc78c964a8ada0d3c6ca4ae422815725a2d420164515d7d39c', 'services/acp_shared/place_cards.py', 'migration-387'),
    ('s1_brand_audit', 'v2', '504c482127b6b7b96cc1f114ba0a927e8f787626b9caaad07e34a7b9326420a2', 'services/content_generation/brand_standards.py', 'migration-387'),
    ('s1_flag_fix', 'v2', 'd3ea3ec4b233e245fa5c08e9363cda9f1796b7ff38743d6946e369aa311e0977', 'services/content_generation/flag_fix_node.py', 'migration-387'),
    ('s1_generate', 'v2', '01c18efbe2412240412253f7d5e65564bdf7cf7e6f6fac809c610894a5382476', 'services/content_generation/s1_writer_v2.py', 'migration-387'),
    ('s1_judge', 'v2', '808a07b8b5d5cd7832d9898341ff1bf6e358b7936df32d0c13a6e212dd194481', 'services/content_generation/brand_fit_v2.py', 'migration-387'),
    ('t10_claims', 'v2', 'dcff98166099c0dc9c5c6396e90d9ca1aa8355cb66d6d8bc9a16d57c7ed478c1', 'services/acp_content_writing/claim_rules.py', 'migration-387'),
    ('t10_judge', 'v2', '5f7d5704af9bf853524dddb7927e90e780d93aad65c06259244bda3c16b2f75b', 'services/acp_content_writing/quality_gates.py', 'migration-387'),
    ('t2_brand_audit', 'v2', '504c482127b6b7b96cc1f114ba0a927e8f787626b9caaad07e34a7b9326420a2', 'services/content_generation/brand_standards.py', 'migration-387'),
    ('t2_flag_fix', 'v2', 'd3ea3ec4b233e245fa5c08e9363cda9f1796b7ff38743d6946e369aa311e0977', 'services/content_generation/flag_fix_node.py', 'migration-387'),
    ('t2_generate', 'v2', '88df7c8f2f84c3d940fc69e65572ae4c72e206b7fec5b285c6c2ffd322ebf355', 'services/content_generation/prompts.py', 'migration-387'),
    ('t2_judge', 'v2', '808a07b8b5d5cd7832d9898341ff1bf6e358b7936df32d0c13a6e212dd194481', 'services/content_generation/brand_fit_v2.py', 'migration-387'),
    ('t9_faq_answer_fit', 'v2', '290742f42bafbda199d5976c4270051f2dab6669b9434ba4b4eff73e06cb2146', 'services/acp_content_writing/faq_answer_fit.py', 'migration-387'),
    ('t9_repair', 'v2', '346460b3cce89e9cee5df178d1fe26f29e0a9fe526890e54a96dc79fd747b9fd', 'services/acp_content_writing/generate.py', 'migration-387'),
    ('t9_write', 'v4', 'ed6b2767697544b46b6c97a1c52d7baa47faaafc972a7c62016dbf5c63ee4ef8', 'services/acp_content_writing/prompts.py', 'migration-387'),
    ('writing_memory_eval', 'v3', '75a825a40e3370e4bb72b2bb8750ccba1609788e731cbada3b95ef2d5932851e', 'services/writing_memory/eval_judge.py', 'migration-387')
ON CONFLICT (stage, version) DO NOTHING;

-- ── 2. guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    expected CONSTANT text[][] := ARRAY[
        ['a3_place_card', 'v2', '588f1e99649190710138baeb6259b46a28818598c0f113dacbc0472ac8682db7', 'services/acp_shared/place_cards.py'],
        ['a3_place_card_check', 'v2', 'a3187a8d39ec38fc78c964a8ada0d3c6ca4ae422815725a2d420164515d7d39c', 'services/acp_shared/place_cards.py'],
        ['s1_brand_audit', 'v2', '504c482127b6b7b96cc1f114ba0a927e8f787626b9caaad07e34a7b9326420a2', 'services/content_generation/brand_standards.py'],
        ['s1_flag_fix', 'v2', 'd3ea3ec4b233e245fa5c08e9363cda9f1796b7ff38743d6946e369aa311e0977', 'services/content_generation/flag_fix_node.py'],
        ['s1_generate', 'v2', '01c18efbe2412240412253f7d5e65564bdf7cf7e6f6fac809c610894a5382476', 'services/content_generation/s1_writer_v2.py'],
        ['s1_judge', 'v2', '808a07b8b5d5cd7832d9898341ff1bf6e358b7936df32d0c13a6e212dd194481', 'services/content_generation/brand_fit_v2.py'],
        ['t10_claims', 'v2', 'dcff98166099c0dc9c5c6396e90d9ca1aa8355cb66d6d8bc9a16d57c7ed478c1', 'services/acp_content_writing/claim_rules.py'],
        ['t10_judge', 'v2', '5f7d5704af9bf853524dddb7927e90e780d93aad65c06259244bda3c16b2f75b', 'services/acp_content_writing/quality_gates.py'],
        ['t2_brand_audit', 'v2', '504c482127b6b7b96cc1f114ba0a927e8f787626b9caaad07e34a7b9326420a2', 'services/content_generation/brand_standards.py'],
        ['t2_flag_fix', 'v2', 'd3ea3ec4b233e245fa5c08e9363cda9f1796b7ff38743d6946e369aa311e0977', 'services/content_generation/flag_fix_node.py'],
        ['t2_generate', 'v2', '88df7c8f2f84c3d940fc69e65572ae4c72e206b7fec5b285c6c2ffd322ebf355', 'services/content_generation/prompts.py'],
        ['t2_judge', 'v2', '808a07b8b5d5cd7832d9898341ff1bf6e358b7936df32d0c13a6e212dd194481', 'services/content_generation/brand_fit_v2.py'],
        ['t9_faq_answer_fit', 'v2', '290742f42bafbda199d5976c4270051f2dab6669b9434ba4b4eff73e06cb2146', 'services/acp_content_writing/faq_answer_fit.py'],
        ['t9_repair', 'v2', '346460b3cce89e9cee5df178d1fe26f29e0a9fe526890e54a96dc79fd747b9fd', 'services/acp_content_writing/generate.py'],
        ['t9_write', 'v4', 'ed6b2767697544b46b6c97a1c52d7baa47faaafc972a7c62016dbf5c63ee4ef8', 'services/acp_content_writing/prompts.py'],
        ['writing_memory_eval', 'v3', '75a825a40e3370e4bb72b2bb8750ccba1609788e731cbada3b95ef2d5932851e', 'services/writing_memory/eval_judge.py']
    ];
    i int;
BEGIN
    FOR i IN 1 .. array_length(expected, 1) LOOP
        IF NOT EXISTS (
            SELECT 1 FROM shared.prompt_version
             WHERE stage = expected[i][1] AND version = expected[i][2]
               AND prompt_sha256 = expected[i][3] AND source_path = expected[i][4]
        ) THEN
            RAISE EXCEPTION 'migration 387: % % is registered with another hash or path, or missing (a version is '
                            'never edited: bump the manifest instead)', expected[i][1], expected[i][2];
        END IF;
    END LOOP;
END $$;

-- ── 3. register ──────────────────────────────────────────────────────────────────────────────────────────────────

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('387', now(), 'AA-1047 S272 W1: register prompt versions moved by the writing policy registry (no prompt text)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
