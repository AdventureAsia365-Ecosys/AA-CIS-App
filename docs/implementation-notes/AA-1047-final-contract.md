# AA-1047 W1 part C — one exit contract in REPORT mode + the W0 review follow-ups

Lane l3-w1-exits of S272. Root ADR 0020 ("Final contract", invariant 4), audit
`docs/audits/2026-11-06-S271-writing-quality-audit/` (root repo) `arch.roadmap[1]`, critic fixes F1, F6, F9, F10, F13,
and the six follow-ups in the AA-1047 Linear comment. Parallel lanes in this repo: l1-w1-policy (policy registry,
rubric, rules, eval) and l2-w1-factsheet (FactSheet, CIS migration 386). No migration here.

## Decisions

- **One pure module, one DB module.** `services/quality/final_contract.py` (checks, verdicts, record, sha, exit
  table) and `services/quality/final_contract_store.py` (the stored-row read `S1_ROW_SQL`, `record_s1_exit`,
  `attach_s1`). The report script and every S1 publish exit read a version through the same `S1_ROW_SQL`.
- **REPORT mode = record, never block.** No status, score, code or text changes because of the contract. Every path is
  best-effort: an error is logged (`final_contract_failed` / `final_contract_record_failed`) and the exit goes on.
- **Verdicts** follow the audit's final graphs: S1 `publish | policy_hold | review | source_hold | degraded`; T2
  `ai_generated | needs_review | policy_hold`; T9 `approved | held | degraded`. A finding is `block` (decides the
  verdict, would hold in enforce mode) or `warn` (recorded). The record carries `actual` (what today's gates decided),
  `agrees` and `flip` (`unpublish` = today's gates let it out, the contract would not; `publish` = the reverse).
- **S1 checks** = today's publish gate (surviving hard codes, brand audit manual_check / flagged-unfixed, the elephant
  policy) re-read on the stored text, plus: source-vs-output day structure (`itinerary_day_gap` + every day-oracle code
  that counts or orders days, all `block` here although the oracle itself is still shadow), an empty source itinerary
  and a source day contradiction (`source_hold`), grounding not run / not run on a reviewer's edit / unsupported
  sentences, forbidden words re-scanned on the stored text, hard degraded entries. The quality score, the meta band
  (D5: 140–155) and the title band are `warn` (ADR 0020: the score is a report column, meta never blocks).
- **Where it is recorded (admin-only):** S1 `generated_content.metadata.final_contract` + `metadata.final_contract_log`
  (compact entry per exit, last 20); T2 `tenant_tour_versions.judge_breakdown.final_contract`; T9
  `content_piece.writer_input.final_contract`. Tenant reads never return these columns (judge_breakdown and
  writer_input are already admin-only).
- **Exits (the `EXITS` table):** S1 graph `final_contract_node` (done, hitl, resumed, resumed_hitl, batch,
  batch_hitl, revalidation), the version INSERT (`_execute_run_tour`), `_revalidate_tour`, a reviewer's text edit
  (`update_generated_content_fields`, exit `human_edit`), every Master publish (`process_export` with a required-by-test
  `contract_exit`: `auto_publish`, `approve`, `promote`, `lambda`), the review approve (also its legacy Step Functions
  branch); T2 the job save and the policy-hold save, the admin sentence apply, the tenant's own edit / approve; T9 the
  write (`run_write_background`), the admin sentence apply, the tenant's hand edit.
- **Tenant edits are log-only.** A tenant's own text is tenant-owned (BYO); critic fix 6 leaves its notice, status
  re-derivation and storage to a later decision, so the contract runs and writes one `final_contract` log event
  (`tenant_owned=true`) and stores nothing.
- **S1 normalizer moved into the graph.** `v1_pipeline._normalize_generated` / `clean_itinerary` ran AFTER the graph:
  a post-gate edit. They live in `services/content_generation/normalize.py` and run as the first step of
  `final_contract_node` (not on a re-validation, which stores no text); `_rewrite_tour`'s call is now a no-op on A1
  text (idempotent) and stays for T2. The INSERT compares the stored text's sha with the gated one (`sha_match`).
- **T9 normalizer fold.** Every post-loop text edit (stock-line cut, authenticity cut, citation-tag strip, FAQ answer
  fit, final integrity) is one ordered function, `_normalize_final_text`; the contract runs on its output with the
  final status (`_t9_final_contract`), and `_finalize_piece` compares the stored text's sha with the contract's
  `text_sha` at the one write (`stamp_stored`).
- **Policy values through one interface.** `final_contract.policy_value()` (meta band, title max, FAQ counts, CTA
  count) holds the decision-page values. **l1 merge note:** point `policy_value` at the policy registry
  (`writing_policy.yaml`) instead of `_W1_DEFAULTS`; no check changes.
- **Elephant code split (follow-up 6).** The detector now emits `POLICY_ELEPHANT_RIDING` (in
  `brand_audit_node._MANUAL_CHECK_CODES`, taxonomy `a1_codes`, the review label and the review-model product-truth set);
  `FACT_CHECK_MANUAL_CHECK` stays the LLM audit's own code. `is_policy_only_hold` keeps a hold when any other manual-check
  reason is present; the LLM's `FACT_CHECK_MANUAL_CHECK` next to the policy code counts as the policy only when every
  audit issue mentions the elephant (the audit prompt restates the rule). A stored version from before the split
  (`FACT_CHECK_MANUAL_CHECK` alone) follows the same issue rule. The approve gate SQL reads
  `quality_scores.brand_audit_issues`.
- **F9 claim span (follow-up 3).** `f9_policy.claim_span` picks the clause of the quoted sentence that holds the
  judge's claim (most shared words), with the sentence's trip subject in front when the clause continues its verb
  phrase ("you visit the market and taste the tea"). A claim with no word in common with the sentence (paraphrase,
  placeholder) falls back to the whole sentence (fail safe, the pre-W1 behaviour).
- **/stats (follow-up 1).** The F9_cta_fact gate rows count in `failed` / `blocking_failed` only the entries with a
  content class; `failed_judge_unavailable` / `failed_text_integrity` count the rest; `f9_classes` sums the classes per
  channel (stored `f9_policy.classes`, else parsed).

## Changed

- `services/content_generation/graph.py`: `final_contract` node is the only edge to END of all five graphs;
  `ContentState.final_contract`; **bug fix** `build_graph_from_generated` had no edge out of the retry `generate` node,
  so LangGraph ended a retried Bedrock-batch run right after the writer (no validate, judge, grounding, day guard) —
  found by the topology test.
- `api/routers/admin_pipeline.py`, `api/routers/v1_pipeline.py`, `services/export/handler.py`,
  `services/jobs/t2_rewrite_job.py`, `services/acp_content_writing/service.py`, `api/routers/admin_content_quality.py`,
  `api/routers/v1_tours.py`: the exits above.
- Follow-ups: `f9_policy.py` (claim span), `gate_eval.ledger_snapshot(..., lexicon)` (service passes `hyg.lexicon`),
  `writing_memory/replay.stored_failures` skips `repair_mode in ('stop_and_label', 'label')`,
  `scripts/quality/replay_aa900_w0.py` maps writer_input `seed` / `trip`, `/stats` split, the elephant code split.
- New: `scripts/quality/final_contract_report.py` (read-only report), `tests/unit/test_aa1047_final_contract.py`,
  `tests/unit/test_aa1047_final_contract_topology.py`, `tests/unit/test_aa1047_w0_followups.py`,
  `tests/integration/test_aa1047_final_contract_store.py`; chain-order and a few existing tests updated for the
  intended changes (process_export's `contract_exit`, the new elephant code, the normalizer).

- Resume after the S272 machine crash: `scripts/quality/final_contract_report.py` is force-added (`scripts/` is in
  `.gitignore`; the other tracked scripts were added the same way), the store integration test uses a listed country
  (`raw_tours.country` is constrained), the AA-986 approve-race export stub forwards `contract_exit`, and the AA-739
  review-block test follows the elephant label split.

## Tradeoffs

- The publish record is written after the publish (last step of `process_export`): a contract read can never delay
  or fail a publish, and it records what was actually published. A refused publish records nothing (the refusal is
  logged by its own path).
- The day-oracle codes are `block` in the contract while the oracle is still shadow in validate: the report must show
  what enforcing them would move (that is the W1 proof); precision is read on the flip list before any W3 enforce.
- The sha "assert" is a recorded `sha_match` + an error log, not an exception: report mode never fails a write. The
  hard guard is the chain-order AST test (nothing edits the text after the normalizer) and the T9 / S1 sha tests.
- Tenant edits stay log-only rather than a stored record until the human-edit decision (critic fix 6).

## Should know

- **Report live step:** `scripts/quality/final_contract_report.py` runs in the api container (S3-mediated ECS exec,
  container `DATABASE_URL`, read-only transaction). Output = aggregates + ids, no page or source text. The audit's
  named cases (a 12-day page cut to 4 days, a 15-day page cut to 10) appear under `day_loss` / `flips` when they are
  still live; a fix since S252 may have removed them, which the report then shows as absent.
- `process_export` now takes `contract_exit=`: any test double that replaces it must accept keyword arguments
  (the AA-986 approve-race stub broke on it and was only caught by its integration test).
- `EXITS` is the single list of exits; `tests/unit/test_aa1047_final_contract_topology.py` scans `api/` and `services/`
  for any function that writes stored AI text or publishes, and fails on one not listed (or listed in `NOT_EXITS` with
  a reason).
- The legacy validation handler (`services/validation/handler.py`) can set a version `approved` and is not wired; it
  has no live caller (listed in `NOT_EXITS`).
- l1 / l2 merge: this lane touches `services/quality/taxonomy.yaml` (one `a1_codes` row) and
  `services/acp_content_writing/f9_policy.py` (claim span); if l1 regenerates either from the policy registry, carry
  `POLICY_ELEPHANT_RIDING` and `claim_span` over.
