# AA-1048 — claim instrument prompt v2 and its gated evaluation (lane l22, S279)

Follows `AA-1048-ground-truth.md` (S272, the R1 harness), `AA-1048-pooled.md` (S278) and the S279 blind 3-lens panel
on the 215 false positives of R1 (root `docs/audits/2026-11-13-S279-session/aa1048/panel-report.md`). The panel found
that the instrument over-fires (H2: about 95% of the false positives), not that the labels are wrong (H1: 2.3-3.3%),
and proposed prompt changes B1-B5 and a gated entailment run on a subset of the fired claims.

Two PRs (MIG-4): the migration-only PR (CIS 416: five settings, `enforced = false`) and this code PR.

## Decisions

- **Prompt versions are their own module** (`services/ground_truth/prompts.py`): `v1` = the S272-S278 texts byte for
  byte (still the default, so `claim_shadow` and the R1 records keep their prompt and their instrument ids), `v2` = the
  panel's B1-B5. `r1.py` re-exports `ENTAILMENT_SYSTEM` / `EXTRACTION_SYSTEM` and keeps `entailment_prompt` /
  `extraction_prompt` as v1 wrappers. r1.py was 722 lines; the prompts moved out keep it under the 800-line ratchet.
- **A prompt version is part of the instrument** (`Instrument.prompt`, default `v1`): a different prompt is a different
  instrument to calibrate (critic F8 spirit). The id of a v1 instrument is unchanged; any other version adds `#<id>`
  (`gpt-6-luna-openai@openai-direct:entailment#v2`).
- **Recorded per call**: `PromptVersion.tag` = `aa1048-r1-<id>@<sha12>` (sha256 of the version id, both systems and the
  user templates). r1.run puts it in `quality_signal.prompt_version` and passes it to `billed_fields(resp,
  prompt_version=...)`, so `shared.llm_call_log.prompt_version` names it (the gateway has no registered version for the
  adhoc stage). v1 calls now record their tag too.
- **v2 entailment reads the whole source** and the text around the sentence (`BEFORE` / `SENTENCE` / `AFTER`,
  `prompts.around`), not the three aligned lines: "support the instrument did not see" and the heading / FAQ cases
  (B4) need it. v2 extraction keeps the v1 user prompt and asks for whole sentences.
- **Every prompt example is invented** (Varno, Mount Selk, Orvin, Tessa, Velmora): the golden-set text guard, and no
  claim text from the panel leaks into a public repo. The first v2 draft broke this for B5: its examples were close
  paraphrases of the five relabelled claims (the same meal phrase, an arrival sentence with only the place name
  swapped, the guide, group and stop-experience phrasings), and the docstring said nothing was copied. Review fix:
  each B5 pattern keeps its class but has examples in wording of its own, and four B1 examples that shared a phrase
  with a pool false positive were rewritten too. Checked against the pool (scratch script, the pool stays in
  `.tmp-session/`): no three-word sequence with a content word is shared between the v2 system texts and any of the
  286 pool claims, and the quoted examples share no content word with a relabelled claim except "before".
- **The evaluation lives under `services/`** (`python -m services.ground_truth.instrument_eval`), not `scripts/`: the
  image has no `scripts/` (S278 had to embed the R1 CLI into its ECS exec script). The ledger probe moved from
  `scripts/aa1048/r1_calibrate.py` into `r1.ledger_probe` for the same reason; `r1_calibrate.ledger_probe` delegates.
