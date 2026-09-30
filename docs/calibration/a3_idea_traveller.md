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

## After the first enforced run (S206, 30/09/2026)
- Job `deaf3850` (Bhutan, US, 10 places): 300 ideas → 214 rejected (all lodging), 50 stored. The stored 50 were
  almost all lodging: generic ("hotel bhutan", "druk hotel paro", "galing resort paro") at Jev p 0.2–0.9 (a
  place name in the keyword makes Jev read it as a trip search), and brand-only ("amankora paro", "como punakha",
  "six senses paro", "gangtey palace paro").
- **Re-pose tried and not adopted:** "somewhere to stay … also when only the property or brand name is given
  (Amankora, COMO Uma …)". It caught 19 more of the 50, but on this sample it rejected "Golgulsa Temple stay"
  (0.10) and "flights to ulaanbaatar" (0.16): 20/22 = 91% at ≤ 0.20, below the 95% floor. The wording above
  stays; the trial key `a3_idea_traveller_v2` (stage `adhoc_aa693`) was only for this test.
- **Added instead (code, `segment_research_batch.is_lodging_search`):** a lodging word (hotel, resort, lodge,
  residency, boutique, homestay, guesthouse, hostel, inn, villa, agoda, airbnb …) drops the idea before Jev, except
  "<airport/station> to hotel". On this 132-row sample it matches the 20 lodging rows and nothing else; on the 50
  stored Bhutan ideas it matches 17.
- **Known gap:** brand-only lodging names (33 of the 50). They are not in any Bhutan tour text, so no list can be
  built from our data. `a3_demand_belongs` (enforce ≤ 0.40) still has to attach such a keyword to a real moment
  before it counts in a Score.
