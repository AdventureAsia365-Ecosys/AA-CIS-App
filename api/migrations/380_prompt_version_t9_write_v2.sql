-- Migration 380: AA-741 (S271 phase-1 finding, T9 blog output tokens) — register prompt version t9_write v2 in
-- shared.prompt_version, so the ledger can tell the new blog writer prompt from v1.
--
-- Why. The S271 audit (4 audit tenants, 06/11/2026) saw t9_write for a blog average 1,846 output tokens but reach 11,465,
-- and 4 calls hit the 12,000 ceiling (3 cut, 1 billed but unusable). The code PR that follows gives the blog prompt a
-- word band, a section cap and a one-pass rule (services/acp_content_writing/prompts.py, _BLOG_LENGTH_INSTRUCTIONS) and
-- bumps the prompt manifest (shared/llm_client/prompt_versions.json) to t9_write v2. The gateway writes
-- llm_call_log.prompt_version only when the deployed source, the manifest and a row of this table agree
-- (shared/llm_client/prompt_registry.version_for); without this row every t9_write call after the code deploy would
-- carry prompt_version NULL and the before / after read (cost, tokens, quality per version) would be lost.
--
-- What.
--   1. One row: ('t9_write', 'v2', sha256 of the source text of SYSTEM_PROMPT, build_user_prompt, _BLOG_WORDS,
--      _BLOG_WORDS_MAX, _BLOG_SECTIONS_MAX and _BLOG_LENGTH_INSTRUCTIONS in services/acp_content_writing/prompts.py
--      (never the text), created_by 'migration-380'), the output of
--      `python3 scripts/prompt_registry_check.py --sql --unregistered --created-by migration-380`.
--      ON CONFLICT (stage, version) DO NOTHING.
--   2. Guard: the row exists with exactly this hash and path (a v2 registered earlier with another hash would be kept
--      silently by the ON CONFLICT and every v2 call would carry prompt_version NULL).
--   3. Register 380.
--
-- Behaviour. Until the code PR deploys, no deployed prompt matches v2 and nothing changes (t9_write calls keep 'v1').
-- After it deploys, within the registry cache TTL, new t9_write rows carry 'v2'. No route, model, cost or prompt
-- change by this file. v1 stays registered (history).
--
-- Class: contract (root ADR 0017 d3: rows into an existing table), so no "-- migrate:" header and MIG-4 order:
-- (1) this migration-only PR merges to AA-CIS-App main; (2) the byte copy in AA-CIS-Infra db/migrations/cis/ merges;
-- (3) Infra Migration Apply applies it once every job table is idle (root ADR 0017a); (4) then the code PR (prompt +
-- manifest v2) merges. No readiness bump. DM-7.1: no new table.
--
-- Rollback (a NEW forward migration): DELETE FROM shared.prompt_version WHERE created_by = 'migration-380';
-- (t9_write rows then carry prompt_version NULL under v2 code; rows already written keep 'v2').
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
        RAISE EXCEPTION 'migration 380: shared.prompt_version is missing (apply 321 first)';
    END IF;
END $$;

-- ── 1. the manifest row ──────────────────────────────────────────────────────────────────────────────────────────

INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) VALUES
    ('t9_write', 'v2', 'dde697990965b95f3fb18cd22d83bc68b62294800e88f39aed90dbea6958792b', 'services/acp_content_writing/prompts.py', 'migration-380')
ON CONFLICT (stage, version) DO NOTHING;

-- ── 2. guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM shared.prompt_version
         WHERE stage = 't9_write' AND version = 'v2'
           AND prompt_sha256 = 'dde697990965b95f3fb18cd22d83bc68b62294800e88f39aed90dbea6958792b'
           AND source_path = 'services/acp_content_writing/prompts.py'
    ) THEN
        RAISE EXCEPTION 'migration 380: t9_write v2 is registered with another hash or path, or missing (a version is '
                        'never edited: bump the manifest instead)';
    END IF;
END $$;

-- ── 3. register ──────────────────────────────────────────────────────────────────────────────────────────────────

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('380', now(), 'AA-741 S271: register prompt version t9_write v2 (blog length and one-pass budget, no prompt text)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
