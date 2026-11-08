# AA-1049 — T2 day-count reader (lane l17, S273)

Defect found by the S273 paid trial: a T2 rewrite with the per-job trial switch `T2_ORCHESTRATOR=linear` (audit tenant)
passed the orchestrator's Master slot check (every Master day present) and was stored `ai_generated`, while the
REPORT-mode final contract recorded `T2_DAY_COUNT_VS_MASTER` ("0 day(s) for a 12-day Master (missing 1..12)"), verdict
`needs_review`, flip unpublish.

## Root cause

Not a shape difference between the paths. Both store `itineraries` as the same string: the writer's day list is
serialized to canonical "Day N — Title" blocks (`graph._process_itineraries`, `normalize_generated`), then the AA-778
dash fix (`t2_dashes.fix_t2_dashes`; in T3's per-candidate fixes under the interim profile on main, in `normalize` on
the linear path, and again before the save) rewrites every marker to "Day N: Title" ("Day N · Title" when a title has
its own colon). The two checks read that text with two parsers:

- slot check `t2_orchestrator.day_slots` -> `day_parse.source_day_markers` (any line-initial "Day N"): 12 days;
- contract `final_contract._day_numbers` -> `day_parse.output_days` (canonical em-dash heading only): 0 days.

So on main too every T2 version recorded the phantom finding (the l15 notes' follow-up; the D8 contract test was a
strict xfail for it). The contract also `str()`-ed a non-string itinerary (a writer day list read as 0 days).

## Decisions

- **One reader for the T2 day-count checks: `day_parse.written_day_numbers(value)`.** It is the repair contract's
  former `day_headings` + `_day_text` logic, moved into the leaf `day_parse` (every line-initial day marker via
  `source_day_markers`, plus any canonical block the line reader missed; a writer-JSON day list serialized first by
  `written_itinerary_text`). Callers: `final_contract._day_numbers` (as a sorted set), `t2_orchestrator.day_slots`
  (ordered, duplicates kept) and `repair_contract.day_headings` / `_day_text` (now thin wrappers). Slot check pass
  (ordered equality) therefore implies no contract day finding (set equality).
- **`output_days` stays canonical-only.** It is S1's reader (day oracle, s1_checks, the A1 contract) and mirrors the
  S1 storage contract and the SQL twin `catalog._tour_day_text`: an S1 page without canonical headings is correctly a
  page with no day blocks. Widening it would weaken S1's checks; T2 text is never canonical once dash-fixed.
- **The default path's stored `judge_breakdown.final_contract` changes on purpose** (report-only data; no status,
  score, prompt, SQL text or other field moves). The parity golden was re-recorded on this commit and diffed against
  the old one: every difference is under `final_contract` (the phantom `T2_DAY_COUNT_VS_MASTER` gone from 7
  scenarios; `day_dropped` keeps it with the true detail "2 day(s) for a 3-day Master (missing 3)"; `elephant` now
  `policy_hold`). The replay base fixture recorded on an old main export cannot be re-recorded the same way (main now
  has the store seam): its two affected `contract_verdict` values (v-clean -> ai_generated, v-elephant ->
  policy_hold) were updated in place and `recorded_on` says so; every other field is as recorded.

## Changed

- `services/content_generation/day_parse.py`: `written_itinerary_text`, `written_day_numbers` (+ module docstring).
- `services/quality/final_contract.py`, `services/content_generation/t2_orchestrator.py`,
  `services/quality/repair_contract.py`: call the shared reader.
- Tests: `tests/unit/test_aa1049_t2_day_reader.py` (agreement over 13 itinerary shapes, dash-fix round trip, the S273
  12-day trial shape, the harness parity: contract day finding iff the slot check fails on both paths, linear stores
  main's shape); `test_d8_contract_verdict_is_policy_hold` is no longer xfail; golden + replay base updated.
- CONTEXT.md (day_parse paragraph).

## Tradeoffs

- The shared reader is the permissive line reader: a body line that starts "Day two brings ..." counts as a day
  heading. It already was for the slot check and the repair contract; the contract reads both sides (Master and
  version) with it, so a copied line counts on both. A stricter heading-only reader would need its own golden pass.
- The contract keeps set semantics (a repeated day is not a contract finding; the slot check and the repair contract
  catch order and duplicates).

## Should know

- No migration, no prompt change, no paid call. Default T2 status/score/qa unchanged; only the admin-only REPORT
  record changes (fewer false `needs_review` verdicts, so the W2 measurement of the contract on T2 becomes usable).
- Admin sentence edits (`admin_content_quality`, exit `admin_sentence_apply`) use the same `check_t2`, so they stop
  recording the phantom finding too.
