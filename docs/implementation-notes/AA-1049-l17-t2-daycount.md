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

- **Review fix (S273): validate_node's day gap on a T2 tenant rewrite reads the same way.** `validate_node` gets the
  written days from `graph.itinerary_day_gap`, which read only the canonical heading
  (`itinerary_utils.generated_day_word_counts`). T3 on main (interim profile: the dash fix runs before
  `_t3_structural_issues`) and the linear orchestrator (`_check`: normalize, then `verify` -> `validate_score`)
  validate the dash-fixed text, so every multi-day T2 candidate got a phantom `ITINERARY_DAY_COUNT_MISMATCH`
  (structure -1, 10.0 -> 9.75 in the harness; `ITINERARY_STILL_COMPRESSED` skipped because no day was "present").
  The S273 trial's stored 9.75 is that deduction. Now `itinerary_day_gap(..., any_form=True)` (set by validate_node
  when `is_tenant_rewrite`) reads the written side with `day_parse.written_day_word_counts`: the same day set as
  `written_day_numbers` by construction, each day with its block's body word count (lines after the marker line up
  to the next marker, title not counted, a range gives each day the block's count, a repeated day keeps its last
  block). On a canonical text it equals `generated_day_word_counts` (test). S1 / A1 (the code is hard on writer
  v2), `final_day_guard` (skips tenant rewrites) and the S1 contract's `_s1_day_findings` keep the canonical reader:
  it is S1's storage contract.

## Changed

- `services/content_generation/day_parse.py`: `written_itinerary_text`, `written_day_numbers` (+ module docstring).
- `services/quality/final_contract.py`, `services/content_generation/t2_orchestrator.py`,
  `services/quality/repair_contract.py`: call the shared reader.
- Tests: `tests/unit/test_aa1049_t2_day_reader.py` (agreement over 13 itinerary shapes, dash-fix round trip, the S273
  12-day trial shape, the harness parity: contract day finding iff the slot check fails on both paths, linear stores
  main's shape); `test_d8_contract_verdict_is_policy_hold` is no longer xfail; golden + replay base updated.
- CONTEXT.md (day_parse paragraph).
- Review fix: `day_parse.written_day_word_counts`; `graph.itinerary_day_gap(..., any_form=False)` keyword and
  validate_node passes `any_form=is_tenant_rewrite`. Tests (section 3 of `test_aa1049_t2_day_reader.py`): same day
  set as `written_day_numbers` on every shape, equal to the canonical counts on canonical text and after the dash
  fix, validate_node on the dash-fixed Master has no day mismatch and the canonical score, a dropped day is still
  found, S1 still reads canonical only, and the linear path stores main's score on 7 harness scenarios (was 9.75 vs
  10.0 on 5 of them). The replay base `quality_score` of v-clean and v-number updated in place 9.75 -> 10.0 (the
  replay feeds the stored "Day N: Title" text back as the writer reply, so main's graph validate hit the phantom
  deduction); `recorded_on` says so.

## Tradeoffs

- The shared reader is the permissive line reader: a body line that starts "Day two brings ..." counts as a day
  heading. It already was for the slot check and the repair contract; the contract reads both sides (Master and
  version) with it, so a copied line counts on both. A stricter heading-only reader would need its own golden pass.
- The contract keeps set semantics (a repeated day is not a contract finding; the slot check and the repair contract
  catch order and duplicates).

## Should know

- Review fix scope on the default path: the parity golden is byte-for-byte unchanged (main's graph validates the
  canonical text; T3's structural check keeps only hard codes, and the day code is soft on a tenant rewrite). A stored
  default-path score moves only when a tenant-rewrite validate sees non-canonical day headings, e.g. a writer reply
  whose itinerary is a string already in "Day N:" form (the replay case): it loses the phantom -1 structure point, so
  in a rare case a version at 6.x could cross the 7.0 needs_review line. That is the intended fix.
- The day oracle (`day_oracle.check`) still reads `output_days`: on a dash-fixed T2 text it files shadow findings
  only (never scored or enforced on a tenant rewrite). Follow-up, not in this slice.

- No migration, no prompt change, no paid call. Default T2 status/score/qa unchanged; only the admin-only REPORT
  record changes (fewer false `needs_review` verdicts, so the W2 measurement of the contract on T2 becomes usable).
- Admin sentence edits (`admin_content_quality`, exit `admin_sentence_apply`) use the same `check_t2`, so they stop
  recording the phantom finding too.
