# Pre-registration — two-step claim instrument v6 (AA-1048, Quality W2, S282)

Registered 16/11/2026, before any paid call of v6. The machine-readable copy is
`services/ground_truth/preregistration/aa1048_two_step_v6.json`. Its sha256 is pinned in
`services/ground_truth/two_step.py` (`STEP_SETS["v6"]`): `two_step run --live` reads only that committed file, refuses
it when its sha256 differs from the pin, and refuses while the settings, prompt texts, shaping runs and pools,
candidate or vote count in force differ from it. Root ADR 0020 W2. It follows the v5 registration
(`aa1048-two-step-preregistration.md`), whose one gated run was no_go.

**Outcome (S282).** The one gated run (`c5983e03`) was no_go, but close: false positives dropped 14 of 25 = 56 % (bar
60 %), stored violations kept 21 of 21 = 100 %; step 1 sent every violation on (100 %), step 2 flagged every violation
it judged (100 %) and cleared none of the 11 false positives it judged (0 %, bar 40 %). H1 held, H2 held for background
(8 of 10 cleared), H3 failed. v6 is now a closed step set (every live run refused in code). The next iteration is v7:
`aa1048-two-step-v7-preregistration.md`.

## Question

Does the two-step claim instrument v6 drop the false positives of the R1 fired claims and keep the stored violations,
on false positives it was not shaped on?

- **Step 1** (`aa1048-r1-v6c@711ef584d7a0`): first sets aside every part of the sentence that is not about what this
  trip gives the traveller (background about a named place, people, craft or region; the writer's reading,
  comparisons, intensifiers and figures of speech; tone; a word that names the reader; text about the text). Then it
  lists the trip specifics left and looks each up in the whole source, with a wider set of one-step readings. Wording
  about meals, the guide, other travellers, crowds, pace, effort, comfort, safety, suitability or what the traveller
  will see, hear or feel at a stop is never set aside. The reply schema asks for `set_aside` before `kind`; the schema
  is part of the tag.
- **Step 2** (`aa1048-r1-v6j@21f0ea6956df`): for each specific step 1 listed, first decides whether it is a trip
  specific at all (if not, it is supported), then looks it up with the same one-step readings, then applies v5j's
  promise list.

## Why v6

v5's gated run `9b0d46f8` (46 claims, 0.076 USD) kept 20 of 21 stored violations (95.2 %) and dropped 8 of 25 false
positives (32 %, bar 60 %). The error analysis (`docs/implementation-notes/AA-1048-v6.md`, counts only):

- step 1 sent 19 of 25 false positives on, 17 of them by all three votes;
- the primary step-1 patterns were background treated as a trip fact (7), an over-literal reading of a paraphrase
  (6), the writer's reading (4) and a word naming the reader (2);
- step 2 cleared 2 of the 19 (10.5 %, bar 40 %): it repeated step 1's source lookup.

Both v5 steps asked "can you point to every fact in the source?" before they applied the background rule. v6 changes
the order of the question, not the bars.

## Hypotheses

- **H1.** Step 1 still sends at least 95 % of the stored violations on: the trip specifics they add are never set
  aside.
- **H2.** Step 1 clears most background and reading false positives, because it sets them aside before it looks
  anything up.
- **H3.** Step 2 clears at least 40 % of the false positives it receives, because a listed specific that is not a trip
  specific is supported.

## Pool and hold-out

The pool is the S281 fresh pool (digest `716df551d458fee1`, train split; the S279 pool and the v2-v4 runs are
excluded at piece level). v6 was written from the 25 false positives of run `9b0d46f8` (their step votes, specifics and
reasons), so those 25 claims are in-sample for v6:

- the registration lists their hash prefixes (`held_out.claims`), and the runner drops them from the pool before the
  subset is drawn;
- run `9b0d46f8` then counts as kept out of this pool (`covered_by_held_out`); on any other pool the fresh manifest
  must exclude it at piece level, as for every other shaping run;
- a pool that does not hold every listed claim under the registered digest is refused.

After the hold-out the pool has 41 sampled false positives (paraphrase 20, background 17, verbatim 4); the 50-claim
subset takes 25 of them (paraphrase 12, background 10, verbatim 3), none of which was read.

**The TP side.** The pool holds 21 stored violations, and all 21 were in run `9b0d46f8`. v6 was written without
reading their text, specifics or reasons (aggregate counts only), so they remain the recall check of this run. A TP
side that no instrument run has seen needs a new paid R1 fire and a panel round; that is a limit of this registration,
stated here before the run.

## Subset, score and bars

Unchanged from v5: `instrument_eval.select`, 3 votes per step on `gpt-6-luna-openai@openai-direct`, stage
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

One gated run of `aa1048-two-step-v6`, after a trial. The ledger rule of v5 applies unchanged: a gated run is refused
when `shared.llm_call_log` holds a gated run of this registration (any pool) or of the same step-1 prompt on the same
pool, or cannot be read. v5 is a closed step set: every live run of it (trial or gated) is refused in code. A no_go of
v6 is read at $0 and closes the iteration; a go is narrow evidence that leads to a proposed full held-out run (the tech
lead's spend yes) before any shadow or pin.
