# AA-1042 — The gateway defers rungs behind an open refusal circuit (S270)

Live Dev (S270, AA-741 phase 0 run, 05/11): the judge stages `t2_judge`, `t7_debate_judge` and `t10_judge` route
`[gpt-5.6-luna, gpt-6-luna, gpt-6-luna-openai, nova-lite]`. acc3 refuses both Luna models (Error 002), so their
breaker circuits are open. The breaker already kept the provider calls away (one real refused call per model per open
period), but `_call_route` still tried each rung in order: `deployment_plan` returned an empty plan with
`circuit_skips`, `_call_plan` raised `breaker.circuit_error`, and `_call_key_logged` wrote a 0 USD `refused` /
`access_denied` row with no connection and latency 0 through `_logged_failure_net`. Two such rows per judge request
(about 560 in the run: t2_judge 57+57, t7_debate_judge 145+145, t10_judge 4+4), and the trace of every request showed
two "attempts" that never reached a provider before `gpt-6-luna-openai@openai-direct` answered at position 2.

## Decisions

- **Defer, do not drop.** A rung is "sunk" when `vendor_route.sunk_skips(plan)` says its plan is empty only because of
  circuits opened by a refusal (`REFUSING_CIRCUIT_CLASSES` = access_denied, quota_exhausted, credential; the
  deployment circuit or the AA-1018 connection circuit; open, or half-open with the probe lease held by another call,
  which `breaker.is_open` already reports as open). `_call_route` puts it on a deferred list with its original route
  position and moves on. No ledger row is written in place.
- **The answered row carries the skipped rungs.** `LLMResponse.route_skips` = one entry per skipped deployment:
  `{model, connection, reason: "circuit_open:<class>", position}`. `route_skips` is the third `call_log.ROUTE_KEYS`
  key, so `billed_fields(resp)` passes it and `record_call*` / `record_calls` / `billed_unusable_rows` fold it into
  `quality_signal` like `route_trace` / `route_position` (sanitised: at most 16 entries, 120 chars per text). The answered
  row keeps its real `route_position` (2 for the judge chains), so `route_watch.is_fallback`, the fallback / last-rung
  alerts, `_note_fallback_write` and the requests filter (`served_fallback`) read exactly as before.
- **Nothing answers: the deferred rungs run last, as before.** After the live rungs, each deferred rung goes through
  `_call_key_logged` in route order (re-planned: a circuit that turned half-open meanwhile gets its probe call), so it
  writes the same `refused` / `access_denied` row (connection NULL, latency 0) and adds the same `CircuitOpen` to the
  failures. The final error is built from the outcomes sorted by route position, so the message and `_all_failed`'s
  classification (LLMAccessDenied when every refusal is account-level, cause = the last in route order) are the ones
  the route gave before S270 (`test_all_rungs_down_*` runs the same request with the sink switched off and compares).
- **Route deadline.** When `RouteDeadlineExceeded` stops the route (the deadline gate or the attempt cap), the deferred
  rungs' refused rows are written first (they were reached before the deadline, so before S270 they had rows). In the
  deferred phase, a deadline already passed means the rung is logged, not called (no provider call after the
  deadline even if its circuit closed meanwhile).
- **The plan is computed once.** The sink check plans the rung (same scopes, role, PII class, inside the AWS-first
  narrowed binding) and hands that plan to `_call_catalog_model` through a context variable (`_ROUTE_PLAN`, consumed
  once, reset after the rung), so the plan that decided "not sunk" is the plan that is called and the
  `llm_deployment_skip` lines are not logged twice.
- **Role skips next to refusing circuit skips still sink.** Those deployments can never serve the stage's role; the
  circuits are why the plan is empty, and before S270 the rung raised the circuit error anyway. A budget skip next to
  a circuit skip does not sink (conservative: the budget would still block once the circuit closes).
- **Not sunk** (unchanged path): a single-key request (`model_tier`, pins, one-model routes: `_call_route` is not
  used), shadow calls (`_run_shadow_bound` calls `_call_key_logged` directly), embed, legacy keys (alias / chain
  path), pinned keys, `LLM_DEPLOYMENT_ROUTING=off`, `LLM_CIRCUIT_BREAKER=off` (checked explicitly, besides `is_open`
  answering False), the route test's forced refusal at position 0 (it must write its row), a circuit opened by
  throttling / an outage / an unknown class, and a plan emptied only by budget / role / scope filters.
- **Default variant only.** The stricter probe-based sink (also deferring a half-open rung with a free lease) is not
  taken.
- **Route trace UI.** `trace_view` turns each `route_skips` entry into an attempt with status `skipped` (cost 0, no
  latency, `error_class` = the circuit class, `error` = the reason) at its route position, so the drawer still shows
  every rung. `request_view` adds `skipped_attempts`. The drawer labels such an attempt "Not called: circuit open"
  (EN + VI); `attemptTone("skipped")` was already neutral.

## Changed

- `shared/llm_client/vendor_route.py`: `REFUSING_CIRCUIT_CLASSES`, `sunk_skips(plan)` (pure).
- `shared/llm_client/client.py`: `_SunkRung`, `_ROUTE_PLAN` / `_take_route_plan`, `route_skip_entries`;
  `LLMClient._sunk_rung`, `_log_deferred`; `_call_route` rewritten around a `call()` / `answered()` pair (same steps
  per rung as before, plus the sink check and the deferred phase); `_call_catalog_model` uses the pre-computed plan.
