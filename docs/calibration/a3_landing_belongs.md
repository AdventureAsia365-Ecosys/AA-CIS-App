# Calibration — `a3_landing_belongs` (AA-661 / AA-694 A3-5, S204, 30/09/2026)

Question (stage `a3_question_landing`, migration 187), Ms. Thư's ticket 05 wording:
> *Would someone searching this query be well served by an article about this moment on a guided trip?*
> state = `{query, moment}`, moment = "<Segment canonical place> — <canonical action>"

Asked once per (moment, candidate question) **before** landing, after the country-scope filter: every
atom of a Segment shares its place + action, so the Verdict does not depend on which atom the question
would land on.

## Sample
- Dev: the ranked Segments (transit / unnamed excluded) × `_capped_candidates()` → **11,143** (moment,
  question) pairs; the enforced country scope removes 1,117 of 2,319 (scope, question) pairs → **7,402**
  pairs that reach landing today.
- **200 of those 7,402** (seed 695), shadow pass (stage `adhoc_aa694`): 0 errors, **$0.003**.
  A full pass over 7,402 pairs ≈ $0.12.
- Raw data: `data/a3_landing_belongs_2026-09-30.json`.

## Labels — agent-proposed (not independent human labels)
Meaning written first (Ms. Thư's 51% lesson; Q5): **1** = the question asks about this place (or
something at it) or about doing this activity here, so an article about the moment answers it.
**0** = it asks about a different place or thing — even one of the same kind (another temple, another
pass) — or a topic the moment does not cover (airport logistics on a bar crawl, a country-level fact on
one monastery).

**Only 28 of 200 (14%) are good landings.** The shortlist needs one shared word, so "What is the story
behind Tiger's Nest monastery?" is a candidate for every Segment whose place contains "monastery".
This is how `questions_count` is inflated today (design A3-5).

## Result
| Zone | n | Correct | Bad landings caught |
|---|---|---|---|
| reject p ≤ 0.30 | 125 | **125 (1.000)** | 125 / 172 |
| **reject p ≤ 0.35** (proposed) | ~135 | 1.000 | ~78% |
| reject p ≤ 0.40 | 140 | **140 (1.000)** | 140 / 172 |
| reject p ≤ 0.50 | 155 | 150 (0.968) | 150 / 172 |
| accept p ≥ 0.80 | 14 | 12 | — |

Lowest-p good landing: 0.41 (#10 Jangothang ↔ "altitude of Jomolhari Base Camp"). Proposed floor
**0.35**, below every observed false reject (same rule as `a3_keyword_belongs`). The accept side is not
used — this question only removes.

Known Jev misses on the accept side (do not matter for a reject-only gate): #162 Xi'an City Wall ↔
"time needed at Mutianyu Great Wall" p=0.87; #140 Jeju Olle Trail ↔ "what can you do in Jeju City" 0.80.

## Review result (Nghiệp, 30/09/2026)
- Reviewed **62 rows** (40 random + 26 disagreements) in the Google Sheet "Jev calibration — PAA landing
  (2026-09-30)". **61 agree, 1 overturned (1.6%)**: #62 Pak Ou Caves — slow-boat on the Mekong ↔ "Which
  country is the Mekong River in?" 0 → **1** (p = 0.36).
- That row is now the lowest-p good landing (0.36), so the proposed 0.35 floor would sit 0.01 below it.

## Decision
- [x] Overturned 1 / 62 (< 10%).
- [x] **Enforce `a3_landing_belongs`: reject_ceiling 0.30**, no accept floor, threshold v1 (Dev, 30/09/2026).
  Reject ≤ 0.30: 125 / 125 correct; catches 125 of 171 bad landings (73%). 0.30 keeps a margin below
  the lowest good landing (0.36).


## Threshold v2 — S223 (10/10/2026, AA-756)

reject_ceiling 0.30 → **0.40**. Sample (200): precision 100% → 99%, negatives caught 73% → 81%. Live act rate: 67.1% → 75.3%.

Recomputed from the labelled sample in `data/` (no new labels). Approved by Nghiệp; applied on Dev through `PUT /admin/decisions/questions/{key}` (threshold_version 2). Before-values snapshot: `s3://aa-cis-bronze-005097885195/scripts/restore/s223_aa756_decision_question_before.json`. Audit: `docs/audits/2026-10-10-S223-jev-audit.html` (root repo). Spot-check 30 new decisions per question after the next recompute.
