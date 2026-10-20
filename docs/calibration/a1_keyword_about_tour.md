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

## Threshold v2 proposal — S245 (AA-845), not applied yet

Nghiệp approved accept **0.65** / reject **0.40** (S245, from the S228 offline grey-share measurement:
grey 32.2% → 15.7%, reject 54% → 61%, accept 14% → 23%), to be applied in the AA-845 measurement wave
**after** this write-up (lessons: a floor is only meaningful once "zone → what the system does →
how product quality changes" is written down).

### Zone → system action → quality change

Code: `services/seo_intelligence/s1_prefetch.py::jev_review`, run by the `s1_seo_prefetch` job
before S1. Lodging searches never reach Jev (`is_lodging_search`). The kept ideas (≤ 25, by volume)
are stored in `silver_aa_internal.seo_context.keyword_ideas`; the top 10 with volume become
`top_keywords`, which the S1 writer prompt lists as the SEO keywords to use (`prompts.py`).

| Zone (enforce) | What the system does | Quality effect when Jev is right | Quality effect when Jev is wrong |
|---|---|---|---|
| reject (p ≤ ceiling) | drops the idea even if the substring rule kept it | the writer is not asked to use an off-topic search ("kilimanjaro base camp" on a Chengdu tour) — fewer forced/off-topic keywords in title, meta and body | an on-topic search is lost: slightly thinner SEO coverage; nothing false is written |
| accept (p ≥ floor) | adds an idea the substring rule dropped (synonyms: "tiger's nest" for Taktsang) | better keyword coverage for the tour's real places | an off-topic search can reach the top-10 list, so the writer is asked to work in a place, hotel or activity the tour does not have — a source-unsupported promise that grounding then has to catch (flag_fix cost) or a misleading title/meta |
| grey | the substring rule's call stands | — | — |

The two mistakes are not equally costly: a wrong reject costs a little SEO coverage, a wrong
accept can put a false promise into master content (which every tenant inherits). The accept floor
therefore needs the higher precision.

### Precision on the labelled sample (S224 set, 100 decisions, `data/a1_keyword_about_tour_2026-10-10.json`)

| Floor | n | Correct | Precision |
|---|---|---|---|
| accept ≥ 0.80 (live) | 20 | 20 | **1.000** |
| accept ≥ 0.70 | 30 | 25 | 0.833 |
| **accept ≥ 0.65 (approved)** | 37 | 26 | **0.703** |
| reject ≤ 0.30 (live) | 30 | 30 | 1.000 |
| **reject ≤ 0.40 (approved)** | 41 | 38 | **0.927** |

The 11 wrong accepts at 0.65–0.80 are other in-country places, hotels and activities the tour does
not do ("taj udaipur", "heritage haveli jaipur", "delhi" on a Delhi-to-Kathmandu tour, "sri lanka
safari" on a non-safari tour, "laos cycling trip" on a non-cycling loop). The 3 wrong rejects at
0.30–0.40 are genuine on-topic searches ("best hiking trails in taiwan", "luang prabang
waterfall").

### Recommendation for the apply step

- **Reject 0.40: apply as approved.** 92.7% precise and the wrong side only costs coverage.
- **Accept 0.65: do not apply as-is.** At 70% precision about 3 in 10 newly accepted ideas are
  off-topic, and an accepted idea can steer the writer into an unsupported promise. Keep accept at
  **0.80** (population: accept 14%, reject 61%, grey 25%) until a relabel shows ≥ 95% at a lower
  floor. This deviates from the approved number, so the orchestrator confirms with Nghiệp before the
  PUT; if Nghiệp keeps 0.65, apply it and watch `top_keywords` for hotel/off-route terms in the wave.

Apply (orchestrator, Dev, $0; back up the current row first with `GET /admin/decisions/summary`):

```
PUT /admin/decisions/questions/a1_keyword_about_tour
{"mode": "enforce", "accept_floor": 0.80, "reject_ceiling": 0.40,
 "calibration_ref": "docs/calibration/a1_keyword_about_tour.md",
 "notes": "AA-845 S245: reject 0.30 -> 0.40 (sample precision 0.927); accept kept 0.80 (0.65 = 0.703)"}
```

`threshold_version` increments on its own when a floor changes. Measure in the same wave as
`a1_claim_supported`: the `a1_keyword_about_tour` zone split from `GET /admin/decisions/summary?days=1`
and 10 tours' `top_keywords` before/after.
