# AA-1048 — the two-step claim instrument, a fresh pool and its pre-registration (lane l8, S281)

This lane follows `AA-1048-instrument-v4.md`. The three one-prompt rounds were all no_go:
- v2: 48 / 90;
- v3: 68 / 85;
- v4: 48 / 85 (run 2977dd78).

The bars are false positives dropped >= 60 % and stored violations kept >= 90 %. The last two rounds traded one side
for the other, so prompt patching stopped (root ADR 0020 W2). This lane builds the next design at $0 and makes no
paid call:
- (a) a two-step instrument;
- (b) a fresh pool builder with a labelling sheet;
- (c) a pre-registration file with per-step bars and a gated runner.

## Decisions

- **Two registered step prompts, not a new mode.**
  - `v5c` (step `classify`) asks whether the sentence is a restatement or a new claim, and lists the new specifics
    (schema `claim_restatement`).
  - `v5j` (step `judge`) asks whether each listed specific is supported (the entailment schema). Its user prompt is
    v2's user prompt plus a `SPECIFICS TO CHECK` list.
  - Both are `PromptVersion` entries with a new `step` field. That field is not in the hash, so v1-v4 keep their
    ledger tags, and the tests pin them.
  - A step version is entailment-only. `r1.instruments` refuses it, so it can never run as a one-prompt instrument.
  - Each step is its own instrument (`...:entailment#v5c` / `#v5j`) with its own tag in every ledger row.
- **Calls reuse r1's machinery.** `two_step.run` calls `r1.gateway_call` and logs through `r1._log`
  (`**billed_fields`, the step's prompt version), so the call-site registry (AA-927) needs no new entry. It also reuses
  `r1.billed_cost`, `r1.worst_call_cost`, `r1.Budget` (one hard budget for both steps) and the ledger probe.
- **Routing.** A claim is cleared only when the step-1 majority is "restatement". A new claim, or a step 1 with no
  majority, goes to step 2: recall first, since a lost violation costs more than an extra check. Step 2 receives the
  specifics of the step-1 votes that said "new claim", merged once each. When there are none, it judges the whole
  sentence.
- **Scoring reuses `instrument_eval.score`.** The two-step verdict per claim is fed to it as a single vote. The step
  record adds three things:
  - step 1's sent-on and restated counts per group;
  - step 2's flagged, cleared and unverified counts per group;
  - the three step bars on the stored violations and false positives.

  The verdict is go only when the overall verdict is go and every step bar holds.
- **Per-step bars are settings (CIS 430).**
  - `route_tp` defaults to 95. The step-1 losses compound with step 2's, so 95 x 95 still clears 90.
  - `judge_tp_keep` defaults to 95.
  - `judge_fp_drop` defaults to 40. It holds when no false positive reached step 2.

  The overall bars and the cap and claim limits stay the CIS 417 keys. The fresh sheet's size is a fourth setting
  (`cis.quality.claim_fresh_pool_sample_max`, 150).
- **The pre-registration file is checked, not only written.** `two_step run --live` refuses unless the settings bars,
  both prompt tags, the candidate, the vote count and the stage equal the file. A test asserts that the settings'
  compiled defaults equal the registered bars, so a fresh Dev matches it. The DRY RUN reports any differences.
- **Out of sample by construction, then checked again.**
  - `fresh_pool eligible` keeps out every piece of an excluded pool (the S279 pool: its whole false-positive side
    shaped v2) and the piece of every claim of an excluded run report. A prefix it cannot locate is refused.
  - Its default split is `train`. The split is frozen per tour, so a train piece shares no tour, and no sentences, with
    the held-out S279 pool.
  - The runner refuses a live run unless one of these holds:
    - the fresh manifest is this pool file's (sha256), names all three shaping runs and puts no pool claim on an
      excluded piece;
    - or `--exclude` names the shaping run reports (an older pool).
- **The labels need the panel and the stored label to agree.** The sheet has four answers per row (label, ok reason,
  step-1 class, reason), and every row is checked (the S272 placeholder lesson; rows are addressed by number, not
  hash). The panel majority decides. A stored label counts only when it agrees; a disagreement, a split panel or an
  "unsure" majority leaves the claim out and is counted. The panel's step-1 answers are reported against step 1
  (`step1_vs_panel`) and never gated.
- **Cost estimate.**
  - The model plan prices step 2 as if every claim reached it (an upper bound).
  - `--measured <two-step report>` prices the full run from the trial: each step's measured cost per call, scaled by
    prompt length. The report keeps `by_step` calls, charged USD and mean prompt characters for this.
  - The live gate still charges every call's worst case.
- **Invented examples only.** The step texts use Morvane, Quell, Tasska and Ilbern; the brief uses Brask, Olme and
  Varek. The S281 leak check (scratch, the v2-v4 rule) finds no three-word sequence with a content word shared with any
  of the 286 S279 pool claims. The first draft shared common phrases with 9 claims and was reworded to 0.

- **Review fixes (S281 fix pass).** Two review findings were real and are fixed in code:
  - *Out of sample for the pool, not only the runs.* A live run now needs `--fresh`; `--exclude` alone only drops
    the claims of the shaping run reports and is refused for a live run (it stays for a DRY RUN reading of an older
    pool). The S279 pool's digest (`sha256[:16]` of the pool file, the value `fresh_pool eligible --exclude-pool`
    writes) is pinned as `PromptVersion.shaped_on_pools` on v5c / v5j and in the pre-registration
    (`shaped_on_pools`, compared with the code like `shaped_on`). `fresh_check` refuses unless the manifest's
    `excluded_pools` holds it, or the pool is a train-split pool: manifest split `train` AND every pool claim
    `train` (the split is frozen per tour; the S279 pool is all held-out). With `--fresh`, the shaping runs must be
    in the manifest itself (piece level); `--exclude` runs are reported as `cli_excluded_runs` and no longer count.
  - *The stop rule is code.* Every ledger row of a two-step run carries `prereg`, `run_kind` (`trial` / `full`)
    and `pool` (the pool file digest) in its quality signal. Before a gated (`--limit`) live run, `prior_runs`
    reads `shared.llm_call_log` (stage `adhoc_aa1048`, issue AA-1048, rows with a run kind) and `stop_rule`
    refuses when a gated run of this pre-registration exists on any pool, or a gated run of the same step-1 prompt
    on the same pool under another id (a re-roll), or the ledger cannot be read (fail closed). A stopped gated run
    counts. A live run reads only the committed pre-registration file (`--preregistration` is DRY RUN only), so the
    id cannot be changed in a scratch copy.
  - *Not taken:* stopping the gated run on a trial's step bar. A 3-claim trial holds one or two violations; a 95 %
    bar on them is noise, and it would change the registered protocol. The trial stays a plumbing check.

