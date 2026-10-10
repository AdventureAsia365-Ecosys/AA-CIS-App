# Calibration — `a3_demand_belongs` (AA-661 / AA-694 A3-6, S204, 30/09/2026)

Question (stage `a3_demand`, migration 188):
> *Is someone searching this keyword looking for this particular moment of a trip: this place, something
> at it, or this activity here?* — state = `{keyword, moment}`

Asked in `run_atom_ranking()` on each Segment's best claimable keyword per market (`demand_candidates()`,
same (fit, volume) order `compute_demand()` used); a confident no moves to the next candidate, up to 3.

## Sample
- Dev: 1,208 Segments claim demand; 1,547 distinct (moment, rank-1 keyword) pairs, 2,307 rank-2/3 pairs.
- **200 pairs** (seed 696): 100 rank-1 + 100 rank-2/3. Shadow pass (`adhoc_aa694`), 0 errors.
- Raw data: `data/a3_demand_belongs_2026-09-30.json`.

## Labels — agent-proposed (not independent human labels)
**1** = the keyword names this place (or a spelling of it), something at it, or doing this activity here.
**0** = a wider area around the place (the town or country it sits in), a different place, only the kind
of thing it is ("monastery" 90,500, "bazaar" 49,500), or the start/end of a route moment.
**Only 33 of 100 rank-1 claims are right** (63 / 200 overall): "fisherman's village" (40,500) is claimed
by Humayun's Tomb, Devon's Falls and a Himalayan aid post; "ger camp Mongolia" by Everest Base Camp.

## Result
| Zone | n | Correct | Bad claims caught |
|---|---|---|---|
| reject p ≤ 0.30 | 75 | 75 | 75 / 137 |
| **reject p ≤ 0.40** (proposed) | 88 | **88 (1.000)** | 88 / 137 |
| reject p ≤ 0.50 | 99 | 99 | 99 / 137 |

Lowest-p good claim: 0.51 (#61 downtown Jeju night out ↔ "night life in Jeju"). Proposed floor **0.40**.
Known accept-side misses (irrelevant to a reject-only gate): #130 Dochu La ↔ "dolma la pass" 0.77.

## Decision
- [x] Nghiệp reviewed all 70 rows of the Google Sheet "Jev calibration — demand ownership A3-6 (2026-09-30)":
  **overturned 0 / 70**. Two borderline rows annotated, labels kept: #65 Angkor small circuit ↔ "angkor wat
  ticket" stays 1 (Angkor Wat is on the circuit); #5 Phuket free time ↔ "Phuket hotels" stays 0 (lodging intent).
- [x] **Enforce `a3_demand_belongs`: reject_ceiling 0.40**, no accept floor (Dev, S205, 30/09/2026).
  Reject ≤ 0.40: 88 / 88 correct; catches 88 of 137 bad claims (64%).


## Threshold v2 — S223 (10/10/2026, AA-756)

reject_ceiling 0.40 → **0.70**. Sample (200, 30/09): precision 100% → 98%, negatives caught 64% → 85%. Live act rate (30 days, latest per subject): 63.2% → 77.9%.

Recomputed from the labelled sample in `data/` (no new labels). Approved by Nghiệp; applied on Dev through `PUT /admin/decisions/questions/{key}` (threshold_version 2). Before-values snapshot: `s3://aa-cis-bronze-005097885195/scripts/restore/s223_aa756_decision_question_before.json`. Audit: `docs/audits/2026-10-10-S223-jev-audit.html` (root repo). Spot-check 30 new decisions per question after the next recompute.