- `shared/llm_client/models.py`: `LLMResponse.route_skips`.
- `shared/llm_client/call_log.py`: `ROUTE_KEYS` + `route_skips`, `_route_skips` sanitiser, `route_signal` /
  `_with_route` / `billed_fields` / `record_call` / `record_call_with_pool` / `_fold_route` / `billed_unusable_rows`.
- `services/content_generation/brand_fit.py`: `BrandFitResult.route_skips` (it is built with `**billed_fields(resp)`)
  and `aggregate_runs` keeps run 1's.
- `api/services/llm_route_trace.py`: `TRACE_SQL` / `requests_sql` read `quality_signal -> 'route_skips'`;
  `skipped_attempt_views`, `trace_view`, `request_view`.
- Frontend: `lib/routeTrace.ts` (`SKIPPED_STATUS`, `RouteRequest.skipped_attempts`), `RouteTraceDrawer.tsx`,
  `routeTrace.messages.ts` (`skippedAttempt`), `lib/routeTrace.test.mjs`.
- Tests: `tests/unit/test_aa1042_sink_refused_rungs.py` (new: the six cases of the lane, deadline paths, the pure
  `sunk_skips` table, the ledger fold, the trace view);
  `tests/integration/test_aa1042_route_trace_db.py::test_deferred_rungs_live_in_the_answered_row_and_show_as_skipped_attempts`
  (real Postgres: stats, request list, trace over an answered row with `route_skips`). The re-run suites (`test_aa1002_s3_routes_snapshot`,
  `test_aa1002_s4_breaker`, `test_aa1018_p2_reliability`, `test_aa1026*`, `test_aa1042_*`) needed no assertion change:
  none counted the pre-call refused rows of a route whose later rung answered (the breaker tests that read them end
  with every rung down, where the deferred rungs still write their rows).

## Tradeoffs

- **The half-open probe stays in place** (S268 preview-serving tradeoff). The S268 notes rejected skipping refused
  deployments in the gateway because it would replace the breaker's half-open recovery (one real call per breaker
  period, at most 30 minutes for access_denied) with the daily 05:00Z probe. This change keeps that recovery: a
  half-open circuit with a free lease is in the plan, so the rung is called in place, its success closes the circuit
  and the answer is a PRIMARY answer at position 0 (`route_back` needs exactly that in real traffic). Only rungs the
  breaker would not call anyway are deferred.
- **Ledger semantics change** for answered requests: a skipped rung is no longer its own 0 USD row. Readers that
  counted those rows see fewer: `route_watch.STATS_SQL.failed_attempts`, `requests_sql.failed_attempts` (and the
  Trace "needed more than one attempt" filter still keeps the request through `served_fallback`), any ad-hoc
  `status = 'refused'` count per stage (e.g. `cost_by_stage.py` of the AA-741 run), the daily rollup's refused count
  (`services/retention/rollups.py`, `shared.llm_call_daily`) and the row count per stage / model. Costs are unchanged
  (the rows were 0 USD). The per-connection stats of AI gateway › Providers (`api/services/ai_gateway.py`
  `LLM_STATS_SQL`) never counted them (connection NULL). Requests with no answer keep every row.
- **Trace time of a skipped attempt** is the answered row's time (the rung was decided earlier in the same request,
  milliseconds before). The drawer's first / last times come from the logged rows only.
- **Deadline row position.** Before S270 a deadline row carried the scope's last position (the previous rung's,
  sunk or not); with the previous rung deferred it carries the position of the last rung that went through
  `_call_key_logged`. Left as is (a 0 USD row whose model names the rung not started); not read by any alert rule
  differently in practice.
- One extra `deployment_plan` + catalog read per catalog rung is not added: the sink check's plan is reused.

## Should know

- **Ledger:** skipped rungs live in the answered row's `quality_signal.route_skips`, not as their own rows. Expected on
  Dev after deploy: the judge stages (t2_judge, t7_debate_judge, t10_judge) write 0 `refused` rows with connection
  NULL while acc3 refuses the Luna models (one real refused call per model per breaker period remains: that is the
  half-open probe), and each answered row (position 2, openai-direct) carries two `route_skips` entries.
- Root doc to align (orchestrator's root PR; not edited here): `docs/architecture/ai-gateway.md` §1 "**Every attempt
  writes one ledger row**, including refused and failed attempts at 0 USD." Proposed sentence: "**Every attempt
  writes one ledger row**, including refused and failed attempts at 0 USD. A route rung the breaker would not call
  (every deployment behind a circuit opened by a refusal) is deferred: when a later rung answers, it is listed in the
  answered row's `quality_signal.route_skips` instead of its own row (AA-1042, S270); when nothing answers, it is
  tried last and writes its refused row as before." And §6 Route trace: add "rungs deferred behind an open refusal
  circuit appear as `skipped` attempts ($0, no latency)".
- No migration, no route data change, no OpenAPI change (the trace routes answer plain dicts), no paid call in tests.
