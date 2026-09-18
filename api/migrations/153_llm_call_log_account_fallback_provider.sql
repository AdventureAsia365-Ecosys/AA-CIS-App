-- Migration 153: AA-617 (epic AA-616) — LLM cost observability by ACCOUNT + fallback + provider
--
-- Problem (S187, Kiro — verified against live DB): shared.llm_call_log records `model` (as the
-- LLMResponse.model_used string, e.g. "satellite-haiku-4-5" / "satellite-sonnet-4-6" / "gpt-4.1")
-- but has NO account column. So the admin /admin/llm-usage tree cannot split acc1 vs acc3 spend,
-- and the acc3→acc1 satellite fallback (LLMClient.generate T1.5a→T1.5b, T2.5a→T2.5b) is entirely
-- invisible in the UI even though Cost Explorer shows real acc3 vs acc1 spend. LLMResponse already
-- carries `satellite_account` + `fallback_used` — they were just never threaded into record_call*().
--
-- This migration adds 3 nullable columns (backward-compatible — every pre-existing row stays NULL,
-- no backfill: we cannot reconstruct which account a historical call used, only structured
-- CloudWatch logs had it). New writes populate them (see call_log.py in the same PR).
--
--   account       — "acc1" (867490540162) | "acc2" (005097885195, native) | "acc3"
--                   (786888028788, satellite primary) | NULL for OpenAI / unknown-legacy rows.
--   fallback_used — the LLMResponse.fallback_used flag: TRUE only when a Sonnet-intended call was
--                   downgraded to Haiku, OR the final GPT-4.1 last-resort fired. A satellite
--                   acc3→acc1 hop is NOT a fallback (same model/tier) — it shows via `account`,
--                   not this flag. NULL for legacy rows.
--   provider      — "bedrock-native" (acc2) | "bedrock-satellite" (acc1/acc3) | "openai".
--                   Derived centrally in record_call() from the model string when not passed
--                   explicitly; NULL for legacy rows.
--
-- `model` going forward is stored WITHOUT the "satellite-" prefix (clean "haiku-4-5"/"sonnet-4-6"/
-- "gpt-4.1") so GROUP BY model is clean; the account distinction now lives in its own column. Old
-- rows keep whatever string they were written with — the admin tree tolerates both (COALESCE/like).

BEGIN;

ALTER TABLE shared.llm_call_log
    ADD COLUMN IF NOT EXISTS account       TEXT,
    ADD COLUMN IF NOT EXISTS fallback_used  BOOLEAN,
    ADD COLUMN IF NOT EXISTS provider       TEXT;

COMMENT ON COLUMN shared.llm_call_log.account IS
    'AA-617 — AWS account the LLM call actually ran on: acc1 (867490540162) | acc2 (005097885195, '
    'native) | acc3 (786888028788, satellite primary). NULL for OpenAI or pre-AA-617 rows.';
COMMENT ON COLUMN shared.llm_call_log.fallback_used IS
    'AA-617 — LLMResponse.fallback_used: TRUE only on Sonnet->Haiku downgrade or GPT-4.1 '
    'last-resort. An acc3->acc1 satellite hop is NOT a fallback (see `account`). NULL for legacy.';
COMMENT ON COLUMN shared.llm_call_log.provider IS
    'AA-617 — bedrock-native (acc2) | bedrock-satellite (acc1/acc3) | openai. NULL for legacy.';

-- Cost/usage rollups filter+group by these; a partial index on account keeps the tree query fast
-- once new rows accumulate (NULL legacy rows excluded — they carry no account signal anyway).
CREATE INDEX IF NOT EXISTS idx_llm_call_log_account
    ON shared.llm_call_log (account)
    WHERE account IS NOT NULL;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('153', now(),
    'AA-617: llm_call_log.account/fallback_used/provider columns for per-account LLM cost tracking')
ON CONFLICT (version) DO NOTHING;

COMMIT;
