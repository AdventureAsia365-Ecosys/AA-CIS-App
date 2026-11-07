-- Migration 387: AA-1047 (S272 Quality W1, root ADR 0020 "policy registry") — register the prompt versions the
-- writing policy registry moves, in shared.prompt_version, so the ledger can tell them from the old ones.
--
-- Why. AA-1047 compiles the writing rules from services/quality/writing_policy.yaml (policy_sha stamped on every
-- artefact). A prompt whose source reads the policy (`_wp.fill` / `_wp.fragment`) now hashes the compiled policy_sha
-- too (shared/llm_client/prompt_registry.prompt_hash_of), so these six stages get a new version:
--   t9_write v4             the background line (D1) and the CTA lines (the literal line, word for word): critic fix
--                           F9, the contradiction-resolving lines; the rest renders byte-equal
--   t9_repair v2            the background line of the sentence repair (D1); the SEO fit and FAQ prompts render
--                           byte-equal (bands and pair count from the policy)
--   writing_memory_eval v3  the R2 judge names the policy (version + sha) and reads the policy's label rubric; the
--                           16000/9000 clipping is gone (evidence_view: an item over the cap is excluded, never cut)
--   s1_brand_audit v2, t2_brand_audit v2   the SEO bands rendered from the policy (byte-equal text)
--   a3_place_card v2        the place-card facts scope rendered from the policy (byte-equal text)
-- The gateway writes llm_call_log.prompt_version only when the deployed source, the manifest
-- (shared/llm_client/prompt_versions.json) and a row of this table agree (prompt_registry.version_for); without these
-- rows every call of these stages after the code deploy would carry prompt_version NULL.
--
-- What.
--   1. Six rows (sha256 of the hashed symbols' source text plus the policy_sha, never the text; created_by
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
    ('a3_place_card', 'v2', '0309d5ac9b0fd0479fd17c48f0a853f4159ab952da2f6ae3032ddee9c3ed9900', 'services/acp_shared/place_cards.py', 'migration-387'),
    ('s1_brand_audit', 'v2', 'd494df4d4ea490cb24337ed7e72d6ffd2e4241d5a3a8f85724a340693bde439c', 'services/content_generation/brand_standards.py', 'migration-387'),
    ('t2_brand_audit', 'v2', 'd494df4d4ea490cb24337ed7e72d6ffd2e4241d5a3a8f85724a340693bde439c', 'services/content_generation/brand_standards.py', 'migration-387'),
    ('t9_repair', 'v2', '0e213fd78d37a119837083b0a38c06007be5598bcaf5120051ef783f6d51d5c0', 'services/acp_content_writing/generate.py', 'migration-387'),
    ('t9_write', 'v4', 'bd13593e1261dc9e590e909e974334abcedfe724213cf9f59c38b092336081dd', 'services/acp_content_writing/prompts.py', 'migration-387'),
    ('writing_memory_eval', 'v3', '311e443af3224a47ec4490e0d3ab3e6298688087d175389d0302447c886cc8a0', 'services/writing_memory/eval_judge.py', 'migration-387')
ON CONFLICT (stage, version) DO NOTHING;

-- ── 2. guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

DO $$
DECLARE
    expected CONSTANT text[][] := ARRAY[
        ['a3_place_card', 'v2', '0309d5ac9b0fd0479fd17c48f0a853f4159ab952da2f6ae3032ddee9c3ed9900', 'services/acp_shared/place_cards.py'],
        ['s1_brand_audit', 'v2', 'd494df4d4ea490cb24337ed7e72d6ffd2e4241d5a3a8f85724a340693bde439c', 'services/content_generation/brand_standards.py'],
        ['t2_brand_audit', 'v2', 'd494df4d4ea490cb24337ed7e72d6ffd2e4241d5a3a8f85724a340693bde439c', 'services/content_generation/brand_standards.py'],
        ['t9_repair', 'v2', '0e213fd78d37a119837083b0a38c06007be5598bcaf5120051ef783f6d51d5c0', 'services/acp_content_writing/generate.py'],
        ['t9_write', 'v4', 'bd13593e1261dc9e590e909e974334abcedfe724213cf9f59c38b092336081dd', 'services/acp_content_writing/prompts.py'],
        ['writing_memory_eval', 'v3', '311e443af3224a47ec4490e0d3ab3e6298688087d175389d0302447c886cc8a0', 'services/writing_memory/eval_judge.py']
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
