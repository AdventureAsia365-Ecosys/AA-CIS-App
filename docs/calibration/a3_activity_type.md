# Calibration — `a3_activity_type` (AA-694 A3-2)

**Question (choice, migration 190 wording):** *Is this moment of a trip something a travel writer could write about
at this place, or trip logistics?* — `transit` = logistics (getting between places, check-in/out, a plain overnight or
meal, free time, briefings/meetings/welcome-farewell gatherings); `experience` = something to do or see there,
including a distinctive stay or meal and a journey that is itself the activity. State = `{moment}`.

Asked in `resolve_exclusions()` (`atom_ranking.py`) only for Segments where `classify_exclusion()` (transit) and the
member atoms' `activity_type` (typed majority) disagree. A confident pick (confidence ≥ accept floor, enforce only)
decides the transit exclusion for Score, PAA landing and Route.

## Wording fix before calibrating (migration 190)
Migration 189's criteria listed meals and stays under `experience`, but the rule's `transit` (ported from Ms. Thư's
`transit-verbs.toml`, ADR 0019/0020) means "not something to write about" and deliberately includes the frame of the
trip: plain meals, plain overnights, free time, briefings. Jev therefore picked `experience` for "stay overnight",
"eat lunch", "attend welcome meeting". Reworded in 190; 108 of 386 earlier picks flipped experience → transit.

## Sample
- Dev, 30/09/2026: 400 disputed Segments / 400 moments (of 3,344 Segments). 302 rule-transit vs atoms-not;
  98 rule-not vs atoms-transit.
- **160 moments** (seed 2050): 80 picked `transit`, 80 picked `experience`. 0 errors.
- Raw data + labels: `data/a3_activity_type_2026-09-30.json`.

## Labels — agent-proposed (not independent human labels)
The label follows the rule's meaning: `transit` for logistics and the plain frame, `experience` for anything a
writer could make a moment of. 12 rows marked borderline (e.g. a ger camp or farmstead stay, a pass with prayer flags
seen on the way, a cycling stage that starts with "depart").

## Result — pick accuracy by confidence floor
| Floor | `transit` picks | `experience` picks | Both |
|---|---|---|---|
| 0.50 | 63/65 | 55/66 | 118/131 |
| 0.80 | 51/52 | 49/53 | 100/105 |
| 0.90 | 43/44 | 47/48 | 90/92 |
| **0.95** (proposed) | **39/39** | **42/42** | **81/81** |

Wrong picks at ≥ 0.90: #26 "travel by local bus to visit Long-Hair Village" → transit 0.93 (the visit is the
point); #128 "Phewa Lake — explore at your own pace" → experience 0.92 (free time). Below 0.95 the `experience` side
keeps confusing free time and plain meals/overnights with experiences.

**Coverage at 0.95:** 217 of 400 disputed moments decided by Jev; the rest keep the rule. If enforced:
79 rule-transit Segments come back into Score (ceremonies, workshops, boat safaris, cable cars — the rule's
`attend`/`board`/`ride`/`receive` openers catch them), and 51 rule-missed logistics Segments are excluded
("check out and depart", border crossings, "begin journey").

## Decision
- [x] Nghiệp reviewed all 160 rows of the Google Sheet "Jev calibration — activity type A3-2 (2026-09-30)":
  **overturned 0 / 160**. Four borderline rows annotated, agent labels kept: #35 Kaichu Road causeway (a known
  sight, but the atom is only the crossing → transit), #50 Phobjikha "pass through" (no activity → transit),
  #122 Nine Arches Bridge on the train (a sight on the way → experience), #153 forest soba restaurant lunch
  (treated as a plain meal → transit).
- [x] **Enforce `a3_activity_type`: accept_floor 0.95**, no reject ceiling (choice question), Dev, S205
  (30/09/2026). At ≥ 0.95: 81/81 correct; decides 217 of 400 disputed moments, the rest keep the rule.


## Threshold v2 — S223 (10/10/2026, AA-756)

accept_floor 0.95 → **0.90**. Sample (160): pick accuracy 100% (81 picks) → 98% (92 picks).

Recomputed from the labelled sample in `data/` (no new labels). Approved by Nghiệp; applied on Dev through `PUT /admin/decisions/questions/{key}` (threshold_version 2). Before-values snapshot: `s3://aa-cis-bronze-005097885195/scripts/restore/s223_aa756_decision_question_before.json`. Audit: `docs/audits/2026-10-10-S223-jev-audit.html` (root repo). Spot-check 30 new decisions per question after the next recompute.
