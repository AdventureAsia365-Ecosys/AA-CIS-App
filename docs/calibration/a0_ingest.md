# Calibration — A0 ingest (`a0_row_kind`, `a0_itinerary_usable`, `a0_country`) (AA-690)

Asked in `services/ingestion/jev_gates.py` for every uploaded row with itinerary text (preview and commit).
Effects when enforced + confident: non-tour kind → drop `not_a_tour`; itinerary reject → drop `thin_itinerary`;
a country pick fills an **empty** country only.

## Sample
- Dev, 30/09/2026 (S205): **all 793 `raw_tours`**, the 3 questions asked together per row, country blanked in the
  state so `a0_country` can be scored against the stored country. 0 errors. Raw: `data/a0_ingest_2026-09-30.json`.
- S203's 100-tour sample was 99/100 tours, so it could not calibrate the negative side; the full table can.
- Review Sheet "Jev calibration — A0 ingest (2026-09-30)": the 55 rows Jev doubted (non-tour kind, itinerary
  p ≤ 0.5, country differing from the stored one at ≥ 0.8).

## Result
| Question | Rule | In sample |
|---|---|---|
| `a0_row_kind` | drop when a non-tour kind is picked with confidence **≥ 0.95** | 9/9 correct (Gunma museums, safari park, bicycle rentals, a hot spring…) |
| `a0_itinerary_usable` | drop when p **≤ 0.20** | 33/33 correct (empty itineraries, articles, a shop page, a test row, one day text repeated for every day) |
| `a0_country` | fill an empty country when confidence **≥ 0.95** | agrees with the stored country on 738/745; the 7 "misses" were **stored-country errors**, Jev right |

## Decision
- [x] Nghiệp reviewed all 55 rows: **overturned 0 / 55** (borderline notes on #7, #17/#18, #21, #42).
- [x] Enforce `a0_row_kind` accept 0.95, `a0_itinerary_usable` reject 0.20, `a0_country` accept 0.95 (Dev, S205).
- [x] Data fixed with Nghiệp's approval: country on 7 raw tours (3 India → Nepal, 3 Laos → Thailand, Ride the
  Trail Laos → Vietnam); 2 junk published tours trashed (Trip 3 – Meiji-no-Yakata, Yaksa Trek BEST DEAL).
- Note: `a0_country` is asked only when the resolver finds no country. The stored-country errors show the resolver
  can be wrong when it does find one; asking when the itinerary names places in another master country (design
  A0-3) is not implemented.
