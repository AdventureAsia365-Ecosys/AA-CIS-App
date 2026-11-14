# AA-1048 — the R1 firing run on unlabelled claims (lane l13, S281)

This lane follows `AA-1048-two-step.md`. Runbook steps 1 and 2 of the fresh pool were done at $0, and then step 3 stopped:
`scripts/aa1048/r1_calibrate.py` builds items only from labelled claims (`r1.items_from`). The eligible train claims
hold 126 labels and 8 violations, so a fire there could never give the 20 violations and 25 false positives that a
50-claim subset needs. The tech lead's option (a) was a firing path that does not need labels: fire R1 on a capped
sample of unlabelled eligible claims, then let the panel label only what R1 fired. This lane builds that path and
makes no paid call.

## Decisions

- **A module in `services/`, not a script.** `services/ground_truth/r1_fire.py` has its own CLI. The worker image has
  no `scripts/` folder, so a wrapper only has to embed the data (the eligible file, about 0.3 MB with lzma) and call
  `r1_fire.main`. There is no second copy of the code in the wrapper.
- **One command samples and fires.** The sample is drawn in the same process that fires, from the full eligible file.
  The manifest's sha256 check stays whole, and so does the out-of-sample check against the manifest. The live run
  re-reads the settings, so its sample size is the approved one. `--expect-sample <digest>` refuses a live run whose
  sample differs from the DRY RUN's.
- **Same instrument, same machinery.** The instrument is `fresh_pool.FIRE_CANDIDATE` in extraction mode, prompt v1,
  which is `ev.R1_INSTRUMENT`. Each call goes through `r1.gateway_call`. Each reply is logged through `r1._log`
  (`**billed_fields`, prompt version), so the call-site registry needs no new entry. The run uses `r1.billed_cost`,
  `r1.worst_call_cost`, `r1.Budget`, the ledger probe and `r1.flags_from_reply`. The loop is a small class (`_Fire`)
  rather than `r1.run`, because the report needs a call id and the provider request id per call. `r1.run` stays
  unchanged, so the r1.py sha pinned by the old wrapper still matches.
- **Sampling.** The run uses `fresh_pool.pick` (writer-proportional by largest remainder, salted-hash order) with the
  salt `aa1048-fire:<eligible sha256[:16]>`. That salt differs from the one `fresh_pool sample` uses, so the order of
  the second draw is not tied to the order of the first.
- **Out of sample is refused in every mode, DRY RUN included.** The manifest must:
  - be `kind fresh_eligible` and match the file (sha256);
  - list every run the two-step prompts were shaped on (`two_step.shaped_on`);
  - keep the shaping pool out, either by listing it in `excluded_pools` or by being a train-split manifest whose every
    claim is train (the same rule as `two_step.fresh_check`).
  No claim may sit on an excluded piece or off the manifest's split. A DRY RUN of an in-sample file has no use, so it
  is refused as well, rather than reported as in `two_step`.
- **Settings.**
  - The sample size is the new `cis.quality.claim_fire_sample_max` (CIS 426, default 600, 1..3000, maker-checker,
    `enforced = false` until this code is deployed).
  - The cap is the existing `cis.spend.instrument_eval_cap_usd` (CIS 417, never above `r1.HARD_CAP_USD`): `--cap`
    defaults to it and may not pass it.
  - The trial size is the existing `cis.spend.instrument_eval_trial_claims`.
  - No literal and no env var was added.
- **Trial and `--measured`.** The model plan assumes 6,000 reasoning tokens per reply, which over-estimates about 16x,
  so a 600-claim plan does not fit the cap on the model estimate. `--trial N` fires the first N claims of the same
  sample. Its report (kind `r1_fire`, mode live, not stopped) prices the full run through `--measured`: the trial's
  charged cost per call, scaled by prompt length. The live gate still charges every call's worst case.
- **The report holds no text.** It carries claim hashes (full 64 hex), booleans, counts, call ids, provider request
  ids, object keys (piece sha prefix and field name) and charges. `votes` has the shape that
  `fresh_pool.read_fire_votes` reads: `{instrument id: {claim hash: [...]}}`.
- **Ledger ids.** Each ledger row's `quality_signal` carries the following, and the report lists each call with the same
  call id:
  - `run_id`;
  - `call_id` (`<run_id>:<n>`);
  - `purpose: fresh_pool_fire`;
  - `fire_kind`.

  After the run, the report counts the run's rows (`LEDGER_ROWS_SQL`, read only). The key is `fire_kind`, not
  `run_kind`: the two-step stop rule reads `run_kind`, and a fire row must never look like a two-step run.
- **`fresh_pool sample` checks the fire report.** It refuses a fire report that is a dry run or a trial, or that fired
  another eligible file. The sample manifest records `votes_sha256`, `votes_run` and `votes_partial`. Other votes
  files (the r1_calibrate report, an R1 log) are read as before.

## Changed

- New: `services/ground_truth/r1_fire.py` and `tests/unit/test_aa1048_r1_fire.py`.
- `services/ground_truth/fresh_pool.py`: `FIRE_REPORT_KIND`, `fire_report_check`, the sample manifest fields above,
  and the docstrings.
- `CONTEXT.md`: one sentence in the two-step glossary entry.
- Migration CIS 426, in its own PR (MIG-4): the setting row, `compiled_defaults.json`, the seed-parity list and its
  integration test.

## Tradeoffs

- **Claims, not objects.** The sample is drawn per claim, as asked. Extraction makes one call per object (piece +
  field), so the cost follows the objects the sample touches, not the claim count. Sampling objects would give more
  claims per dollar but a different, less even spread over writers. The plan prints both counts (`sample.objects`,
  `plan.instruments[0].calls`).
- **The trial claims are fired again in the full run.** This keeps the full report covering the whole sample by
  itself. The trial costs a few calls.
- **A stopped full run is still accepted by `fresh_pool sample`.** It is recorded as `votes_partial`, because its votes
  were paid for. An unverified majority is simply not fired.

## Should know

- Known red before the chain: until CIS 426 has merged, the unit tests that read the new setting fail with KeyError,
  and so does settings-lint rule 2 ("a `settings.get` literal must be a compiled key"). The same was true of the
  two-step PR before CIS 423 merged.
- Follow-up: a later migration marks `cis.quality.claim_fire_sample_max` enforced, together with the CIS 417 and 423
  keys, once this code is on Dev.
- The live order is: DRY RUN, then a trial of 3 claims (live), then a DRY RUN with `--measured`, then the full live
  run, then `fresh_pool sample`. The wrapper builder that embeds the eligible file is a scratch tool outside the repo.
