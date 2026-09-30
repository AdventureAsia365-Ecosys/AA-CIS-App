# Calibration — `a3_same_place` (AA-695 A3-7)

**Question (noul, migration 192):** *Do these two names refer to the same real-world place?* Asked in
`run_segment_matching()` for place pairs the moment rule would join although written differently (same or unknown
country, at least one new atom). An enforced, confident no keeps the pair apart. State = `{place_a, place_b, country}`.

## Sample
- Dev, 30/09/2026 (S205): every differently-written place pair inside today's multi-place Segments —
  **1,020 pairs** (384 of 3,337 Segments mix place names; 65 span countries, handled by the country guard).
  0 errors. Jev puts 663 pairs at p ≤ 0.3.
- **150 pairs**, stratified by p (30 ≤ 0.1, 45 in 0.1–0.3, 45 in 0.3–0.7, 30 > 0.7). Raw + labels:
  `data/a3_same_place_2026-09-30.json`.

## Labels
true = the same place (a longer/shorter name, a spelling, the place plus its town); false = two places (two
villages/temples/passes, two legs of a route, a city vs its airport/station) or a generic kind of place.

## Result
| Reject zone | n | Different places |
|---|---|---|
| **p ≤ 0.10** (enforced) | 30 | **30/30** |
| p ≤ 0.20 | 61 | 59 — misses #31 Cuc Phuong primate rescue centre (0.11), #61 Preah Vihear / temple (0.20) |
| p ≤ 0.30 | 75 | 73 |

At 0.10 the gate splits 327 of the 1,020 pairs.

## Decision
- [x] Nghiệp reviewed all 150 rows: **overturned 0 / 150** (notes on #91, #107, #113, #118, #122, labels kept).
- [x] **Enforce `a3_same_place`: reject_ceiling 0.10** (Dev, S205, 30/09/2026).
- Takes effect for Segments formed from now on; the rerun rebuilds all Segments from scratch (AA-653).
