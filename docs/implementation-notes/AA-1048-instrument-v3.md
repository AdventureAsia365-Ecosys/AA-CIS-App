# AA-1048 — claim instrument prompt v3 (paraphrase) and an out-of-sample evaluation (lane aa1048, S280)

Follows `AA-1048-instrument-v2.md` (S279). The v2 evaluation (run 3018008d, 50 claims, 150 calls, 0.0316 USD) was
**no_go**: false positives dropped 12/25 = 48% (bar 60%), stored violations kept 18/20 = 90% (bar 90%). By group:
verbatim 4/4 dropped, paraphrase 7/17, place background 1/4. This change builds v3 against the paraphrase failures and
adds what a clean v3 measurement needs. The pre-registered bars are unchanged (settings `cis.quality.
instrument_eval_fp_drop_min_pct` = 60, `..._tp_keep_min_pct` = 90; nothing in this PR touches them).

## Decisions

- **What v3 targets.** The 13 false positives v2 still fired on (10 paraphrase, 3 background) were read with the
  panel's reasons ($0, the S279 local panel file). The pattern: the sentence restates the source and adds only a
  style word ("entirely"), a viewpoint ("seen from the bike"), a framing verb ("built around"), an image, the
  writer's reading of the facts ("so the two belong to one outing"), the source's own options as traveller choices,
  a trip-wide fact placed on one day, a descriptive label made of source words, or a day heading whose start the
  source implies. v2's B1 said "paraphrase is supported" with examples, but the model still fired on the leftover
  word. v3 therefore makes paraphrase a procedure (P1):
  1. find the source statements the sentence restates, in the whole source;
  2. set aside wording that adds no checkable fact (a named list of kinds);
  3. judge the facts that are left.
  It fires only on a fact it can name ("If you cannot name one, the sentence is supported"), and the reply's `why`
  must name that fact.
- **Recall guard inside P1.** Step 2 says explicitly that wording about meals, the guide, the group, crowds, or what
  the traveller sees, hears or feels at a stop is a fact, not style. Otherwise "set aside the style" would swallow
  the B5 promise patterns the instrument exists for.
- **New rules beside P1.**
  - P2: a trip-wide fact supports the same fact on one day, unless the source says otherwise for that day.
  - P2b (B2 widened): background on crafts, foods and customs, and on how a place is usually described or compared.
    A sentence that joins that background to a visit the source gives is supported.
  - P3: a label made only of source words is a paraphrase, capitals or not. A name with a word the source lacks is
    still a new name, which protects fact.name. A day heading is supported when its start is where the trip
    already is.
- **B3 and B4 are v2's strings, byte for byte** (`_V2_RULES[2]`, `_V2_RULES[3]`). B5 keeps every v2 item and adds two:
  an order or schedule the source does not give, and a number bound to the wrong thing ("it" read as the thing
  named just BEFORE). These are the two stored violations v2 lost: an invented order between two evening stops, and
  the dimensions of a viaduct written after a sentence about a nearby peak.
- **v3 user prompts = v2's** (whole source, BEFORE / SENTENCE / AFTER). Only the system texts change, so the
  comparison with v2 isolates the rules.
- **Every example is invented** (Karsk, Brellin, Ostra; olive groves, a cable car and a hilltop fort, a canoe trip,
  herons and otters). A scratch check against the 286-claim pool, the same rule as v2, found that no three-word
  sequence with a content word is shared between the v3 system texts and any pool claim. The first draft failed
  this on 13 claims (generic phrases such as "the same day", "a visit to", "known for its") and was reworded until
  0 matched. v2 still has 0.
- **The v2 subset is in-sample for v3, so v3 is measured on other claims** (`run --exclude <v2 report>`). v3's rules
  were written from v2's misses on its own 50 claims, so scoring v3 on those claims would grade the prompt on the
  questions it was written from. That is the S279 relabel rule applied to the whole subset. `exclude` drops the 45
  non-relabel claims of the v2 report; the 5 relabels stay, as in v2, reported apart and never gated. The fresh
  subset is again 17 paraphrase / 4 verbatim / 4 background false positives (from 118 / 26 / 25 left) plus 20 stored
  violations (from 51 left) and the 5 relabels. The R1 pool has enough claims for one more fresh round of this size
  (paraphrase 101, verbatim 22, background 21 and stored 31 left after v3).
- **$0 replay (`score`)**: re-scores a finished run from the votes in its report (the subset = the pool claims it
  voted on). On the S279 report it reproduces no_go exactly: 12/25 dropped and 18/20 kept, `matches_stored = true`.
  It cannot evaluate v3 at $0: no v3 reply exists, and model votes cannot be replayed under a new prompt. v3 needs
  the gated paid run.
