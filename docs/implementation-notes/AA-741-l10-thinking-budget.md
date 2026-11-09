# AA-741 — S271 lane l10: a thinking budget per stage x model

Finding after t9_write v2 (CIS #1341, word band 900-1,300) went live: a real blog write on 06/11 (~07:26Z, an audit
tenant) still had t9_write on Sonnet 5.5 (anthropic-direct) cut 2 of 3 calls at max_tokens 12000 (one billed
unusable, one truncated, ~0.29 USD lost); the call that answered produced 5,800-9,500 output tokens for an article of
~985 words (~1,300 tokens); the route fell back to Sonnet 4.6. Cause: adaptive thinking is billed as output inside
max_tokens, and the gateway sent Sonnet 5.5 at its family default effort, high. A word band cannot bound thinking, and
the budget retry (12000 -> 24000) of S270 / #1344 would only pay twice.

Two PRs: migration 381 (migration-only, contract) and this code PR, stacked on #1344.

## Decisions
- **No numeric thinking budget.** The brief asked for `thinking.budget_tokens` (2000-4000). The Anthropic API refuses it
  on every Claude 5.x model in the catalog (`budget_tokens` is a 400 on Sonnet / Haiku / Opus 5.5 and Fable 5 / 5.1;
  `{type: "disabled"}` is a 400 on Sonnet 5.5 and Opus 5.5; claude-api reference cached 2026-10-06, the per-model
  thinking table and the Sonnet 5.5 / Haiku 5.5 migration sections). Adaptive thinking is bounded by
  `output_config.effort`. So the stage's thinking budget is the existing `shared.stage_model_params` pair
  **effort + reasoning_mode** (migration 320 has both columns; no schema change needed).
- **effort was already sent** on the Anthropic Messages API from a row (AA-1017). The live fix is therefore data:
  migration 381 seeds effort `medium` for t9_write on sonnet-5-5 and `low` for five Claude
  5.5 validate stages (t9_faq_filter, t9_faq_answer_fit, a3_place_card_check, a3_place_days, a3_place_dedupe_check).
  It works with today's code; this PR does not need it and it does not need this PR.
- **reasoning_mode was never sent anywhere.** This PR sends it on the Anthropic path as the one `thinking` object each
  family takes (`adapters/anthropic_messages.resolve_thinking`, table `_THINKING_FACTS`): 'off' = `between_tools` on
  Sonnet 5.5 (its only off switch), `disabled` on Haiku 5.5 / Opus 5 / Sonnet 5; nothing on Opus 5.5 / Fable (they refuse
  every off form: warned once, "lower the effort instead"); nothing on Opus 4.8 / 4.7 / 4.6, Sonnet 4.6 and the 4.5
  models (already off when the field is left out). 'adaptive' = `{type: "adaptive"}` (needed on 4.6 / 4.7 / 4.8, where
  leaving it out means no thinking); refused on the 4.5 models (manual budget only, which the gateway never sends).
  'off' is never sent at effort xhigh / max (a 400 on the models that take it). No other field inside `thinking`
  (`budget_tokens`, `display`, `block_binding`) is ever sent.