## Changed (vs. the lane brief)

- The brief's "hard cap read from settings" reuses `cis.spend.instrument_eval_cap_usd` (and the claim and trial
  limits) instead of a new key. The two-step run is the same kind of evaluation, and one cap keeps the Settings page
  simple.
- The fresh pool cannot be fired at $0. The S279 held-out R1 votes are all used, so new fired claims need an R1 run on
  new pieces. `fresh_pool eligible --labels` prints that run's plan. The run itself is the existing
  `scripts/aa1048/r1_calibrate.py` (extraction, gpt-6-luna-openai, `--split train`, paid, its own yes), not a new paid
  path.

## Tradeoffs

- **Step 2 sees only step 1's specifics.** If step 1 lists the wrong specific of a violating sentence, step 2 may clear
  it. The step-1 prompt asks it to list every specific and to answer new_claim when unsure. The route bar and the
  overall TP bar measure the loss.
- **More calls.** At most twice the calls of a one-prompt run (300 for 50 claims). Using v3/v4's measured cost per
  call (about 0.0002 USD), that is under 0.07 USD. The model plan is far higher, because it assumes 6,000 reasoning
  tokens.
- **Panel labels on train pieces.** The labels on the train side were never adjudicated by the tech lead as the
  held-out ones were. The agreement rule (panel and stored label) and the three-lens majority stand in for that.
  Disagreements are counted for the tech lead.

## Should know

- **Migration.** CIS 430 is a contract file: four config rows, `enforced = false`. It ships first, in the wave's CIS
  migration batch (MIG-4). This code PR reads the keys and needs 430 applied. Its own CI is red on the settings-lint
  check until the migration PR has merged, because the code reads keys that are not yet compiled.
- **Follow-up.** A later migration should mark the four CIS 430 keys and the five CIS 417 keys enforced once this code
  is deployed (417's code has been live since S279).
- **Order of the live steps** (runbook in root `.tmp-session/S281/aa1048-two-step/`):
  1. export claims;
  2. `eligible`;
  3. the paid R1 firing run;
  4. `sample`;
  5. the agent panel fills the sheet;
  6. `import`;
  7. the two-step DRY RUN;
  8. the trial;
  9. one gated run.
- **Stop-rule residual risk.** The record is the ledger row. If a ledger write failed for every call of a gated run,
  that run would leave no record and a second gated run would pass. The gateway writes the row on the call path, so
  this needs a ledger outage during a paid run; the run report (`run_kind`, `stop_rule`) is the second record.
- **Privacy.** The pool, candidates, sheets, sources and any `--with-reasons` report hold real or model text and stay
  in `.tmp-session/`. Outputs are refused inside a git work tree unless ignored there (`scripts/aa1048/outpath.py`).
  Only counts go to the root audit.
