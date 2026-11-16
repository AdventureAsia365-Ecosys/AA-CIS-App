# S282 — Sentry triage fixes (Refs AA-1111)

Eight Sentry issues of `aa-cis-api` (Dev), each traced to file and line by the S282 triage (with a skeptic pass).
One backend PR, no migration, no paid call.

| Sentry | What it was | Fix |
|---|---|---|
| AA-CIS-API-M | Admin usage row lost: the raw path of a route with two UUIDs (113-119 chars) did not fit `shared.tenant_api_usage.endpoint` varchar(100); logged at ERROR | The rate limiter records the matched route template (`_endpoint(request)`); `track_api_call` cuts endpoint / method to the column widths and logs a failed write as a warning with traceback |
| AA-CIS-API-1F | Weekly writing-memory KPI job failed: a Dev fixture wrote `gate_ledger = '{}'` (an object) and `jsonb_array_elements` raised on it | Fixture writes `'[]'`; every unguarded `gate_ledger` unnest goes through `shared/repository/jsonb_sql.json_array()` |
| AA-CIS-API-1A | N+1 on the claim label set browser (2 + 2N queries; really 2 + 4N with the savepoints) | `store.read_rows_many` + `set_list.read_relabels_many_soft`: 4 queries whatever the number of sets |
| AA-CIS-API-18 | Blog envelope refused although the body was there: json-repair turns "envelope + a second JSON value" into a list | `_salvage_list`: take the first dict with a non-empty body (warning `t9_reply_json_salvaged`); same for the 4 sibling T9 parsers with their own key; clearer error reason |
| AA-CIS-API-19 | Recompute job waited 1,800 s on the platform lock while an earlier attempt's thread still held it, then failed | A busy lock is a `Reschedule` (reason `recompute_lock_busy`, attempt not counted) after the release delay |
| AA-CIS-API-1B | Tenant flag read timed out at 0.5 s on the first request of a fresh task (cold TLS connect), logged at ERROR | Warm the api Redis client with one PING at start-up; outer bound above the client's worst case; timeout at WARNING |
| AA-CIS-API-1D | `pilot_taken` refusal reported as an error event with traceback | New `registry.Refused(NonRetryable)`; the runner logs it (and `OutputFailed`) at warning |
| AA-CIS-API-1C | Re-run of an already failed T9 piece: `OutputFailed` logged with `log.exception` | Same runner change as 1D |
| (AA-CIS-API-T follow-up) | `t3_skipped_gpt41_retired` duplicated the raise right after it | Logged at warning |

## Decisions

- **Route template, not a wider column (M).** The template is short, has no ids, and gives the metrics reader one group
  per route. `isinstance(p, str)` keeps the MagicMock requests of the older unit tests on the raw path (skeptic note).
  The 429 branch runs before routing and keeps the raw path; the writer's cut is the safety net (a 404 on a long
  unknown path).
- **One SQL helper for the array guard (1F).** `json_array("cp.gate_ledger")` returns the
  `CASE WHEN jsonb_typeof(...) = 'array' ... ELSE '[]'` pattern funnel.py and admin_tenant360.py already used; it
  refuses anything but a column name. Applied at kpi.py (2 sites), disagreements.py, shadow.py, admin_a4.py (2) and
  admin_content_quality.py (3). `admin_content_quality` line with `gate_ledger || flags` is left alone: an object
  concatenated with an array is an array (skeptic).
- **Full columns kept in `read_rows_many` (1A).** `set_summary` -> `final_labels` subscripts `claim_text`,
  `content_sha256`, `labeller`, ... (skeptic): the column list stays `store._SELECT_COLS`. Order per set is the
  `_READ_SQL` order (rows) and `created_at, relabel_id` (corrections), which `effective_rows` relies on.
- **List salvage only, no extra model call (18).** The triage's "retry the write once" is a paid behaviour change and
  is left out. No blanket stray-key check either (skeptic: it would fail replies that pass today).
  `t9_write_background_failed` for a `SeoEnvelopeError` is logged at warning (same event name).
- **Busy lock, no new threshold (19).** The job tries the lock once (`max_wait_seconds=0`, `RecomputeLockBusy`) and
  `run()` turns that into `Reschedule(RELEASE_DELAY_SECONDS)` outside the thread (a Reschedule raised in the thread
  would be wrapped as `RecomputeStageFailed`, skeptic). The existing `LOCK_MAX_WAIT_SECONDS` now bounds how long ONE
  attempt keeps finding the lock busy across reschedules (`progress.lock_busy = {attempt, since}`); past it the
  attempt ends `recompute_lock_timeout` as before (retryable, the next attempt gets its own window). So the total
  patience is unchanged (two windows of 1,800 s), but no worker slot and no "running" recompute is held meanwhile.
- **Flag read bound derived, not a new number (1B).** `FLAG_READ_TIMEOUT_S = (retries + 1) * (connect + read)
  + 0.2 s` from `api/core/redis_client.py` (2.2 s today); the client's own bounds decide, the outer one is a net. The
  event name `aa817_tenant_flag_read_failed` is unchanged, so the ops check and the CloudWatch alarm still count a
  timeout; only the level (Sentry) changes. Redis error / no client stay at ERROR.
- **`job_failed` event name kept (1C/1D).** Handled ends (`OutputFailed`, `Refused` = `registry.HANDLED_FAILURES`)
  log `job_failed` at warning without a traceback; metric filters on the name still match.

- **Code-health ratchets kept (AA-1097).** `list_sets` and the new many-versions corrections read moved to
  `services/ground_truth/set_list.py` (review.py is a baseline file and may not grow); review.py re-exports
  `list_sets` (the router and tests call `rv.list_sets`) and set_list imports review inside its functions (no import
  cycle). The ledger fragment is one constant `jsonb_sql.GATE_LEDGER` imported by both routers, and the two baseline
  functions that gained a branch hand it to a helper: `worker.log_job_failed` (warning vs exception) and
  `recompute_job.settle_recompute_error` (busy lock vs any other recompute error).

## Changed

- `Refused` errors are written like `OutputFailed`: `<code>: <message>` (was `NonRetryable: <code>: <message>`), reason
  = the code. `GET /admin/tenants/provision/{job_id}` shows the shorter text.
- The two-jobs recompute tests (unit and integration) now expect one run plus one Reschedule, then the second run.

## Tradeoffs

- 19: a stuck holder keeps the job queued (rescheduling every 120 s) for up to two windows; a queued job is visible
  to the deploy guard like any queued row. Before, it was a running row for the same time.
- 1B: on a dead Redis the flag read can now take up to the client's own worst case (~2 s) instead of 0.5 s; the
  rate-limit middleware on the same request already pays that bound.

## Should know / follow-ups

- Not done here (need a CIS migration or more design): a CHECK `jsonb_typeof(gate_ledger) = 'array'` (and on
  `flags`, `repair_log`, `discarded_attempts`) after repairing old rows; `EXPECTED_SECONDS` of `recompute` (1,800 s)
  is below a real platform run (16-52 min) and should become a Platform Settings value (no migration in this lane);
  the same busy-reschedule for `a3_tour_lock` / `TourLockTimeout`; finer stop checks inside the Score / landing /
  route stages; the paid one-retry on a blog envelope miss; `t3_skipped_pii_stage` at warning.
- After deploy: re-queue `writing_memory_kpi_weekly` for week 2026-11-09 (its report was never stored); resolve the
  eight Sentry issues and watch for new events.
