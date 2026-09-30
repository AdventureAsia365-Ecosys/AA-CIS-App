# AA-694 (A3-5) — PAA landing gate + the filtered shortlist actually reaching the landing

## Decisions
- The landing Jev Question is asked per (Segment moment, question) **before** landing, like the country
  scope — every atom of a Segment shares its canonical place + action. One Verdict per distinct pair;
  Segments with the same moment share it. Concurrency 8 (a full pass ≈ 7,400 pairs, ≈ $0.12).
- Shadow until the Calibration Record is reviewed (Q5).

## Changed
- **Bug fix (pre-existing, from #495):** `land_questions_for_segment()` recomputed its own shortlist with
  `_capped_candidates()`, ignoring the Jev-filtered `segment_candidates`. A question the country scope had
  dropped had no vector, went straight to `claim_by_name_fallback()` and could still count. It now takes
  `candidates=` from `precompute_question_landings()`. The country-scope enforcement (S204) was therefore
  only partly effective until this deploy.
- New `filter_candidates_by_landing()` after the country filter; migration 187 (shadow question).

## Should know
- Measured on Dev: 11,143 candidate pairs → 7,402 after country scope; only ~14% of sampled pairs are
  good landings.
- `questions_count` of every Segment will drop at the next atom ranking once the landing gate is
  enforced — expected, it was inflated.
