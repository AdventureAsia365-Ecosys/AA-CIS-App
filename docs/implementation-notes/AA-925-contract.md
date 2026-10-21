# AA-925 PR 3 — repair contract, AA-740 fitter fix, `degraded[]` + hold, harness INVALID, Jev drill

## Decisions

- **Pure modules, injected gates.** `services/quality/repair_contract.py` and `services/quality/degraded.py`
  import no writer module at load time (a test walks their top-level imports). Gate runners reach the contract
  as callables (`gate_runner(field, text)`); `new_specifics` moved here from `sentence_repair`, which
  re-exports it (its `trip_facts` import stays lazy).
- **Five invariants from the issue** (gates kept, no new specifics, no truncation, scope kept, tags balanced).
  The design's sixth, "no blanding" (fact coverage may not drop), needs the Q-LEARN coverage measure — left to
  that issue.
- **Truncation is relative**: a field is charged only when it ended as a complete sentence before the repair
  (highlights without a period are not). The **cut signature** catches what `meta_complete_sentence` accepts
  on purpose (AA-794): the repaired text minus its final period is a strict word-prefix of the text before,
  cut where the original sentence did not end ("…a 20-kilometre." from "…a 20-kilometre walk…"). A
  sentence-boundary salvage is not a signature.
- **Names are checked only where a repair should only remove**: the A1 grounding fact editor and the T3
  sentence repair. flag_fix rewrites whole fields for brand reasons and the writer policy allows background
  about a named place, so flag_fix / the itinerary nudge / the meta regenerate check numbers only
  (`names=False`). A name check there would revert legitimate brand repairs (review-rate risk).
- **Splice points (existing only)**: flag_fix's two `_revert_introduced_forbidden` calls are replaced by
  `_contract_revert` (gate runner = the same forbidden scan, so AA-641 behaviour is kept; the Q0 pin
  `a1-flagfix-reintroduces-forbidden` stays green); seo_meta is checked AFTER its band guard, so the final
  value is what the contract sees. `grounding.repair_and_recheck` (covers `_apply_grounding_recheck`'s
  post-validate repair), `revalidate_node` after strip + fit (`_contract_after_edit`), T3
  `_t3_sentence_repair` and the meta regenerate, T9 `splice_checked` (`enforce_spans`: truncation + balance
  per replaced sentence; specifics + integrity were already there). Not spliced: T9 `_cut_blog_truth_items` /
  `_cut_truth_items` (deletions only — they already refuse a cut under the minimum or one that fails
  F16/F0) and `_enforce_faq_contract` (it regenerates the FAQ block; its own F16 gate re-runs) — recorded
  here instead of adding a second check of the same thing.
- **The AA-528/AA-780 guard stays in `run_write_background`.** Moving it would split the 620-line function
  (design §11.1: separate work); it already enforces invariant 1 for T9 full rewrites.
- **AA-740 fitter**: `_cut_long_meta` (clause and word cut, then "."), `_META_CLAUSE_CUTS`,
  `_META_CUT_BEFORE` and `_next_word` are removed. Kept: `_salvage_to_band` (sentence boundary), the
  facts-only `_extend_short_meta`, and the "in band but not closed, nothing cut → add a period" branch
  (`meta_in_band` still rejects weak endings). `SEO_META_TOO_LONG` stays in `_DETERMINISTIC_SEO_CODES`, so
  flag_fix's LLM meta repair + `_rerepair_meta` take it; `HARD_CODE_REPAIR` says "llm: …".
- **T3 meta-only regenerate** (`_t3_meta_regenerate`): when a meta code is in T3's structural list after the
  fit, one bounded `_rerepair_meta` loop on `t2_flag_fix` (AA-763 route) runs before a whole-tour rewrite,
  once per distinct meta, under the contract (numbers only). Without it AA-794's warning applies: a length
  code costs a whole T2 rewrite (~$0.38).
- **degraded severities** (`REGISTRY`): `jev_unavailable` hard on enforce / unknown mode (`off` + error,
  missing verdict), count on shadow; `grounding_not_run` hard (node crash, `regrounding_failed`; never on a
  tenant rewrite or the revalidation graph, which has no grounding node); `grounding_no_source` block;
  `f9_judge_unavailable` hard; `day_check_skipped` block; `judge_unavailable` block; `brand_audit_unavailable`
  count; `repair_contract` soft. A detector that re-runs on the latest content (validate's day oracle, the
  grounding re-checks) REPLACES its entries (`replace_detector`) — what holds is what is true of the final
  text, not the sum of every check.
- **One A1 hold**: `apply_a1_hold` wraps the last step (`_apply_grounding_recheck` → `_grounding_recheck`)
  and adds `DEGRADED_DETECTOR` + `manual_check`. In `_HARD_BLOCK_CODES` (so `_is_publishable`,
  `human_edit_gate`, T3's hard-code read and the review summary all see it), NOT in
  `_REWRITE_CLASS_HARD_CODES`. `HARD_CODE_REPAIR` = "review: …"; test_s218 accepts the `review:` prefix.
- **T9 hold** (`_degraded_final`) is the step right after `_final_integrity`, computed from the final
  attempt's ledger (an outage on attempt 1 and a good attempt 2 is not degraded). The F9 outage result keeps
  `blocking: False` (no rewrite on it, AA-528 guard unaffected) and carries `degraded: "f9_judge_unavailable"`.
  An approved piece becomes `held` ("degraded: f9_judge_unavailable") + a non-blocking `degraded` flag + a
  `repair_log` entry. `held` stays admin-only (tenant still receives it).
- **Central Jev counter**: `decide()` (and `decide_sync` on a loop) pass every Decision through `_counted`,
  which notes zone=error verdicts into a contextvar collector the job worker binds next to `bind_job`; the
  worker merges it into `shared.job.result.degraded` (dict / None results only). A3, ingestion and the S1
  prefetch are counted without touching their code.
- **Eval**: every score verdict carries `degraded` + `degraded_hard`; a hard-degraded sample is scored once
  more; still hard → its stratum is in `invalid_strata` and `report.py` refuses that stratum's delta (and the
  "all" row). `report.py` prints the fitter revert criterion. `evalpatch.install(run_id, stage=…)` and
  `spend_sql(stage)` take the ad-hoc stage (default `adhoc_aa729`; the drill uses `adhoc_aa925`).
  `services/eval/regression.py` writes `details.degraded` / `details.invalid` and never flags an INVALID run.
