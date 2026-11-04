# AA-1042 — lane "trace": Route trace, Alerts, Quality guard (S268)

Parallel lane of AA-1042. The admin chain editor, provider order, presets and route config versions are the
`controls` lane (AA-1042-controls.md); this lane does not touch them.

## Decisions

- **Request id = `quality_signal.route_trace`, no new ledger column.** `shared.llm_call_log.request_id` is the
  PROVIDER's request id, so the gateway had no id that groups the attempts of one request. A new column on
  `llm_call_log` (~390k rows, > 100k) is "lock risk → contract" (ADR 0017 d4, runner exit 15), would change the
  shared ledger INSERT that TripPlanner's core and the AA-729 eval relabel both pin, and would collide with the
  controls lane's own column on the same statement. The trace id rides in `quality_signal` instead:
  - the gateway gives every request scope (`_AttemptScope`) a uuid4 `trace_id`;
  - every gateway-written row (refused / failed attempt, billed unusable reply) carries
    `quality_signal.route_trace` (+ the existing `position`);
  - the answered reply carries `LLMResponse.route_trace` / `route_position`; `billed_fields(resp)` passes them as
    two new keys and `record_call*` / `record_calls` fold them into `quality_signal` (`route_trace`,
    `route_position`). Every call site already passes `**billed_fields(resp)` (AST guard S6), so no call site
    changed except `BrandFitResult`, which takes `**billed_fields` as constructor keywords.
  - A single trace is looked up inside a +-15 min window of the clicked row's `created_at` (the `created_at`
    index), never by a full scan.
- **Rung share needs no trace id.** It is read from the answered rows: `attempt_no = 1` is the primary rung,
  `> 1` a fallback rung (another model, or the same model on another connection). Rows without `attempt_no`
  (before ledger v2) count as "unknown".
- **Alerts reuse the ops-check plumbing of CIS #1296** (`shared/ops_checks.raise_alert`: bell + one e-mail per
  check and UTC day, advisory lock). `check_for` now also answers the route prefixes (`llm_route_fallback.`,
  `llm_route_last_rung.`, `llm_route_primary_back.`, `llm_route_cost_shift.`) and `llm_route_digest`.
- **"Primary back" state is derived from the bells**, no state table: a stage is degraded while its newest
  fallback / last-rung bell is newer than its newest primary-back bell (7 days). Fallback and last-rung bells
  repeat at most hourly, so a stage that degrades again soon after recovering is seen again.
- **Scheduling = job kind `llm_route_watch` on `shared.job`**, queued by the api's ops sampler like the other
  periodic kinds: one run per 15-min bucket (`llm_route_watch:<UTC minute bucket>`) and one digest run per UTC
  day from 01:00 UTC (`llm_route_digest:<day>`). $0 (database reads only), background, deploy-gate exempt.
  Switch `LLM_ROUTE_WATCH` (default on).
- **Digest bell / e-mail only when there is something to say** (a stage served by a fallback or with failed
  requests in the last 24 h); the job result always carries the digest.
- **Quality guard rides on `degraded[]` (AA-925).** New name `fallback_writer`: severity `count` (marked) by
  default, `hard` when the stage's `hold_fallback_writes` is on. Hard → A1 adds `DEGRADED_DETECTOR` (the existing
  hold → Review Queue path), T9 labels the piece `held` (admin-only, the tenant still receives it). A1 reads
  the writer's own `fallback_used`; T9 binds a per-piece collector that the gateway fills when a writer-role
  request is answered by a fallback model (route position > 0).
- **Forced refusal for the live check**: `POST /admin/ai-gateway/route-trace/test` runs 1-6 tiny real requests
  on one stage (16 output tokens each); with `refuse_primary` the gateway refuses the stage's first model for
  those requests only (a context variable, never a stored switch): a `refused` / `access_denied` ledger row,
  raised before the provider call, so no breaker, circuit or stage coverage state moves.

## Changed

- Migration 373 (expand): `shared.llm_route_policy` (per-stage thresholds + hold switch; `*` row = defaults).
- Gateway (`shared/llm_client/client.py`, `models.py`, `call_log.py`): route trace id + position on every row.
- New: `shared/llm_client/route_watch.py` (policy, stats, findings), `services/jobs/llm_route_watch_job.py`,
  `api/services/llm_route_trace.py`, `api/routers/admin_llm_route.py`.
- Admin: AI gateway tab "Trace" (`TraceTab.tsx`, `routeTrace.messages.ts`, `lib/routeTrace.ts`), a "Route" link
  on a Calls row.

## Tradeoffs

- A JSON key is weaker than a column (no index of its own). Lookups always carry a time window, and the rung
  share does not need it, so the cost is small; a column can come later with the next ledger contract file.
- The digest is skipped on a quiet day; the job result keeps it, so the Jobs page still shows the run.
- Content of T2 (tenant tour rewrite) is marked only through the job-level degraded counter, not held: its QA
  flow has no hold step today.

## Should know

- Rows written before this change have no `route_trace`: the Trace tab shows them as single-attempt requests
  only through the Calls list, not in the request list.
- The test endpoint makes real (tiny) paid calls: about 0.0001 USD each on today's routes.
