# AA-900 — S271 W0 lane l14: Writing Lab eval interim (R2)

Source plan: root audit `docs/audits/2026-11-06-S271-writing-quality-audit/` (README, decisions, the judge root cause
`data/aa900-judge-rootcause.json` plan F1-F8 and the 3 votes, `data/design-review.json` eval pick + roadmap W0
"Interim eval"). Scope: `services/writing_memory/{r2,eval_judge,eval_checks,config}.py` + tests. The rubric text and
the golden labels are not changed here (W1 compiles the rubric; t9b-002 is a golden-set version bump later).

## Decisions

- **Pairing on (sample_id, input_sha256), with the release-0 baseline replacement (F1 as the skeptic narrowed it).**
  `PgStore.control_sources` now matches the control run's rows to today's items on `(sample_id, input_sha256)`
  (joined through `shared.eval_golden_item`), keyed by the current item id, with `source_item_id`. The CIS 284 trigger
  refuses a rejudge row whose `scored_result_id` belongs to another item, so a row of another golden version can
  never be stored as this item's control. Therefore: when the active part's eval run is on another golden set and it
  is a `baseline_import` run or the release is `empty` (release 0 and every empty release write as the baseline
  writer), the current version's baseline replaces it (`details.control_run_stale.replaced_by`). Any other stale run
  (a rules release evaluated on an older version) keeps its run; its sources are counted as `controls.cross_set` and
  the run is refused at $0 with "run a reverify on this version first". No migration of the 284 trigger.
- **Planned count** = non-trap planned items, minus post_edit items when the control is a baseline import (derive
  skips them, there is no frozen row). Refusal when `found < planned` (rejudge mode, not calibrate-only).
- **`require_calibrated` defaults to True.** Pre-spend refusal (R2Refused, nothing stored) when calibration cannot be
  measured (no baseline, < 10 labelled frozen items with a baseline row, < 3 of a label). After the kappa block a
  judge that does not count raises the existing `_Stop("completed", "calibrate")`: verdict informative, judge cost only,
  `details.calibration_gate = judge_not_calibrated`. `--allow-uncalibrated` restores the old informative run;
  `--calibrate-only` (mutually exclusive) always stops there and judges no control of its own (only the frozen texts;
  a same_row control rides along at no cost).
- **Mixed judge = `stop_reason 'error'`, `details.error = 'judge_model_mixed'`** (CIS 284 CHECK allows only
  completed / hard_stop / futility / budget_cap / probe_fail / error). Identity = (answered model, answering
  connection). Checked after every stored row, so the run stops at the first mixed row. Applies to unpinned route
  runs too (a route fallback mid-run now invalidates the run, which is the point of RC5).
- **Pin.** `--judge-model` binds `judge_output(model_key=...)` through `functools.partial` (the runner's judge call shape
  and every fake judge in the tests stay the same, vote 3). `--judge-connection` puts that connection first with the
  existing `deployments.priority_override` (context-local) and refuses a reply from another connection
  (`JudgeIdentityMismatch`, raised after the ledger row is written, so the cost is never lost). A reply from another
  model than the pin is refused the same way (compared with `family_rule.model_key_of`, so `satellite-` labels match).
  Result rows: `judge_version = <prompt version>@<pin>`, `details.judge = {model_key, connection, provider_model}`;
  run: `prompt_version = <writer>:judge-<hash>@<pin>`, `details.judge`, `details.judge_identities`.
- **Pinned family check** (`pinned_conflicts`): the pin against EVERY rung of the writer route (primary + fallbacks),
  because a pinned judge runs alone and any rung may write a candidate; a pin the catalog does not know is refused.
  Without a pin the old route check runs unchanged.
- **Judge input (F6).** SEED 24000, SOURCE 32000, OUTPUT 16000; `build_user_prompt` clips both with `clip` (line or
  word boundary + CUT_MARK) instead of a silent slice; one system line: an OUTPUT ending in the cut mark was shortened
  by the grader, never report struct.truncated there. `judge_output_text` sends labelled fields, short ones first
  (TITLE, NAME, SEO TITLE, SUBTITLE, META DESCRIPTION, SLUG (URL key, at most 60 characters), HASHTAGS, other keys,
  BODY last), so a cut shortens the body, never the meta or slug. QA / isolation / injection checks keep
  `ew.output_text`. `judge_version()` moves (now `dcc7a1415a6c`, was `73e641a99aca`).
- **Prompt registry.** The judge prompt symbols changed, so the manifest gets `writing_memory_eval v2` and migration
  385 registers it (contract: rows into an existing table). Separate migration-only PR first (MIG-4).
- **Cost (F8).** `item_estimate`: median of the writer's last 50 logged candidate item costs (finished r2 / reverify
  runs, no seeded run) x 1.3, the config constant until 5 exist. Constants: t9_blog 0.08 -> 0.18 (measured median
  $0.134, mean $0.161 at effort high in the two S271 runs), JUDGE_CALL_ESTIMATE_USD 0.01 -> 0.005 (median $0.0025).
