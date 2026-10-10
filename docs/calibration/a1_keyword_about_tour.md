# Calibration — `a1_keyword_about_tour` (AA-756, S224, 10/10/2026)

Question (stage `a1_seo`, AA-706), asked by the `s1_seo_prefetch` job for each tour's candidate
DataForSEO keyword ideas (up to 30 per tour):
> *Would a traveller searching this keyword be looking for this particular tour — its places, route or
> activity — rather than something else in the country?*
> state = `{tour, country, title_places, activity, keyword}`

Before S224: shadow, no floors (all 17,089 verdicts grey), ~$0.30 / 30 days, never labelled.

## Sample
- **100 keyword decisions**, latest verdict per subject, stratified by p decile (10 per decile).
- Raw data: `data/a1_keyword_about_tour_2026-10-10.json` (keyword, p, tour, country, day titles, label, borderline).

## Labels — agent-proposed (Claude Code, S224); Nghiệp approved enforcing on these
| Label | Meaning |
|---|---|
| **1** | The keyword names a place, route or activity the tour covers (incl. other names for the same place), or country + the tour's own activity ("nepal trekking" for a Nepal trek). |
| **0** | Another place/region; a specific hotel ("ajit bhawan jodhpur", "taj udaipur"); a generic country/culture search ("culture in india"); an activity the tour does not do ("south korea bike tour" for a non-bike tour); unrelated ("build bmw x7"). |

Balance: 40 × 1, 60 × 0; 14 marked borderline.

## Result
| Zone | Threshold | n | Precision vs labels |
|---|---|---|---|
| accept | p ≥ 0.80 | 20 | **1.000** (recall 50%) |
| accept | p ≥ 0.70 | 30 | 0.833 |
| reject | p ≤ 0.30 | 30 | **1.000** |
| reject | p ≤ 0.40 | 41 | 0.927 |

Population (17,086 distinct keyword decisions): p ≥ 0.80 → 2,336 (14%); p ≤ 0.30 → 9,247 (54%).

## Decision
`enforce`, accept_floor **0.80**, reject_ceiling **0.30**. In `s1_prefetch.py` an enforced reject drops an
idea the substring rule (AA-702) kept; an enforced accept keeps one it missed (synonyms). Grey keeps the
substring rule's call.
