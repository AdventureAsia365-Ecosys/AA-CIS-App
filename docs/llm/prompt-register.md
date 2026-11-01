---
id: APP-06-prompts
title: LLM prompt register
description: Every prompt the CIS Model Gateway sends - prompt id, stage, role, file and symbol, how its version is recorded and the eval baseline it is measured against.
type: reference
status: draft
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-cis-api, aa-cis-worker]
last_reviewed: 2026-10-31
review_every_days: 90
related: [APP-06-eval, APP-06-routes, CAT-01, AA-900, AA-729, AA-804]
---

# LLM prompt register

This page maps each prompt to the code that builds it, the Model Gateway stage that sends it, how its version is
recorded and the evidence that measures it. The model behind a stage is not here: routes and models are runtime data
in `shared.llm_role_config` and `shared.llm_model_catalog`, published as the generated route table in the root repo
(`docs/generated/llm-routes.md`, catalog ID `APP-06-routes`).

Change a prompt, a model or a route only with an eval run ID in the PR. The procedure and the cost cap are in the
[eval runbook](eval-runbook.md).

## How to read the register

- **Prompt id**: stable key of the prompt, `<stage>` or `<stage>.<part>`. Use it in PRs, eval notes and the
  `prompt_version` column when AA-804 ships it.
- **Stage**: the `stage` the call carries through `LLMClient.generate()`, the key of `shared.llm_role_config`.
  A `t2_*` stage is the tenant twin of its `s1_*` stage (`services/content_generation/stages.py` `stage_for`); it
  sends the same prompt unless the row says otherwise.
- **File and symbol**: the module and the constant or function that holds the prompt text. The guard test
  `tests/unit/test_aa900_prompt_register.py` fails when a file or a symbol named here no longer exists.
- **Version**: where the prompt's version is recorded today. `sha8 of X` means `prompt_version_of(X)`
  (`services/content_generation/batch_prompt.py`, sha256 of the system prompt, 8 hex). `git` means no recorded
  version: the file's git history is the version until AA-804 adds `prompt_version` to the ledger.
- **Eval baseline**: what a change is measured against.
  - `R2 <writer>` = an R2 run on the AA-729 golden set (`shared.eval_golden_set` name `aa729`, active version
    1.2.0); its `baseline_import` run (frozen fixture outputs, release 0) is the control. See the
    [eval runbook](eval-runbook.md).
  - `Q0` / `Q2` / `R0` / `Q3` = the quality-system tier for the file in `services/quality/paths.yaml`
    (Q0 cases in CI, Q2 judge re-score before merge, R0 replay on stored output, Q3 writer train).
  - `none` = no eval exists for this prompt yet: a change states its manual check in the PR.

## Version register and CI gate

Every stage in the table has a numbered version in `shared/llm_client/prompt_versions.json` (newest last): the file,
the symbols above and `prompt_sha256`, the sha256 of those symbols' source text (read with `ast`, so other code in the
file does not count). The gate `tests/unit/test_aa900_prompt_registry_gate.py` fails when a stage's newest version no
longer matches its source. To change a prompt:

1. `python3 scripts/prompt_registry_check.py --bump` appends the next version for each changed stage.
2. A CIS migration registers the new rows in `shared.prompt_version`
   (`python3 scripts/prompt_registry_check.py --sql --unregistered --created-by migration-NNN` prints the INSERT of the
   rows no migration inserts yet; the table stores no prompt text). The gate fails until such a file exists
   (migration 325 registered the first 32 versions).
3. After the migration, `llm_call_log.prompt_version` carries the version for every call of that stage (the gateway
   writes it only when the deployed source and the registered hash agree). `llm_call_log.prompt_hash` is the sha256 of
   the system prompt the call sent.

## Register

