# AA-790 brand-label addendum to the tenant-voice rubric (ids and counts only)

Plan data: [`data/aa790-brand-label-addendum.json`](data/aa790-brand-label-addendum.json). Measured in S294 with
`python -m services.quality.brand_voice_kappa --fixture <fixture>` ($0: no model call, no DB). The golden fixture
holds tenant text and stays in the private store (see the [golden set changelog](aa729-golden-set-changelog.md)).

## What it is for

The tenant-voice rubric (`services/acp_content_writing/voice_rubric.py`, `voice_rubric.v1`) is deterministic and
offline. ADR 0020 D9 lets it become a repair-only gate that never holds only when it agrees with brand labels at
Cohen's kappa >= 0.6 on >= 40 labelled pairs per writer, with >= 5 labels of each class. The labels are the AA-1079
brand labels (`label.brand`, rubric `aa1079-brand-v1`: `fits` / `partly` / `off`, plus the three panel lens votes),
read strict (only `fits` passes) and lenient (`fits` or `partly` passes). Off-brand is the positive class.

## Where it stands (fixture 1.7.0)

| Writer | Brand items | Labelled | Human | Unlabelled in fixture | Pairs missing | Missing after labelling the fixture | Rubric kappa strict / lenient | Panel lens kappa (strict) |
|---|---|---|---|---|---|---|---|---|
| t2 | 68 | 20 | 0 | 48 | 20 | 0 | 0.34 / -0.11 | 0.35 |
| t9_blog | 34 | 20 | 0 | 14 | 20 | 6 | 0.00 / n/a (no lenient fail) | 0.56 |
| t9_social | 34 | 34 | 0 | 0 | 6 | 6 | 0.12 / 0.39 | 0.35 |

Class balance: strict off-brand labels t2 7, t9_blog 4 (1 short), t9_social 23; lenient off-brand labels t2 2,
t9_blog 0, t9_social 2 (each short of 5).

Reading: no writer reaches the bar, and the labels themselves are not a stable reference yet: the three panel lenses
agree with each other at kappa 0.35 to 0.56, below the 0.6 the rubric is asked to reach against them. A rubric
measured against those labels cannot be told apart from label noise, so a human anchor comes before any kappa
decision.

## The addendum (fixture 1.8.0)

1. **Panel on the existing items (0 USD, agent panel).** Brand-label the 62 brand items 1.7.0 already holds without a
   brand label: `t2-021` to `t2-068` (48) and `t9b-021` to `t9b-034` (14). Same protocol as 1.4.0: three lenses
   (voice, audience, skeptic), the pack shows the brand pack and the text only (`scripts/aa729/brand_labels.py
   export-pack`, then `apply --approved-by <tech lead>`; a disputed answer only with the tech lead's decision).
   After it: t2 68 pairs, t9_blog 34, t9_social 34.
2. **New samples (0 USD).** t9_blog and t9_social each need 6 more labelled items. Select them from stored pieces with
   `scripts/aa729/select_samples.py --ids` and `labels.py merge` (content label first, then the brand panel), with a
   preference for pieces flagged off-voice, so each writer reaches 5 lenient off-brand labels.
3. **Human anchor (tech lead, about 54 items on 1.7.0).** The tech lead confirms or overrides the panel label
   (source `tech_lead`, never overwritten by a panel label) on the items where the rubric and the panel disagree plus
   the panel's split votes: t2 15, t9_blog 10, t9_social 29 on 1.7.0. Kappa is then reported on the human-anchored
   set.
4. **Measure.** `python -m services.quality.brand_voice_kappa --fixture <1.8.0>`; the decision per writer is
   `calibrated` (repair-only gate allowed), `not_calibrated_*` or `insufficient_labels` (report only).

The paid 20-piece pilot (Done-when 3) is not part of this addendum: it needs its own USD cap (tech lead S294 D5).
