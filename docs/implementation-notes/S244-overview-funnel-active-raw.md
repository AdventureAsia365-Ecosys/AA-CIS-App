# S244: overview funnel counts only active raw tours at every stage

Backlog row "Bhutan 33 vs 32" (finding 8 of the AA-858 review, CIS #848).

## Decisions

- Cause: one Bhutan tour was trashed at the source on 05/10 (raw `source_status = 'trashed'`,
  `deleted_at` set, HITL rejected) and kept its S1 drafts in `generated_content`. "Raw (active)"
  filtered on active raw rows, "S1 generated" did not, so generated (33) > raw (32). The data is
  correct; the funnel mixed two populations. It was the only such tour in any country (live check, S244).
- Every later stage (generated, in review, published, atomized) now joins `raw_tours` with the same
  active filter as the raw column (`_ACTIVE_RAW`), so each column is a subset of the one before it.

## Changed

- `api/routers/admin_overview.py` `_FUNNEL_SQL` (f-string with one constant; no user input).
- Tooltip sources say "active tours" for the four later stages.
- `tests/unit/test_overview_funnel_active_raw.py`.

## Tradeoffs

- Published/atomized tours whose raw row was later trashed drop out of the funnel. That matches what
  the funnel is for (the pipeline for tours we still sell); the Master Content page still lists them.

## Should know

- No migration, no new route, no env var.