- **Q0 `jev: "none"` stub** now returns `zone=skipped` verdicts (Jev off) instead of `{}`: in production `{}`
  only happens when `judge_units` failed open, which is now degraded. Same behaviour on the base branch
  (a skipped verdict and no verdict classify the same), so red-on-base is unaffected. New kinds: `down`
  (`{}`), `raise`, `error-shadow`.

## Changed

- New: `services/quality/{degraded,repair_contract}.py`, `scripts/aa925/jev_drill.py` (force-added),
  tests `tests/unit/quality/test_{degraded,repair_contract}.py`, Q0 cases `a1_degraded.json` (5),
  `a1_contract.json` (4), `t9_degraded.json` (2).
- `graph.py` (hard code + repair text, validate's `day_check_skipped`, `_grounding_recheck` + hold, revalidate
  contract), `grounding.py` (`jev_errors`, `degraded_entries`, contract in `repair_and_recheck`, node
  degraded), `flag_fix_node.py`, `judge_node.py`, `brand_audit_node.py`, `seo_meta_utils.py` (no cut),
  `tenant_pipeline.py` (meta regenerate, Jev errors, contract, outcome `degraded`), `sentence_repair.py`,
  `quality_gates.py` (F9 marker), `service.py` (`_degraded_final`), `decide.py`, `jobs/worker.py`,
  `eval/regression.py`, `scripts/aa729/{score,report,evalpatch}.py`, `reviewModel.ts`, `CONTEXT.md`.
- Tests updated: `test_aa740_seo_length_fit`, `test_aa747_flag_fix_seo_fit`, `test_aa794_seo_meta_cut_country`
  (a case that expected a cut now expects the meta unchanged / the T3 regenerate), `test_s218_hard_code_repair`
  (`review:`), `test_aa768_s234_round4` (the contract guard fires before integrity on a broken bracket),
  `test_aa729_golden_set` (+ degraded / INVALID / stage tests), `quality/test_chain_order` (the post-validate
  repair now lives in `_grounding_recheck`; the A1 / T9 holds are the last step).

## Tradeoffs

- **Reverses AA-770** for one case: an F9 outage now holds (admin label). Review load goes up during an
  outage; the tenant is unaffected.
- **A1 Jev hold = review load without safety gain today** (`a1_claim_supported`: enforce, accept_floor 0.85,
  reject_ceiling NULL — Jev only clears numeric hits; numbers are already fail-closed). It becomes protective
  after AA-845 sets a reject ceiling. An outage during a wave sends every A1 tour to manual_check; the
  wave-level stop / requeue and the 5% banner (design §6.3) are out of scope.
- **Fitter change raises LLM repair load** (lessons S218: the AA-740 cut took flag_fix from 79% to 25% of
  tours). Kept: the uncut-period branch and the sentence salvage; T3 has the meta-only regenerate.
  **Revert criterion** (PR body): in the AA-729 regenerate vs baseline `dab254c4`, revise the fitter part if
  s1_flag_fix calls per S1 sample or the S1 review rate move beyond baseline + the A' noise (report.py prints
  both).
- Not spliced (see Decisions): T9 truth cuts and FAQ regeneration — their own gates already re-run.

## Should know

- PR 3 is stacked on PR 2 (`feat/aa-925-day-oracle`), which is stacked on PR 1. After PR 2's squash merge:
  `git rebase --onto origin/main <PR2 tip> feat/aa-925-repair-contract`.
- Red on base: the 7 new positive cases (`a1-degraded-*-hold` ×3, `a1-contract-flagfix-adds-figure`,
  `a1-contract-meta-cut-and-period`, `a1-fitter-long-meta-goes-to-llm`, `t9-f9-outage-held`) fail on the PR 2
  tip; the 4 negative twins carry `red_on_base: false` + `twin_of`.
- **Drill (Done-when 3)**, after deploy, from an origin/main archive (`scripts/aa925`, `scripts/aa729`):
  `cd /app && PYTHONPATH=/tmp/aa925 python3 -m scripts.aa925.jev_drill --max-cost-usd 0.5`. It picks a
  published tour with day markers, ≥ 8 sentences and 0 numeric hits, trips the breaker directly (never a fake
  402 — that sends a real alert), bypasses the verdict cache for the drill (a cached verdict skips the
  breaker), runs `_rewrite_tour` on the stored text via `batch_text` (no writer call unless the gate retries)
  and asserts `jev_unavailable` > 0, `DEGRADED_DETECTOR`, `manual_check`, not publishable, 0 grounding
  violations. Exit 0 only when all hold. Nothing is persisted (`_rewrite_tour` does not write).
- `silver_aa_internal.generated_content` has RLS on + FORCE; the drill and the day-oracle report read it as
  `cis_app` (the container DSN). If the read returns 0 rows, the role's policy is the reason — run the read
  part through DB Migrate `mode=run-script` instead.
- Frontend: only `reviewModel.ts` (one string added to a Set); eslint clean on that file. tsc not run (whole
  project, RAM); CI runs it.
