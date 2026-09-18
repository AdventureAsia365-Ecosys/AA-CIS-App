-- Migration 157: AA-622 follow-up — backfill account/provider for pre-AA-617 llm_call_log rows
--
-- Problem (S188, Kiro — verified against live Dev DB): every one of the 2676 existing
-- shared.llm_call_log rows was written BEFORE migration 153 (AA-617) added the account/provider
-- columns, so all of them have account=NULL, provider=NULL. The External Spend admin page
-- (frontend/app/admin/llm-usage/page.tsx) COALESCEs NULL account -> 'unknown' and the FE labelled
-- 'unknown' as "OpenAI / legacy" — so EVERY legacy row (incl. real Bedrock satellite atomize/S1
-- writer spend) was mislabelled "OpenAI". Only gpt-4.1 (s1_judge) is truly OpenAI.
--
-- Ground truth of the 3 legacy model strings actually present (verified query, Dev DB, 2026-09-18):
--   satellite-haiku-4-5  n=1324  $1.5462  -> S1 writer, Bedrock satellite, primary account acc3
--   sonnet-4-6           n=998   $19.2226 -> t5_atomize, ran on acc3 satellite (model stored bare,
--                                            no "satellite-" prefix, but it WAS a satellite call;
--                                            acc3 sonnet atomize = 82% of acc3 spend per S187)
--   gpt-4.1              n=354   $1.3878  -> OpenAI (no AWS account)
--
-- NOTE: we do NOT reuse _normalize_model_provider()'s string heuristic here, because it would
-- misclassify the bare "sonnet-4-6" as bedrock-native (acc2). This backfill uses the verified
-- historical mapping instead. It is guarded on `provider IS NULL` so it only ever touches legacy
-- rows and never overwrites correctly-instrumented rows written after AA-617.
--
--   account : satellite models -> 'acc3' (primary); gpt-4.1 -> NULL (OpenAI has no AWS account).
--   provider: satellite models -> 'bedrock-satellite'; gpt-4.1 -> 'openai'.

BEGIN;

-- Bedrock satellite writer (S1 attempt-1/flag_fix/nudge), acc3 primary
UPDATE shared.llm_call_log
   SET provider = 'bedrock-satellite', account = 'acc3'
 WHERE provider IS NULL
   AND model = 'satellite-haiku-4-5';

-- t5_atomize on acc3 satellite (bare model string, but satellite)
UPDATE shared.llm_call_log
   SET provider = 'bedrock-satellite', account = 'acc3'
 WHERE provider IS NULL
   AND model = 'sonnet-4-6';

-- OpenAI judge — provider only; no AWS account
UPDATE shared.llm_call_log
   SET provider = 'openai'
 WHERE provider IS NULL
   AND model = 'gpt-4.1';

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('157', now(),
    'AA-622: backfill account/provider on pre-AA-617 llm_call_log rows (satellite->acc3, gpt-4.1->openai)')
ON CONFLICT (version) DO NOTHING;

COMMIT;
