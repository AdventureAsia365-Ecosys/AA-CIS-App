# S291 — CIS follow-ups from S290 (lane a-cis-fixes)

Four small slices from the S290 close-out (session log section "Còn lại") and the C4.3 W48 database review
(root `docs/audits/2026-11-24-S290-session/c43-w48/README.md`, findings 1 and 3). Two PRs: CIS migration 480 (expand,
one index) and this code PR. The code works with or without 480 (slower without), so the two PRs are independent.

| Slice | Issue | What |
|---|---|---|
| 1 | AA-1148 | Upload › "Tours Ready for Rewrite" (`GET /admin/tours-ready`) no longer lists hand-made tours |
| 2 | AA-1148 | Trash / restore of a tour whose key was never served queues no platform recompute |
| 3 | AA-838 | `REQUEST_TOURS_SQL` (`shared/tour_liveness.py`) reads the key state with a one-key LATERAL lookup |
| 4 | AA-1126, AA-838 | Anomaly-breaker ledger reads in two sargable branches, the baseline cached in `evaluate`; the Tenant 360 cost sums in two branches |

## Decisions

- **Slice 1: hide, not label.** A hand-made tour (`raw_tours.entry_origin = 'manual'`, CIS 478) is written, checked and
  published in the content workspace (required list, two-person rule); the Upload page is about uploaded sheets. The
  list's SQL adds `AND t.entry_origin <> 'manual'` (the column is NOT NULL, default `sheet`; readiness already requires
  478). The S1 list (`GET /admin/tours`) is unchanged: Rewrite of a hand-made tour stays possible there, as AA-1148
  designed (the export guard and the Review Queue handle it).
- **Slice 2: the rule is "the key was ever served", not "the tour is hand-made".** `retire_source` / `restore_source`
  add their recompute with `served_only=True` (`Outcome.add_recompute`): it is dropped when `v_tour_liveness` has no
  row for the key before nor after the action (`before == after == (None, None)`, the same test `_cascade` uses for
  `ever_served`). Kept inside `add_recompute` so `service.py` stays at its code-health size baseline. A never-served key has no atom in the Score / Route caches, no Segment member, no place in the tour
  graph and no public trip page, so nothing served changes; this covers an uploaded row that was never published too.
  A Master the action switches off still recomputes through `_switch_off_dead_masters` (unchanged). The S290 lesson
  "Trash một tour tay CHƯA từng xuất bản..." now has its guard: `tests/integration/test_aa1148_s291_upload_list_and_trash.py`.
- **Slice 3: LATERAL plus an optimisation fence.** The representative tour is computed once (`CROSS JOIN LATERAL
  (SELECT COALESCE(r.trip_id, ta.tour_id) AS tour_id) rep`). The key's state comes from
  `LEFT JOIN LATERAL (SELECT tl_rq.state FROM v_tour_liveness tl_rq WHERE tl_rq.source_id = <key of rt_rq> OFFSET 0)`.
  Without `OFFSET 0` the planner flattens the subquery back into a join and computes the whole view per request row
  (local EXPLAIN showed the same `Join Filter` as before); with it, the filter is pushed into the view's GROUP BY and,
  with CIS 480, becomes `Index Cond` on `raw_tours_key_expr_idx` (local EXPLAIN, `enable_seqscan` on and off). The
  Subject rule's served-member subquery stays in the CASE branch, evaluated only for a retired representative.
- **Slice 3: the key expression, not `raw_tours.source_key`.** The stored column (CIS 258) would read cleaner, but the
  integration replay skips 258, so every tour_liveness test would fail on it; inside the LATERAL the expression is a
  value of the outer row, not a join key, so it costs nothing.
- **Slice 4: the cache miss was `evaluate`, by design "no cache".** The call path (`current` → `_cached`) caches the
  median per process, app and UTC day, but `evaluate` read it on every call, and `llm_route_watch` evaluates the three
  apps every 15 minutes: 3 × 96 = 288 baseline reads a day, the ~300 W48 counted. `evaluate` now reads the median
  through `cached_median` (the same `_baseline` dict and key as the call path); today's spend stays fresh. No lock in
  the async path: dict reads / writes are atomic and `_lock` may be held by a sync loader thread.
- **Slice 4: two disjoint branches for the app rule.** `coalesce(app, quality_signal ->> 'app', 'cis') = $n` equals
  `app = $n` UNION ALL `app IS NULL AND coalesce(quality_signal ->> 'app', 'cis') = $n` (the branches cannot overlap).
  The rolled-day test (`NOT EXISTS llm_call_daily`) moved from every ledger row to every grouped day (same days kept).
- **Slice 4: the baseline already reads `shared.llm_call_daily` for rolled days** (the `rolled` CTE, unchanged). The
  table is near empty on Dev (rollup happens at retention, still dry run), so nothing more to gain there today.
- **Slice 4: Tenant 360 sums.** `tenant_id = $1 OR ($2 AND tenant_id IS NULL)` became two branches
  (`TENANT_COST_TOTAL_SQL`, `TENANT_COST_WINDOW_SQL`, moved to `api/services/tenant360_lists.py` next to the other
  Tenant 360 statements, which also brings `api/routers/admin.py` back under its size baseline); the NULL branch carries `$2` as a
  one-time filter, so a non-internal tenant reads only its `(tenant_id, created_at)` index range.

## Changed (against the ask)

- No new index on `shared.llm_call_log`. With the baseline cached, the baseline query runs about once per process, app
  and UTC day (single digits a day instead of ~300), and today's read is a one-day range on `llm_call_log_created_id_idx`.
  A `(app, created_at) INCLUDE (cost_usd)` index on a 300 MB ledger would need `CREATE INDEX CONCURRENTLY` in a
  `-- migrate: no-transaction` file, which the CIS integration replay cannot run today (it sends each file as one
  multi-statement query, as 205 shows). Follow-up only if W49 still shows the baseline.
- Restore of a never-served key also skips the recompute (same rule as the trash; nothing served changes either way).

## Tradeoffs

- Hiding hand-made tours from the Upload list instead of a "Content workspace" label: one list, one purpose; the
  workspace already lists them with their state.
- The cached median can be up to one UTC day "old" only if past ledger rows change, which they do not (rows carry
  their own `created_at`; a rollup keeps the sum). An admin reset clears the cache (`invalidate`) as before.
- `OFFSET 0` is a planner fence that reads oddly; the comment says why, and the equivalence test pins the rows.

## Should know

- Equivalence tests freeze the old statements: `tests/integration/test_aa838_request_tours_sql.py` (served, retired with
  and without a served Segment member, paused, grouped versions, never served, atom only, missing atom, another
  tenant) and the two new tests at the end of `tests/integration/test_aa1126_gateway_caps_sql.py` (every app shape, a
  rolled day; tenant sums for a tenant and the internal tenant, all time and the 30-day window).
- The live EXPLAIN comparison (old vs new for slices 3 and 4) is a DB Migrate `run-script` file the orchestrator runs:
  root `.tmp-session/S291/aa838_explain_s291.py` (plans only, no row values).
- `tests/unit/test_aa1126_spend_stops.py`: the second watch run now reads today's spend only (3 reads, no baseline);
  a new test pins one baseline read per app and UTC day over 96 watch runs, and the call path reusing it.
