# Calibration — `a3_idea_traveller` (AA-693 A3-4)

**Question (noul):** *Is this a search by a traveller who wants to see or do something at a destination, rather than a
search for a hotel, resort, homestay or a booking?* Asked in `_gate_ideas()` before a DataForSEO keyword idea is
stored; state = `{keyword, seed_places}`.

## Sample
- Dev, 30/09/2026 (S205): 132 stored `search_demand` keywords — all 32 with a lodging/booking word + 100 random.
  `seed_places` left empty (ideas are stored without their seeds); the question is about the keyword itself.
- Raw + labels: `data/a3_idea_traveller_2026-09-30.json`.

## Result
| Zone | n | Correct |
|---|---|---|
| **reject p ≤ 0.20** (proposed) | 19 | **19/19** (hotels, guesthouses, resorts, homestays, a ryokan) |
| p 0.20–0.25 | 2 | "hot springs resort" 0.23 (lodging, kept — a known miss); "Golgulsa Temple stay" 0.24 (a temple-stay programme, an experience) |
| p ≥ 0.5 | 111 | places, activities, getting there |

0.20 stays: moving it to 0.25 would drop the temple stay.

## Decision
- [x] Nghiệp reviewed 132 rows: **0 labels overturned**; flagged #20 "hot springs resort" as a keyword the rule keeps.
- [x] Enforce `a3_idea_traveller` reject 0.20 (Dev, S205).