- **DRY RUN cost refusals (review fix).** `estimate()` adds `estimated X > cap Y` when `estimated_usd_max` passes
  `cap_usd`, and `month cap` when the month's learning spend plus `estimated_usd_max` passes `MONTH_CAP_USD`, to
  `would_refuse`, so `--enqueue` exits 1 and queues nothing (it used to queue a run its own estimate said would end
  `budget_cap`, verdict `invalid`, as 08c34cb4 / 858c8bf6 did). It applies in every mode; calibrate-only reserves
  judge calls only (cents). The month check uses the estimate, not the cap (that is where the run's reservation stops);
  `month_after_cap_ok` stays as the informative cap-based figure. The runbook runs the DRY RUN at `--cap-usd 10` ($0)
  and sizes `<cap>` from `estimated_usd_max` rounded up (a full t9_blog plan is about $3 on the S271 ledger).
- **after() never stops.** It records `details.overspend_usd` / `overspend_phase`; the reservation before the next item
  is the stop. A finished run keeps its verdict.
- **Hard stop 1 (F4 + skeptic 2):** counts only when the candidate verdict is fail AND the control verdict was pass.
  `new_hard` stays recorded as evidence on every row.
- **Job payload:** `config_from_payload` refuses unknown keys (vote 3: an older worker image would silently drop
  calibrate_only / require_calibrated / judge_model and run a paid run unguarded). The job handler parses inside the
  `R2Refused -> NonRetryable` block.

## Changed (vs the lane brief)

- F1's "or its eval run holds no row for today's items" clause was NOT implemented (vote 3: it would silently change
  a rules release's comparison to the frozen fixture); only baseline-import / empty-release stale runs are replaced.
- `prompt_version_mismatch` in reverify (roadmap W0 item) is not in this lane: listed as a follow-up.
- The OpenAI-direct monthly headroom precondition (design-review P0) is not coded: the DRY RUN shows
  `month_after_cap_ok` for the writing-memory month cap only.
- Extra: `estimate()` (DRY RUN) now reports `controls`, `control_run_stale`, `calibration_ready`, `item_estimate`,
  `judge_model`, `would_refuse`, `month_after_cap_ok`; `scripts/aa919/judge_compare.py` and `kappa_diag.py` read the
  labelled output view.

## Tradeoffs

- The cost refusals live in the DRY RUN / CLI only, not in `run_r2`: `estimated_usd_max` is an upper bound (ledger
  median x 1.3 per item), and the job keeps the exact per-item reservation as its guard. Refusing in the job too would
  turn runs that can finish under the cap into $0 refusals and break the budget-cap tests of the runner.

- Pairing a stale non-empty release's outputs across versions would need the 284 trigger changed (a contract
  migration on a guard) for little gain: such a run should be a reverify on the new version anyway. Refusing at $0 is
  the honest result (today it silently paired 0 and could never pass).
- `require_calibrated` by default changes behaviour for every R2 caller (CLI and job). Tests that exercised the
  informative path now pass `require_calibrated=False`; the Releases tab enqueues nothing itself (CLI only).
- A route (unpinned) run that falls back mid-run now ends invalid instead of mixing judges; with acc3 deferred this can
  happen more often. Use `--judge-model` for paid runs.
- The connection pin uses the admin-preview priority override (reorders, does not remove other deployments): a fallback
  to another connection is caught after the call (billed, error row), not prevented. A gateway-level hard pin is W1+.

## Should know

- **Offline replay (FREE-3), `scripts/aa900/w0_r2_replay.py` on the S271 export (`.tmp-session/S271/design/calib.raw`):**
  - 858c8bf6 (gpt-6-luna-openai): paired old 0 -> new 12 of 15 (3 traps unpaired); old rule's first hard stop
    t9b-006 (7th candidate, a fail on a FAIL control); W0 rule: no hard stop (the uncalibrated judge failed every
    control, so 5 noisy triggers on fail controls are gone). Estimate predicts $2.78 vs logged $2.46 (+13%).
  - 08c34cb4 (nova-lite): paired 0 -> 12; old first stop t9b-002 (4th candidate); W0: none (t9b-010's class on a
    passing candidate no longer stops). Estimate $2.79 vs $2.47 (+13%).
  - What-if with controls = golden labels (`--control-from-label`, a perfectly calibrated control side): the first stop
    is UNCHANGED (t9b-006 / t9b-002): those candidates are real fails on pass-labelled items with a new hard class.
    So W0 removes the noise stops, but if first drafts genuinely fail pass controls the run still stops early, as the
    skeptic predicted. That is a real signal, not a bug; the T9 first-draft-vs-final confound is W3/W5 (eval runbook
    now says a t9 fail alone does not justify a revert).
- **Migration number:** written as 385 from the orchestrator hint, shipped as migration 384 (renumbered from 385 at merge time, S271). The recipe for a renumber: `git grep -n '<old>'` in the
  migration file, its test (`test_aa900_migration_385.py`, `migration-385`, `'385'`) and nothing else (the code PR
  does not name the number; the manifest entry has no number).
- **Merge order:** migration PR (384) -> Infra bot copy -> Migration Apply (class contract) -> code PR. The code PR's
  `test_aa900_prompt_registry_gate.py` fails until 385 is on main (the manifest names v2, no migration inserts it).
- **Golden set (not changed):** t9b-002 ("ZZ S239 test label" stub) removal is a later golden-set version bump (F7);
  t9b-004 / -011 / -017 contested labels go to a panel review. Importing a new version supersedes every writer's set;
  with this PR release 0 follows automatically (baseline replacement), other releases need a reverify.
- Labels are inconsistent on the same defect types (vote 1: 004 vs 019, 017 vs 020, 011 vs 012): the kappa gate may
  stay unreachable with this rubric; calibrate-only makes that cost ~$0.06 instead of ~$2.4.
