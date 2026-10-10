# Calibration — `a1_claim_supported` (AA-661 / AA-691, S204, 30/09/2026)

Question (stage `s1_grounding`, migration 186), asked per sentence of the S1 master rewrite:
> *A travel writer rewrote a tour from the source text. Is every fact in this sentence stated or
> clearly implied by the source?* — state = `{source, sentence}`

## Sample
- The 121 published master tours → 7,911 sentence units (subtitle, summary, highlights, itinerary
  bodies). The deterministic numeric check hit **337** (302 after the source-number normalisation
  below) in 90 → 87 tours.
- **200 sentences** (seed 691): 60 numeric hits + 140 others. Shadow pass through the deployed
  `decide()` (stage `adhoc_aa691`): 0 errors, ~224 ms mean, sources up to 29k chars, cost ≈ $0.
- Raw data: `data/a1_claim_supported_2026-09-30.json` (sentence, p, numeric_hit, label, note).

## Labels — agent-proposed (not independent human labels)
**1** = every fact is in the source (paraphrase, unit conversion, sums allowed; mood/colour about
something the source names counts). **0** = the sentence states a fact the source does not give:
place, activity, number, meal, service, timing, record, history. Balance: 120 × 1, 80 × 0.
Review: Nghiệp checks 59 rows (40 random + 23 agent-vs-Jev disagreements, 4 in both) in the Google
Sheet "Jev calibration — master content grounding (2026-09-30)", AdventureAsia Drive folder.

## Result (agent labels)
| Zone | n | Correct | Note |
|---|---|---|---|
| reject p ≤ 0.20, all | 62 | 60 (0.968) | |
| reject p ≤ 0.20, **non-numeric only** | 13 | 11 (**0.85**) | both misses are table-shaped sources (`Meals included: Breakfast`, `Elevation Gain + 500 m`) |
| reject p ≤ 0.10, non-numeric only | 5 | 3 | |
| **accept p ≥ 0.90**, all | 50 | **50 (1.000)** | |
| accept p ≥ 0.85, all | 62 | 60 | |

The reject side only matters for sentences without a number (numbers are caught deterministically),
and there it is **below 95%** → `UNSUPPORTED_CLAIM` stays **shadow** (notes for the reviewer only).

The accept side is clean at 0.90 → Jev may **clear a deterministic numeric hit** (unit conversion,
sum, odd source format) when it confidently says the sentence is supported.

## Deterministic numeric check — false hits
7 of the 60 numeric hits (12%) were supported. 5 were source formats, now handled in code
(`grounding.source_number_parts`): numbers glued to letters (`3h260km`, `2h30m-3h96km`), travel
times in other units (`1h30m` = 90-minute, `2hr 30min` = 2.5-hour), clock formats (`12.30` =
`12:30`). The remaining 2: a typo in the source (`1. 5 hours`) and "14th century" inferred from
"Ming dynasty" (both have Jev p ≥ 0.60; one ≥ 0.90).

## Review result (Nghiệp, 30/09/2026)
- Reviewed **59 rows** (40 random + 23 disagreements, 4 in both) in the Google Sheet
  "Jev calibration — master content grounding (2026-09-30)" (AdventureAsia Drive folder).
- **54 agree, 5 overturned (8.5%)**, below the 10% re-pose limit. All 5 were agent label 1 → **0**,
  i.e. Nghiệp sided with Jev (p 0.24–0.37):
  - #105 "The entire journey spans approximately 31 hours…": the source ties 31 h to the train from Xi'an, not "the entire journey";
  - #128 "Hotel checkout and airport transfer.": the source says "departure", not an airport transfer;
  - #135 "…reaches Siachen Base Camp on day six, and returns southward via Khardung La.": route details not in the quoted source;
  - #151 "The afternoon continues over mountain passes into Bumthang's valleys.": the source only says "drive to Tang valley";
  - #174 "Check-in … and acclimatize to the altitude.": the source says nothing about acclimatising.
- Borderline notes confirmed: #43 (4 passes "each above 4,900m" → 0), #75 (Sekong River → 0),
  #34 ("Elevation Gain + 500 m" → 1, Jev wrong), #191 (22 towers → 1, Jev wrong).
- Labels in `data/…json` carry the 5 corrections (`review`).

## Result after review
| Zone | n | Correct |
|---|---|---|
| accept p ≥ 0.90, all | 50 | **50 (1.000)** |
| accept p ≥ 0.85, all | 62 | 60 |
| reject p ≤ 0.30, non-numeric | 21 | 19 (0.905) — misses #62, #144, both table-shaped sources |
| reject p ≤ 0.20, non-numeric | 13 | 11 (0.846) |

## Decision
- [x] Nghiệp overturned 5 / 59 (8.5% < 10%).
- [x] **Enforce `a1_claim_supported`: accept_floor 0.90, reject_ceiling NULL**, threshold v1,
  `calibration_ref docs/calibration/a1_claim_supported.md` (Dev, 30/09/2026). Effect: a confident
  "supported" clears a deterministic numeric hit; `UNSUPPORTED_CLAIM` is never raised.
- [ ] Reject side: below 95% on non-numeric sentences (both misses are table-shaped sources). Collect
  low-p non-numeric sentences from the rerun's decision_log and re-pose with the table lesson before
  any reject floor.


## Threshold v2 — S223 (10/10/2026, AA-756)

accept_floor 0.90 → **0.85**. Sample (200): precision 100% → 97%, supported sentences cleared 43% → 52%. Live accept rate: 31.1% → 41.9%. Reject ceiling still NULL: reject ≤ 0.30 would hold 97% precision and catch 81% of unsupported sentences on the sample, but flag 19.5% of live sentences — measured offline on the next wave from logged probabilities before any change (Nghiệp, S223).

Recomputed from the labelled sample in `data/` (no new labels). Approved by Nghiệp; applied on Dev through `PUT /admin/decisions/questions/{key}` (threshold_version 2). Before-values snapshot: `s3://aa-cis-bronze-005097885195/scripts/restore/s223_aa756_decision_question_before.json`. Audit: `docs/audits/2026-10-10-S223-jev-audit.html` (root repo). Spot-check 30 new decisions per question after the next recompute.