- **`--measured <report>` prices the plan from a measured run.** The model estimate assumes 6,000 reasoning tokens
  per reply. GPT-6 Luna billed a mean of 253 output tokens per entailment reply in S279 (max 1,233). So the model
  plan for 50 claims is 0.52 USD against a real spend of about 0.03 USD, and with the 2x safety rule it refuses any
  cap under about 1.05 USD. The brief caps this run at 0.10 USD.
  - The measured expectation of one call = the run's measured cost per call x (this call's input estimate / that
    run's mean input estimate), both on r1's conservative chars/3 estimate.
  - The report must be a finished live run (not stopped, calls > 0, spent > 0) of the same model, connection and
    mode. Its prompt-version tag must resolve to a version whose texts still hash the same, and every voted claim
    must be in the pool. Anything else is refused.
  - The 2x safety rule is unchanged. The live gate never reads the expectation: every call is still charged its
    worst case against the cap before it is made.
  - On the real pool: v3 50 claims, 150 calls, expected 0.0415 USD (fits 0.097 and 0.10); trial 3 claims, 9 calls,
    expected 0.0019 USD.
- **The plan rows add `worst_call_usd_max`** (0.0139 USD for v3 at 1 reserved attempt) **and `expected_from`**, and
  `_live` returns `reserved_attempts`. The trial cap has to cover the first call's worst case times the gateway's
  reserved attempts, or the gate refuses the first call. The runbook states how to choose the cap.
- `DEFAULT` stays `v1` (claim_shadow, R1 records). The CLI default prompt stays `v2`; the live command passes
  `--prompt v3` explicitly.
- Test that pins the recorded tags: `aa1048-r1-v2@ddaab8c279ef` (the S279 ledger rows) and
  `aa1048-r1-v1@bf496005d329`. A future edit to v1 or v2 text fails CI instead of orphaning the records.

## Changed (vs. the lane brief)

- The brief asked for "a $0 evaluation over the saved forms if the harness can replay". The harness replays the
  *scoring* of a finished run (the new `score` command, used to confirm the S279 verdict). It cannot replay a model
  under a new prompt. The $0 part of v3 is therefore:
  1. the replay of v2;
  2. the miss analysis that shaped v3;
  3. the leak check;
  4. the dry-run plan on a fresh subset.
  The v3 verdict needs the gated paid run.
- To fit the 0.10 USD cap, two flags were added that the brief did not name: `--measured` and `--exclude`.
  `--exclude` was not strictly needed to fit the cap; it is there for validity (above).

## Tradeoffs

- **Fresh claims vs. a like-for-like comparison.** v2 and v3 are scored on different claims, so the comparison
  includes sampling noise: with 25 false positives a share moves 4 points per claim. The alternative was to re-score
  v3 on v2's subset, which is like-for-like but in-sample and biased toward v3. The bars are absolute (60% / 90%),
  not "better than v2", so fresh claims answer the pre-registered question. If v3 passes narrowly, a second fresh
  round (enough claims are left) settles it before a full R1.
- **Less firing could cost recall.** P1 makes "cannot name a fact" mean supported. That lowers fires, and could
  lower recall on vague promises. The recall guard in step 2 and the unchanged B5 list are the counterweight, and
  the gate checks recall on 20 fresh stored violations (a 90% bar allows 2 lost).
- **A measured price vs. the model estimate.** The measured price trusts one earlier run of a sibling prompt. v3's
  system text is about 2.7k characters longer than v2's (5.6k vs. 2.9k), which the input-ratio scaling covers. Its replies may run
  longer because `why` must name a fact; the 2x safety covers about 2x. The hard per-call gate makes an under-estimate
  cost a cut run, never an overspend.

## Should know

- **No migration and no settings change.** `--exclude`, `--measured` and `score` are CLI-only tools of an adhoc
  evaluation, not policy values. The bars and caps stay in the settings registry.
- The live run needs this code on Dev (the image has no `scripts/`, and the evaluation is `services/...`). Order:
  merge, then Deploy Dev, then the trial, then the full run. Runbook and exact commands: root
  `.tmp-session/S280/aa1048/RUNBOOK.md` (scratch, not in the repo).
- The exclude file holds hash prefixes only (no text), so it is safe for the scripts bucket. The pool file holds
  real text: upload it for the run and delete it afterwards, as in S279.
- If v3 is go, the next step is a full held-out R1 on v3 (entailment) under the tech lead's spend yes, before any
  shadow or pinning. If v3 is no_go, read its misses on the fresh subset ($0, `score` plus the panel reasons) before
  writing a v4. Do not pay for a re-run of the same prompt.
