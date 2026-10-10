# AA-756 — Jev log writes: retry on a fresh connection + backfill (S224)

## Finding
The Jev dashboard showed $11.80 / 408k requests / 292M tokens for 03–10/10; `shared.llm_call_log`
had $7.05 / 139k / 168M. The gap equals CloudWatch `llm_call_log_write_failed` +
`decision_log_write_failed` counts per day ("cannot perform operation: another operation is in
progress"): A3/recompute ran parallel `decide()` calls on one shared connection
(`_SingleConnAsPool`). Jev answered and billed; only the log write failed. #600 (real job pool)
fixed the cause at 09/10 ~01:00 UTC; the last failure is 00:50 UTC. 10/10 matches Jev.

Window: 30/09 (Jev in A3) → 09/10 00:50 UTC. 347,242 calls, 145.4M tokens, $6.11 unlogged.
Second-order cost: a lost `decision_log` row is also a lost cache entry, so each recompute asked
Jev again — the main reason 03/10, 05/10 and 08/10 were expensive.

Price is not the problem: $11.80 / 280.97M input tokens = $0.0420/Mtok = catalog `jev-latest`.

## Changed
- `record_call_with_pool`: on a pool write failure, retry once via `record_call` (own connection).
  Warning renamed to `llm_call_log_pool_write_failed`; `llm_call_log_write_failed` now means the
  row is really lost.
- `decide._write_logs`: ledger rows + hit counts go through `_insert_logs` in one transaction; on
  failure retry once on a fresh connection. Hit counts are re-queued only if the retry fails too.
  `decision_log_write_failed` now means really lost.

## Backfill (applied S224, DB only, no code)
`.tmp-session/s224/s224_backfill.py` — 53 aggregate rows (UTC day x stage) in `llm_call_log`,
`quality_signal.source = "reconcile_s224"`, `calls` = failed writes from CloudWatch, tokens =
calls x the stage's average tokens/call, cost at the catalog price. Cross-check vs the Jev
dashboard per day: 03/10 $1.78 vs ~1.75, 05/10 $2.21 vs ~2.2, 08/10 $3.84 vs ~3.8, 03–10/10
$11.78 vs $11.80. Typesafe total in CIS: $8.44 → $14.54.
Rollback: `DELETE FROM shared.llm_call_log WHERE quality_signal->>'source' = 'reconcile_s224'`.

## Tradeoffs
- No synthetic `decision_log` rows: verdicts are unknown and would pollute calibration. The
  Decisions page's per-question cost stays under-reported for that window; its billed total
  (from `llm_call_log`) is correct.
- Aggregate rows count as one call each on the pages (53 instead of 347k). Spend and tokens are
  exact; request counts for 30/09–09/10 are understated.
- One retry, not a queue: the failure mode was connection contention, which a fresh connection
  avoids. A DB outage still loses the row (and is logged as such).

## Should know
- Not covered: retries inside `_call_jev` (3s timeout, 3 attempts). A timed-out attempt Jev
  still processed is billed but not logged. 10/10 matched the dashboard, so it is small.
- Follow-up in AA-756: alert when `*_log_write_failed` > 0 (CloudWatch metric filter).
