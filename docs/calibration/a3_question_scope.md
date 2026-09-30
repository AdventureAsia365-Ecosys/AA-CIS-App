# Calibration — `a3_question_foreign` + `a3_question_about_here` (AA-661 / AA-694, S203–S204, 30/09/2026)

Two questions (stage `a3_question_scope`, migration 185), asked before a People Also Ask question
counts for a Segment. State = `{question, countries}`, where countries = the Segment's own tour countries.

- `a3_question_foreign`: *Does this question name a place, landmark or region outside the countries given?*
- `a3_question_about_here`: *Does this question name a place in those countries, or an activity, custom
  or subject particular to them?* (generic questions are out, Q3)

A question stops counting when **foreign is a confident yes OR about_here is a confident no**
(`scope_dropped()` in `services/acp_contract/atom_ranking.py`).

## Sample
- **600 (countries, question) pairs** from Dev, drawn with seed 694 from every pair
  `_capped_candidates()` lands today (Segments with a tour country, transit excluded). Both
  questions per pair, 0 errors, **$0.012** (stage `adhoc_aa661`).
- Of the 600, the rule below drops **301 (50%)**: today's word-overlap landing credits these
  questions to places in other countries, or to no particular place.
- **200 of the 600** labelled (agent-proposed), then reviewed by Nghiệp.
  Raw data: `data/a3_question_scope_2026-09-30.json` (`p_foreign`, `p_here`, `label`, `review`).

## Labels
Operational meaning: *should this question's demand count for a Segment in these countries?*
**1** = keep (names a place or subject particular to those countries); **0** = drop (another
country, or could be asked about anywhere). Balance after review: 51 × 1, 149 × 0.

## Review by Nghiệp (54 rows, 30/09/2026)
- Group A: 40 random; group B: 16 rows where rule and agent label disagree (2 in both).
  Google Sheet "Jev calibration — question country scope (2026-09-30)", AdventureAsia Drive folder.
- **53 agree, 1 overturned (1.9%)**, below the 10% re-pose limit:
  - #49 Taiwan ↔ "Where is Lovers Bridge?": 0 → **1** (Lovers Bridge is in Tamsui, Taipei). The rule
    already kept it (p_foreign 0.22, p_here 0.73), so the correction improves the rule's score.
- Borderline labels confirmed: #16 "Love Bridge" (Laos, 0), #36 "giant Buddha" (Laos, 0), #31
  "Which Chinese city is closest to Nepal?" (Nepal, 1), #54 Manas National Park (Bhutan, 1).

## Result (200 labelled, after review)
| Rule | Dropped | Wrong drops | Bad questions caught |
|---|---|---|---|
| **foreign ≥ 0.95 OR here ≤ 0.20** | 133 | **1** (99.2%) | 132 / 149 |
| foreign ≥ 0.95 OR here ≤ 0.15 | 130 | 1 | 129 / 149 |
| foreign ≥ 0.90 OR here ≤ 0.20 | 138 | 4 | 134 / 149 |
| foreign ≥ 0.95 OR here ≤ 0.30 | 137 | 2 | 132 / 149 |

Each question alone: foreign ≥ 0.95 drops 116 (1 wrong); here ≤ 0.20 drops 133 (1 wrong). It is the
same wrong row in both.

**The one wrong drop:** Bhutan + Sri Lanka ↔ "What is Manas National Park famous for?" (p_foreign
0.96, p_here 0.09). Manas spans the India–Bhutan border; Jev reads it as India. Border parks shared
with a neighbour are the known weak spot.

## Decision
- [x] Nghiệp reviewed 54 rows; overturned **1 / 54** (< 10%).
- [x] Enforce `a3_question_foreign` with **accept floor 0.95** (a confident yes drops), and
  `a3_question_about_here` with **reject ceiling 0.20** (a confident no drops), `calibration_ref
  docs/calibration/a3_question_scope.md`.
- [ ] After the first enforced atom ranking: read the dropped questions on /admin/decisions
  (Verdicts → question, zone Accept for foreign / Reject for about_here) and look for border places
  like Manas. If a pattern shows, raise the foreign floor to 0.97 first.
