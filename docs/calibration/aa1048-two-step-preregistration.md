# Pre-registration — two-step claim instrument v5 (AA-1048, Quality W2, S281)

Registered 15/11/2026, before any data of the run exists. The machine-readable copy is
`services/ground_truth/preregistration/aa1048_two_step_v5.json`; `python -m services.ground_truth.two_step run --live`
reads only that committed file and refuses while the settings, prompt texts, shaping runs and pools, candidate or vote
count in force differ from it. Root ADR 0020 W2.

## Question

Does the two-step claim instrument drop the false positives of the R1 fired claims and keep the stored violations, on a
pool that is out of sample for both steps?

- **Step 1** (`aa1048-r1-v5c@50704a977e52`): sorts each fired sentence as a *restatement* of the source (paraphrase,
  one-step reading, tone, text about the text, background about a place the source names) or a *new claim* (a new
  number, name, time, order, means of travel, service, meal character, guide quality, group or crowd condition,
  activity, view, access, pace, effort, comfort, safety or suitability), and lists the new specifics.
- **Step 2** (`aa1048-r1-v5j@0c3529d50a55`): for a sentence step 1 sent on, is each listed specific supported by the
  whole source?

## Why a new design

Three one-prompt rounds (v2, v3, v4) were all no_go, and the last two traded one side for the other:

| Round | False positives dropped | Stored violations kept |
|---|---|---|
| v2 | 48 % | 90 % |
| v3 | 68 % | 85 % |
| v4 | 48 % | 85 % |

v4's guard against new specifics made paraphrase fire again. One prompt was asked two questions at once: is there a new
specific, and is it supported? The two-step design asks them separately. Prompt patching on the old pool stops here.

## Hypotheses

- **H1.** Step 1 sends on almost every stored violation, because a violation adds a specific by definition.
- **H2.** Most paraphrase and verbatim false positives stop at step 1, because they add no specific.
- **H3.** Step 2 clears most of the remaining background false positives, because it judges a short list of specifics
  with the place-background rule, not the whole sentence.

## Pool

The pool is fresh: R1-fired claims on pieces outside the S279 pool and outside every claim of the runs the steps were
shaped on (3018008d, 33835b87, 2977dd78). R1 here is gpt-6-luna-openai, extraction mode, prompt v1, the instrument
whose false positives are measured. The default source is the train split, because the split is frozen per tour, so a
train piece shares no tour with the held-out S279 pool.

The labels come from the agent panel sheet (`docs/calibration/aa1048-two-step-panel-brief.md`, three lenses, majority).
A stored label counts only when it agrees with the panel majority. A disagreement, a split panel or an "unsure"
majority leaves the claim out. The builder records the excluded runs and pieces, the salt and the file digests
(`services/ground_truth/fresh_pool.py`). A live run needs `--fresh` (`--exclude` alone drops claims of run reports,
not the pool the panel read). The runner checks that:

- the manifest belongs to the pool file;
- it names every shaping run, and no claim sits on an excluded piece;
- the S279 pool (digest `487bf4847b6136fe`, pinned in the prompt versions and in this registration) is in the
  manifest's excluded pools, or the pool is a train-split pool (manifest split `train` and every claim `train`).

## Subset and score

- **Subset.** This is `instrument_eval.select`:
  - half false positives: paraphrase, verbatim and background, in proportion to the pool;
  - half stored violations, in proportion per writer;
  - claim-hash order within each group.
- **Votes.** Each step casts 3 votes (2 agreeing decide) on the pinned model and connection: stage `adhoc_aa1048`,
  role `judge`.
- **Verdict per claim.** A claim is cleared when the step-1 majority is "restatement". Otherwise its verdict is the
  step-2 majority:
  - unsupported: flagged;
  - supported: cleared;
  - no majority: unverified, which counts neither as dropped nor as kept.

  An unverified step 1 never clears a claim.

## Bars (all needed for go)

| Bar | Value | Setting |
|---|---|---|
| False positives dropped (overall) | >= 60 % | `cis.quality.instrument_eval_fp_drop_min_pct` |
| Stored violations kept (overall) | >= 90 % | `cis.quality.instrument_eval_tp_keep_min_pct` |
| Stored violations step 1 sends on | >= 95 % | `cis.quality.claim_two_step_route_tp_min_pct` |
| Violations step 2 flags (of those it judges) | >= 95 % | `cis.quality.claim_two_step_judge_tp_keep_min_pct` |
| False positives step 2 clears (of those it judges) | >= 40 % | `cis.quality.claim_two_step_judge_fp_drop_min_pct` |

- **The step-2 false-positive bar** holds when no false positive reaches step 2.
- **The step-1 answers** of the panel (restatement / new claim) are reported against step 1 (`step1_vs_panel`) and are
  never gated.
- **Cost.** The run's cap and claim limits are `cis.spend.instrument_eval_*` (CIS 417).

## Stop rule

The evaluation is one gated run (after a trial). A no_go is read at $0 with the stored reasons (`--with-reasons`) and
is never re-run on the same pool with a changed prompt.

The runner enforces it. Every ledger row of a run carries the registration id, the run kind (`trial` or `full`) and the
pool digest. A gated run (`--limit`) is refused when `shared.llm_call_log` already holds a gated run of this
registration (any pool), or of the same step-1 prompt on the same pool, or when the ledger cannot be read. A stopped
gated run counts, because its votes were seen. A trial may repeat: its verdict is always `trial`. A trial does not stop
the gated run on a step bar, because 3 claims hold one or two violations and a 95 % bar on them is noise. A go is narrow evidence. It leads to a proposed full held-out
R1 on the two-step instrument (the tech lead's spend yes) before any shadow or pin.