- **Every call goes through `r1.run`**: the same hard cap (gated on each call's worst case), ledger probe, identity
  check, unusable-reply rows and `**billed_fields`. No new LLM call site (the AA-927 registry is unchanged).
- **Stage**: the existing `adhoc_aa1048` (`r1.STAGE`, role `judge`), not a new `adhoc_AA-1048` spelling: the R1 ledger
  probe and every earlier R1 row use it; the run is told apart by `quality_signal.run_id`, the instrument id and the
  prompt version.
- **Pool = R1's decision record**: the `pool` step rebuilds the 286 fired claims from the R1 export files, the current
  labels (Writing Lab claim browser lines), the 5 approved relabels (to violation) and the S278 R1 votes log. Machine
  labels (source `prefilter`) never count (F14), exactly as R1's decision record: without that rule the pool had 305
  (the 19 prefilter fires). Verified: 286 = t2 168 + t9_blog 118; groups tp 71, tp_relabel 5, fp_paraphrase 135,
  fp_verbatim 30, fp_background 29, fp_other 11, fp_disputed 5 (= the panel report's table).
- **Subset**: half false positives from the three groups the panel named, in proportion to their sizes (largest
  remainder: 17 / 4 / 4 of 25), so the raw subset shares estimate the population's; half true positives, every relabel
  first, then stored violations in proportion per writer. Deterministic (claim-hash order, no randomness). The trial
  is the first N of the subset in round-robin group order (a relabel, a false positive, a stored violation).
- **Score**: FP dropped = majority cleared it; TP kept = majority flagged it; unverified counts neither way (D7). go
  only when the run finished, false positives and stored violations are both present and both bars hold; a trial
  never gives a verdict.
- **The relabels are in-sample, so they are never gated on** (review fix): the five relabelled claims shaped the B5
  patterns and always enter the subset (5 of its 25 true positives, 20% against 5/76 = 6.6% in the population).
  TP-kept, recall, precision and the verdict read the stored violations (`tp`) only (`true_positives`,
  `bars.tp_kept_on = ["tp"]`); `relabelled_in_sample` and `true_positives_with_relabels` are reported beside them.
  Example the reviewer gave: 18/20 stored kept + 5/5 relabels = 23/25 = 92% pooled, but the gate reads 18/20 = 90%.
- **Thresholds are settings** (tech lead S279): the cap, the claim limits and both bars are read from the settings
  registry (CIS 416), refreshed from the database before a paid run (`settings.ensure_fresh`, an ECS exec process has
  no refresher). The cap is also clamped to `r1.HARD_CAP_USD`.

## Changed (vs. the lane brief)

- Migration number **416**, not 412: other lanes of the wave hold CIS 412-415 (quality settings 412-413, TripPlanner
  settings 414-415). Renumber to owner main max + 1 at ship time if the batch changes (`git grep '416_'` and `'416'`
  in the file, its integration test, `compiled_defaults.json` and the seed-parity test).
- Stage `adhoc_aa1048` instead of `adhoc_AA-1048` (above).
- B3 is in v2 as written, but the $0 check does **not** support the hypothesis behind it (below).

## B3 check ($0, stored R1 votes; `instrument_eval pool` prints it)

R1 kept no raw fragments (votes only), so the check reads behaviour on the 597 stored-ok, voted claims of objects whose
source has a "Place background" block (t9 only; t2 sources have none):

| best source line of the claim | n | fired | fire rate |
|---|---|---|---|
| a Place background line | 274 | 21 | 0.077 |
| another line | 323 | 71 | 0.220 |
| covered whole by a Place background line | 101 | 3 | 0.030 |
| covered whole by another line | 67 | 6 | 0.090 |

Of the 30 verbatim misses (fp_verbatim), 8 have a Place background line as their best support. Reading: R1 does not
discount Place background lines; claims supported by them fire less, not more. The "they say nothing about what this
trip does" clause is not what drives the verbatim misses; B3 stays in v2 as an explicit rule (it costs nothing), but
the expected gain is small. The verbatim misses look like plain extraction errors (the 22 not explained by place
background), which the whole-source entailment of v2 targets.

## Tradeoffs

- Entailment v2 sends the whole source per claim (median 6.5k characters): more input tokens than v1's three lines,
  but GPT-6 Luna input is cheap and the reasoning output dominates. DRY RUN on the real pool: 50 claims x 3 votes =
  150 calls, expected 0.51 USD (cap 1.5, fits with the 2x safety), worst case 1.93 USD (the per-call gate stops before
  the cap); trial 3 claims = 9 calls, expected 0.03 USD.
- The relabels still take 5 of the 25 true-positive slots at `--limit 50`, so the gated out-of-sample TP-kept rests on
  20 stored violations (a 90% bar allows 2 lost). Kept that way: the relabels are the direct check that v2 still
  fires on the confirmed patterns, and a larger out-of-sample n costs a larger limit (a setting), not a code change.
- Proportional FP allocation gives only 4 verbatim and 4 background claims: their per-group shares are noisy. The go
  decision reads the pooled shares, which the allocation keeps unbiased.
- A setting per limit (five keys) instead of CLI literals: a migration and a MIG-4 chain for an adhoc tool, but the
  tech lead's S279 rule has no exception for adhoc runs.

## Should know

- **CI of this code PR needs CIS 416 on main**: the settings-lint test fails on a `settings.get("cis....")` key that is
  not compiled until the migration PR merges (it carries `compiled_defaults.json`). Ship the migration PR first; this
  PR is tested on the merge result.
- **Review fix in CIS 416**: the description of `cis.quality.instrument_eval_tp_keep_min_pct` now says the bar reads
  the stored violations only (the relabels are in-sample). The migration branch was also rebased on main after CIS
  412-413 merged (`compiled_defaults.json` regenerated, not hand-merged). At ship time the CIS preflight wants main max
  + 1; in this wave lane l21 holds 414-415 and another lane also drafted a 415, so the wave's CIS batch renumbers.
- **Follow-up (settings)**: a later migration sets `enforced = true` on the five keys once this code is deployed, and
  removes them from the not-enforced list in `tests/unit/test_s279_settings_seed_parity.py`.
- **Follow-up (T9 seed bug, not filed as an issue here)**: the T9 seed's SCOPE line can contradict its own itinerary
  line. Seen on the Bhutan days 7-10 piece (claim 5076b862a179): SCOPE runs National Library -> Folk Heritage ->
  Tashichhodzong -> Dochu La -> Thimphu -> Traditional School of Arts and Craft, while the itinerary line visits the
  crafts school first. The writer followed SCOPE and the S275 ruling is right against the seed; the defect is in the
  seed builder's SCOPE ordering.
- The pool file (`.tmp-session/S279/aa1048/pool-v2.jsonl`) holds real text: scratch only, uploaded to the scripts
  bucket for the ECS exec run and deleted after; the run's report holds counts, groups and hash prefixes only.
- `claim_shadow` stays on v1 (the only version with an R1 record); v2 becomes a shadow candidate only after a go and a
  full held-out R1 on v2.