<!-- register:start -->
| Prompt id | Stage | Role | File · symbol | Version | Eval baseline |
|---|---|---|---|---|---|
| `s1_generate.v2` | `s1_generate` | writer | `services/content_generation/s1_writer_v2.py` · `w_system_prompt`, `w_user_prompt`, `VERSION` | `VERSION` constant, sha8 of the final system prompt in `generation_meta.prompt_version` | R2 `a1` |
| `s1_generate.v1` | `s1_generate` | writer | `services/content_generation/batch_prompt.py` · `build_s1_system_prompt` | sha8 of the system prompt (`graph.py` `prompt_version`) | R2 `a1`; Q0, Q3 |
| `s1_generate.base` | `s1_generate` | writer | `services/content_generation/prompts.py` · `SYSTEM_PROMPT`, `build_rewrite_prompt` | sha8 (through `build_s1_system_prompt`) | R2 `a1`; Q0, Q3 |
| `t2_generate` | `t2_generate` | writer | `services/content_generation/prompts.py` · `T2_SYSTEM_PROMPT`, `build_t2_base_prompt` | sha8 of the system prompt in `generation_meta.prompt_version` | R2 `t2`; Q0, Q3 |
| `s1_judge.v2` | `s1_judge`, `t2_judge` | judge | `services/content_generation/brand_fit_v2.py` · `SYSTEM_V2`, `build_user_prompt` | git | Q2 (A1, T2, T7) |
| `s1_judge.v1` | `s1_judge`, `t2_judge` | judge | `services/content_generation/brand_fit.py` · `JUDGE_SYSTEM`, `_build_judge_prompt` | git | Q2 (A1, T2, T7) |
| `t7_debate_judge` | `t7_debate_judge` | judge | `services/content_generation/brand_fit.py` · `CANDIDATE_JUDGE_SYSTEM`, `_build_candidate_prompt` | git | Q2 (A1, T2, T7) |
| `s1_brand_audit` | `s1_brand_audit`, `t2_brand_audit` | judge | `services/content_generation/brand_standards.py` · `AA_BRAND_IDENTITY_PROMPT`, `AA_COWORK_STRUCTURE_PROMPT` | git | Q2 (A1, T2, T7) |
| `s1_flag_fix` | `s1_flag_fix`, `t2_flag_fix` | writer | `services/content_generation/flag_fix_node.py` · `FIX_SYSTEM`, `fix_system_prompt` | git | Q0, R0, Q3 (repair replay) |
| `s1_flag_fix.grounding` | `s1_flag_fix`, `t2_flag_fix` | writer | `services/content_generation/grounding.py` · `REPAIR_SYSTEM`, `_repair_prompt` | git | Q0, R0 |
| `s1_itinerary_nudge` | `s1_itinerary_nudge` | writer | `services/content_generation/itinerary_utils.py` · `_ITINERARY_NUDGE_SYSTEM_PROMPT` | git | Q0, R0 (day oracle) |
| `t2_itinerary_nudge` | `t2_itinerary_nudge` | writer | `services/content_generation/itinerary_utils.py` · `_T2_ITINERARY_NUDGE_SYSTEM_PROMPT` | git | Q0, R0 (day oracle) |
| `s1_source_facts` | `s1_source_facts` | validate | `services/content_generation/source_facts.py` · `_SYSTEM_PROMPT`, `_extract_prompt` | git | Q0 |
| `a0_column_map` | `a0_column_map` | writer | `shared/llm_client/column_mapper.py` · `detect_column_mapping` (inline system prompt) | git | none |
| `a3_atomize` | `a3_atomize` | validate | `services/acp_shared/atom_extraction.py` · `SYSTEM_PROMPT`, `build_user_prompt`, `build_day_user_prompt` | git | none |
| `a3_place_card` | `a3_place_card` | validate | `services/acp_shared/place_cards.py` · `_CARD_SYSTEM`, `PROMPT_SHA` | `PROMPT_SHA` (sha16 of both card prompts), stored per card as `prompt_sha` | none |
| `a3_place_card_check` | `a3_place_card_check` | validate | `services/acp_shared/place_cards.py` · `_CHECK_SYSTEM`, `PROMPT_SHA` | `PROMPT_SHA` | none |
| `a3_place_days` | `a3_place_days` | validate | `services/places/days.py` · `SYSTEM_PROMPT`, `build_user_prompt` | git | none |
| `a3_place_locate` | `a3_place_locate` | validate | `services/places/locate.py` · `SYSTEM_PROMPT`, `build_prompt` | git | none |
| `a3_place_dedupe` | `a3_place_dedupe`, `a3_place_dedupe_check` | validate | `services/places/dedupe.py` · `SYSTEM_PROMPT`, `PROMPT_VERSION` | `PROMPT_VERSION` constant (with `RULE_VERSION`) | none |
| `a3_search_demand` | `a3_search_demand` | validate | `services/acp_contract/segment_research.py` · `SYSTEM_PROMPT`, `USER_PROMPT` | git | none |
| `segment_research` | `segment_research` | writer | `services/acp_contract/segment_research.py` · `SYSTEM_PROMPT`, `USER_PROMPT` | git | none |
| `segment_research_batch` | `segment_research_batch` | writer | `services/acp_contract/segment_research_batch.py` · `PROPOSE_SYSTEM` | git | none |
| `t8_angle_gen` | `t8_angle_gen` | writer | `services/acp_angle_gate/prompts.py` · `SYSTEM_PROMPT`, `SYSTEM_PROMPT_R8`, `system_prompt`, `build_user_prompt` | git | none |
| `t9_write` | `t9_write` | writer | `services/acp_content_writing/prompts.py` · `SYSTEM_PROMPT`, `build_user_prompt` | sha8 in `generation_meta.writer_prompt_version` (`service.py` `WRITER_PROMPT_VERSION`) | R2 `t9_blog`, R2 `t9_social` (first draft only); Q0, Q3 |
| `t9_repair` | `t9_repair` | writer | `services/acp_content_writing/generate.py` · `_REPAIR_SYSTEM` | git | Q0, R0, Q3 (T9 repair replay) |
| `t9_repair.seo_fit` | `t9_repair` | writer | `services/acp_content_writing/generate.py` · `_SEO_FIT_SYSTEM` | git | Q0 |
| `t9_repair.faq` | `t9_repair` | writer | `services/acp_content_writing/generate.py` · `_FAQ_SYSTEM` | git | Q0 |
| `t9_faq_filter` | `t9_faq_filter` | validate | `services/acp_content_writing/faq_filter.py` · `SYSTEM_PROMPT`, `build_user_prompt` | git | Q0 (selector keep/drop set) |
| `t9_faq_answer_fit` | `t9_faq_answer_fit` | validate | `services/acp_content_writing/faq_answer_fit.py` · `SYSTEM_PROMPT`, `ISOLATED_SYSTEM_PROMPT`, `build_user_prompt` | git | Q0 |
| `t10_judge.f9` | `t10_judge` | judge | `services/acp_content_writing/quality_gates.py` · `_JUDGE_SYSTEM_PROMPT`, `F9_PROMPT_VERSION` | `F9_PROMPT_VERSION` constant, in `quality_signal.prompt_version` | Q2 (when F9 changes), R0 |
| `t10_claims` | `t10_claims` | validate | `services/acp_content_writing/claim_rules.py` · `EXTRACTION_SYSTEM` | git | Q0, R0 |
| `t10_adjudicate` | `t10_adjudicate` | judge | `services/acp_content_writing/seed_align/adjudicate.py` · `SYSTEM_PROMPT`, `SCHEMA`, `build_user_prompt` | git | AA-789 eval set via `scripts/aa1000_adjudicate_calibrate.py` (not run yet; $5 hard cap) |
| `supply_content_writer` | `supply_content_writer` | writer | `services/supply_content/writer.py` · `SYSTEM_PROMPT`, `user_prompt` | git | none |
| `writing_memory_mine` | `writing_memory_mine` | judge | `services/writing_memory/drafter.py` · `SYSTEM_PROMPT`, `build_user_prompt` | git | none (rules go through L0 and R2 before release) |
| `writing_memory_eval` | `writing_memory_eval` | judge | `services/writing_memory/eval_judge.py` · `system_prompt`, `build_user_prompt`, `judge_version` | `judge_version()` (sha12), in `prompt_eval_runs.prompt_version` and each result's `judge_version` | Calibration against the golden labels in every R2 run (Cohen's kappa >= 0.6) |
<!-- register:end -->

Not in the register:

- Embedding stages (`f10_embed`, `a3_place_embed`): no prompt.
- `s1_atom_writer`: retired stage (the S1-from-atom pipeline was removed, AA-611); migration 336 switches its route
  row off (history kept).
- Jev questions (TypeSafe `decide()`, for example `a1_promise_unsupported`): questions, not gateway prompts. They are
  listed on the admin Jev Decisions page and in `shared.decision_log`.
- TripPlanner stages (`tp_*`): their prompts live in AA-TripPlanner-Web. Their routes appear in the generated route
  table because the table is shared.

## Change a prompt

1. Edit the prompt in the file named above. If the row has a version constant, bump it in the same commit, and bump
   the registered version (`scripts/prompt_registry_check.py --bump`, see "Version register and CI gate").
2. Update the row here when the file, symbol, stage or version rule moves. The guard test fails on a stale file or
   symbol.
3. Follow the [eval runbook](eval-runbook.md): R2 for the four writers, the quality-system tier otherwise.
4. Put the eval run ID and its cost in the PR (section "LLM change" of the PR template).
