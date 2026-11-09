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
- **Rung share needs no trace id.** It is read from the answered rows with ONE rule (`route_watch.FALLBACK_RUNG` /
  `PRIMARY_RUNG` / `UNKNOWN_RUNG`, `is_fallback` in Python), used by the watch stats, the cost baseline, the rung
  share, the request list and the trace drawer: a FALLBACK answer has route position > 0, or `attempt_no > 1` (a later
  deployment of the first model), or `fallback_used`; the PRIMARY answer has `attempt_no = 1` and none of those.
  Rows with no attempt number and no fallback mark (before ledger v2) count as "unknown". (Review fix, S268: the first
  version used `attempt_no` alone. `attempt_no` counts only calls that reached a provider, so when the first model is
  skipped without an attempt — every circuit open, catalog row disabled, budget skip, role not served — the fallback
  model's answer has `attempt_no = 1`: it was counted as primary, the fallback alert went quiet and the fallback
  answers raised a false "primary back".)
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
- **T2 (review fix).** The T2 job binds the same collector around the rewrite and the T3 repair rounds and stores
  `judge_breakdown.fallback_writer = {held, entries}` on the version (`route_guard.t2_mark`; `judge_breakdown` is
  admin-only, `_tenant_projection` drops it). `held` = the stage holds fallback writes: an admin-only review label
  like T9's, the version status stays score-based and the tenant still receives the text. Inside the T2 graph the
  entry is always `count`, so the hold never adds `DEGRADED_DETECTOR` to a tenant rewrite. The job result keeps the
  graph's own `degraded[]` next to T3's (`t3.graph_degraded`) and the mark (`fallback_writer`).
- **Tenant fallback list (review fix).** Every tenant's T2 versions / T9 pieces with the admin-only held mark are
  Tenant QA data, so they moved off the Trace tab's spend.read route to `GET /admin/content-quality/fallback-writes`
  (`tenant_content.read`: admin, content_lead, tenant_qa; `quality_router` in `admin_llm_route.py`). The finance and
  ai_ops roles keep the catalog (A1) list only; the Trace tab asks for the tenant lists only when the viewer holds
  `tenant_content.read`. The path sits under `/admin/content-quality`, not `/admin/ai-gateway`, because the role
  matrix forbids the content side any `/admin/ai-gateway` route.
- **Forced refusal for the live check**: `POST /admin/ai-gateway/route-trace/test` runs 1-6 tiny real requests
  on one stage (16 output tokens each); with `refuse_primary` the gateway refuses the stage's first model for
  those requests only (a context variable, never a stored switch): a `refused` / `access_denied` ledger row,
  raised before the provider call, so no breaker, circuit or stage coverage state moves.

## Changed

- Migration 371 (expand): `shared.llm_route_policy` (per-stage thresholds + hold switch; `*` row = defaults).
- T2 (`services/jobs/t2_rewrite_job.py`): the fallback mark on the version (`judge_breakdown.fallback_writer`).
- `GET /admin/content-quality/fallback-writes` (tenant list) split from `/admin/ai-gateway/route-trace/fallback-content`
  (catalog list, spend.read).
- Gateway (`shared/llm_client/client.py`, `models.py`, `call_log.py`): route trace id + position on every row.
- New: `shared/llm_client/route_watch.py` (policy, stats, findings), `services/jobs/llm_route_watch_job.py`,
  `api/services/llm_route_trace.py`, `api/routers/admin_llm_route.py`.
- Admin: AI gateway tab "Trace" (`TraceTab.tsx`, `routeTrace.messages.ts`, `lib/routeTrace.ts`), a "Route" link
  on a Calls row.

## Tradeoffs

- A JSON key is weaker than a column (no index of its own). Lookups always carry a time window, and the rung
  share does not need it, so the cost is small; a column can come later with the next ledger contract file.
- The digest is skipped on a quiet day; the job result keeps it, so the Jobs page still shows the run.
- T2 has no hold step in its QA flow, so its "held" is a label for QA, not a block (same as T9's).

## Should know

- Rows written before this change have no `route_trace`: the Trace tab shows them as single-attempt requests
  only through the Calls list, not in the request list.
- The test endpoint makes real (tiny) paid calls: about 0.0001 USD each on today's routes.
- **Migration number.** Renumbered 373 -> 371 by the orchestrator after CIS 370 merged (main max + 1). The
  CIS preflight (ADR 0017 d1) accepts only main max + 1, so the file is renumbered at merge time to the then-current
  main max + 1. The code no longer names the number (it says "the llm_route_policy migration"; the integration test
  finds the file by name and reads its version from the file name), so a renumber touches only: the file name, the
  number inside its body (header, step 3, `updated_by` default `migration:NNN`, the COMMENTs, the `schema_versions`
  INSERT), this file, and the root retention row (`02-data-management.md`: "AA-1042, CIS 371" and the S268 note).
  Check with `git grep -n '371' -- api/migrations/371_llm_route_policy.sql docs/implementation-notes/AA-1042-trace.md`
  and in the root worktree `git grep -n 'CIS 371'`.
- **No readiness raise.** Every read falls back when 371 is missing (`ready: false`, defaults; policy writes 503), so
  the migration and the code ship in one PR (ADR 0017 d1, expand file).
- **Live check (Dev).** Pick an active judge / validate stage with at least two models, then
  `POST /admin/ai-gateway/route-trace/test {"stage": "...", "refuse_primary": true, "count": 6}`, run
  `POST /admin/ai-gateway/route-watch/run {"mode": "watch"}` (fallback + last-model bells and e-mails), then the test
  again with `refuse_primary: false` and the watch again (primary back). At most one e-mail per alert and UTC day.
- **Shared-file edits** kept small for the controls lane: `AiGateway.tsx` (tab + target state), `lib/aiGateway.ts`
  (`TAB_KEYS`, `PAGE_ACTIONS`), `CallsTab.tsx` (Route link), `admin_ai_gateway.py` (`PAGE_WRITES`), `staff_authz.py`
  (one POLICY block), `call_log.py` / `client.py` (route keys, forced refusal).