- **OpenAI / Azure:** the row's effort `low` / `medium` / `high` becomes `reasoning_effort`, only on a reasoning-capable
  catalog model (`shared/aa_llm/tokens.reasoning_capable` from #1344: GPT-5.x and later, or `capabilities.reasoning`).
  xhigh / max are not sent (not every GPT model takes them; a 400 would cost the attempt), nor is reasoning_mode.
  `LLMResponse.effort` carries the value sent, so `llm_call_log.effort` is filled for OpenAI rows too.
- **Converse unchanged**: no `additionalModelRequestFields` (AA-803: no probe has shown the provider's field).
- **Only t9_write among the writers (review fix pass).** The first draft also set t2_generate and t8_angle_gen to
  medium. A reviewer pointed out that nothing checks writer quality on a stage_model_params change, and the S271
  cuts were t9_write only (t8 had 7 cuts at 2048 in 30 days, t2 none recorded). So 381 seeds t9_write plus the five
  validate rows; the tour and angle writers stay at high until a paid quality read on an audit tenant (t2_judge
  score and tokens_out next to the high-effort baseline) backs a forward migration. The t9_write paid check reads
  quality too (piece status, attempts, t10_judge rows, subject_gate flags), with a roll-back rule.
- **Judges not seeded.** s1/t2/t7/t10 judges and writing_memory_* run GPT on Converse (nothing would be sent) and a
  judge's effort moves the eval gate (Writing Lab kappa): an eval-gated choice, not this lane's. a3_place_locate (geo
  accuracy) and the Haiku 5.5 fix / nudge / repair writers keep their defaults too.
- **max_tokens unchanged.** Rows leave max_tokens NULL: the call sites keep their ceilings (t9_write 12000 explicit,
  the budget retry at 24000, the #1344 floor). A ceiling is head-room and is not billed unless used; lowering it only
  adds cuts. At medium the expected t9_write reply is the article (~1,300-2,100 tokens) plus a few thousand thinking
  tokens, well under 12000.

## Changed
- `shared/llm_client/adapters/anthropic_messages.py`: `_THINKING_FACTS`, `resolve_thinking`, `build_params(...,
  reasoning_mode=)`, `send(..., reasoning_mode=)`, `MessagesResult.thinking`; module doc.
- `shared/llm_client/stage_params.py`: `reasoning_mode_for`, `openai_reasoning_effort`, `OPENAI_REASONING_EFFORTS`,
  `warn_not_applied(..., applied=)` (names only the fields a path did not send); module doc.
- `shared/llm_client/client.py`: `_call_anthropic_messages` passes the row's reasoning_mode (only when set: an empty
  table sends today's body), logs the thinking type; `_call_openai` sends `reasoning_effort` and sets
  `LLMResponse.effort`.
- `shared/llm_client/models.py`: comment only.
- `CONTEXT.md`: the stage_model_params paragraph (thinking budget) and the ledger note.
- Tests: `tests/unit/test_s271_thinking_control.py` (the family matrix, the wire body on a fake Anthropic API, a fallback
  model never inherits the primary's thinking, the cut-rate model, the ledger keys, OpenAI reasoning_effort).
- Migration PR: `api/migrations/381_stage_thinking_effort_writers.sql`, `tests/integration/test_s271_migration_381.py`,
  `tests/integration/test_aa1017_registry_v2_db.py` (the read is no longer empty after the replay).

## Tradeoffs
- `medium` instead of `low` for the writers: Anthropic's guidance starts chat-like work at low, but these are the
  product's prose writers; medium keeps more planning for the same one-pass article while spending less thinking
  than `high` (the size is measured by the paid check, not known in advance). If the paid check shows
  tokens_out >= 6000 at medium, the next step is a forward migration to `low` for t9_write only (one row).
- Effort is a quality lever. The writer eval gate (release fingerprint) does not cover stage_model_params today: a
  writer effort change does not flag a release for reverify. Follow-up (orchestrator to open): make the writer
  stages' stage_model_params rows part of the writer release fingerprint, before t2_generate / t8_angle_gen move.
- Quality stop rule for the t9_write row: if the first paid blog write at medium is held where the high-effort
  baseline passed, needs more attempts, or opens a subject_gate flag, roll back with a forward migration
  `DELETE FROM shared.stage_model_params WHERE stage = 't9_write' AND updated_by = 'migration-381-aa741'`.
- The family table is code (like `_EFFORT_DEFAULTS`): a new Claude family needs a row there, else 'off' / 'adaptive' is
  warned and not sent (safe: never a 400).

## Should know
- The 381 rows take effect within the stage_params cache TTL (300 s) after Migration Apply, on the code already live.
  Kill switch: `LLM_STAGE_PARAMS=off`.
- Reading the result in the ledger: `shared.llm_call_log.effort` = 'medium' on t9_write rows of model sonnet-5-5;
  `tokens_out`; status ok vs truncated / billed_unusable; `quality_signal ->> 'budget_cut'`.
- A `reasoning_mode` 'off' row for sonnet-5-5 is the lowest setting (no extended thinking) and is the next lever after
  effort low; Anthropic recommends trying adaptive at low first.
- Verified locally: the gateway unit subset (~3.1k tests; the 2 `test_aa804_ledger_v2` failures are order-dependent and
  fail the same on the base branch, they pass alone), the new unit file, flake8 on the touched files; the migration on a
  local Postgres 16 replay (its integration test, 320 / 350 tests, the gateway integration set: the 8 local-only
  failures are the same on main), the CIS migration preflight with the Infra tools (contract, lint ok).
