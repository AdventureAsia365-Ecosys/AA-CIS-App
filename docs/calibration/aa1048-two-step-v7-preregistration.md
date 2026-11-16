# Pre-registration — two-step claim instrument v7 (AA-1048, Quality W2, S282)

Registered 16/11/2026, before any paid call of v7. The machine-readable copy is
`services/ground_truth/preregistration/aa1048_two_step_v7.json`. Its sha256 is pinned in
`services/ground_truth/two_step_sets.py` (`STEP_SETS["v7"]`): `two_step run --live` reads only that committed file,
refuses it when its sha256 differs from the pin, and refuses while the settings, prompt texts, shaping runs and pools,
candidate or vote count in force differ from it. Root ADR 0020 W2. It follows the v6 registration
(`aa1048-two-step-v6-preregistration.md`), whose one gated run was no_go.

## Question

Does the two-step claim instrument v7 drop the false positives of the R1 fired claims and keep the stored violations,
on a fresh pool no instrument run has read?

- **Step 1** (`aa1048-r1-v7c@0fb75e310375`): v6c's order stays (set aside what is not about this trip, then the trip
  specifics left, then the lookup) and so does its recall guard. The lookup now looks for the fact, not the words:
  for each trip specific the reply writes a `checked` row (the specific, the closest source wording, `same_fact`)
  before `new_specifics` and `kind`. A close-paraphrase list says what the same fact is: the same thing under another
  name (synonym, a looser or more general name, singular or plural, a verb for the source's noun), the same number or
  measure written another way, the same quality in other words, a feature the place background gives for a place the
  trip visits, landscape the source puts around a stop crossed on the way to it. The guard next to it: a source place,
  stop or time with a new quality, mood, light, view, sound, measure, group or suitability added is NOT the same fact,
  nor is a fact stretched to other days, more nights or the whole trip. The schema is part of the tag.
- **Step 2** (`aa1048-r1-v7j@5a91a98e9136`): a lookup that can come out either way. For each specific step 1 listed
  the reply writes a `checks` row (the specific, the closest source wording, a finding: `not_trip`, `same_fact`,
  `one_step`, `not_given`, `against`) before the verdict: `supported` when every row is `not_trip`, `same_fact` or
  `one_step`, `unsupported` when at least one row is `not_given` or `against`. v5j's promise list defines `not_given`.
  The vote is the reply's `verdict`. Same user prompt as v5j / v6j; the schema is part of the tag.

## Why v7

v6's gated run `c5983e03` (46 claims, 0.072 USD) kept 21 of 21 stored violations and dropped 14 of 25 false positives
(56 %, bar 60 %); step 2 cleared none of the 11 false positives it judged. The error analysis
(`docs/implementation-notes/AA-1048-v7.md`, counts only):

- the 11 false positives step 1 sent on: the same fact in other words (5), a quality in other words or one step away
  (4), the writer's reading told in see / feel words (2); 9 of the 11 were restatements by the panel's step-1 answer;
- both steps were unanimous on every claim they flagged; in 6 of the 11 the step-2 reasons name the source line that
  gives the same fact and still flag, because the words differ;
- the background fix of v6 worked (8 of 10 background false positives cleared, v5: 3 of 11).

Both v6 steps compared words, not facts. v7 changes the lookup and gives step 2 a way to clear, not the bars.

## Hypotheses

- **H1.** Step 1 still sends at least 95 % of the stored violations on: a source place, stop or time with a new
  quality, mood, light, measure, group or suitability added is not the same fact.
- **H2.** Step 1 clears most paraphrase false positives that only restate a source fact in other words, because it
  writes the closest source wording before it decides.
- **H3.** Step 2 clears at least 40 % of the false positives it receives, because a listed specific the source gives in
  other words is `same_fact`, not `not_given`.
- **H4.** Step 2 still flags at least 95 % of the stored violations it judges: the promise list defines `not_given`.

## Pool

v7 was written from run `c5983e03` on the S281 fresh pool (`716df551d458fee1`): the 11 false positives step 1 sent on
were read claim by claim with their source lines, and the 21 stored violations for the shape of their specifics. So
`c5983e03` is a shaping run and the whole S281 fresh pool a shaping pool, with the S279 pool (`487bf4847b6136fe`) and
the runs `3018008d`, `33835b87`, `2977dd78` and `9b0d46f8`. There is no claim-level hold-out.

The run uses a NEW fresh pool (`fresh_pool.py`: R1-fired claims on pieces no instrument run has read, built in S282 from
never-read claims on 20 pieces), given at run time with `--fresh`. Its manifest must match the pool file, list every
shaping run in `excluded_runs` and both shaping pools in `excluded_pools`, and share no piece with them. The S281
fresh pool is a train-split pool, so a train-split manifest alone does not keep it out (`prompts.TRAIN_SPLIT_POOLS`):
it must be named. The registration names no pool digest; the run report records it, and the stop rule reads it from the
ledger.

## Subset, score and bars

Unchanged from v5 and v6: `instrument_eval.select`, 3 votes per step on `gpt-6-luna-openai@openai-direct`, stage
`adhoc_aa1048`, role `judge`; a claim is cleared when the step-1 majority is restatement, otherwise its verdict is the
step-2 majority.

| Bar | Value | Setting |
|---|---|---|
| False positives dropped (overall) | >= 60 % | `cis.quality.instrument_eval_fp_drop_min_pct` |
| Stored violations kept (overall) | >= 90 % | `cis.quality.instrument_eval_tp_keep_min_pct` |
| Stored violations step 1 sends on | >= 95 % | `cis.quality.claim_two_step_route_tp_min_pct` |
| Violations step 2 flags (of those it judges) | >= 95 % | `cis.quality.claim_two_step_judge_tp_keep_min_pct` |
| False positives step 2 clears (of those it judges) | >= 40 % | `cis.quality.claim_two_step_judge_fp_drop_min_pct` |

## Stop rule

One gated run of `aa1048-two-step-v7`, after a trial. The ledger rule applies unchanged: a gated run is refused when
`shared.llm_call_log` holds a gated run of this registration (any pool) or of the same step-1 prompt on the same pool,
or cannot be read. v5 and v6 are closed step sets: every live run of them (trial or gated) is refused in code. A no_go
of v7 is read at $0 and closes the iteration; a go is narrow evidence that leads to a proposed full held-out run (the
tech lead's spend yes) before any shadow or pin.
