# AA-1024 follow-up (S276 lane l6): refused ledger rows of a skipped deployment name their connection

## Problem

On Dev (10/11/2026), the T7 debate judge stage wrote each hour its answered rows on the OpenAI API plus about 10 %
extra `shared.llm_call_log` rows with `status refused`, `error_class access_denied`, the shadow model as `model` and
`connection_key NULL`. On 09/11 the same model had refused rows WITH the acc3 connection (real calls, before the
circuit opened).

Root cause: the stage's shadow model (10 % sample) has one deployment, on acc3, behind an open access_denied circuit.
The shadow call runs `_call_key_logged` alone (no route, so no S270 deferral), `deployment_plan` returns an empty plan
with `circuit_skips`, `_call_plan` raises `breaker.circuit_error(...)` before any attempt, and the net row
(`_logged_failure_net` -> `log_gateway_failed_attempt`) is written with no connection, wire id or provider. The same
NULL row came from: a deferred route rung tried last (no live rung answered) or logged after the route deadline
(`_log_deferred`), a single-model request (`model_tier`) behind an open circuit, and a hard-budget skip
(`ConnectionBudgetExceeded`). Such a row never matches the expected-refusal rule (`expected_refusal.EXPECTED_SQL`,
keyed by connection + model) and never the root gateway status tool's circuit-open rule, so every one counted as an
error (route watch failed attempts, gateway status top errors).

## Decisions

- Keep the row (ledger v2: a failure the gateway raised before any attempt gets one row from the net; the shadow had
  no later rung to defer to). Only its facts change.
- `breaker.circuit_error` keeps the skips on the error (`CircuitOpen.skips`, plan / priority order).
- `client.not_sent_facts(exc, model, connection_key)`: for a `CircuitOpen` with skips or a `ConnectionBudgetExceeded`
  with `connection_keys`, and no connection known by the caller: the first skipped connection, that deployment's wire
  id (deployment registry, enabled or not) and provider (`_provider_of`). With more than one connection skipped,
  `quality_signal.skipped_connections` lists them (max 8). Only the exception itself is read, not its cause chain: an
  aggregate route error keeps its own shape.
- Every refused row of a `CircuitOpen` (also `breaker.guarded`'s own, which already had its connection) gets
  `quality_signal.refusal = circuit_open` (`provider_circuit.refusal_signal`, the S275 mark): nothing was sent.
- `route_config_version` needs nothing: the llm_call_log BEFORE INSERT trigger (CIS 370) stamps every CIS row.

## Changed

- `shared/llm_client/breaker.py`: `CircuitOpen.skips`, set by `circuit_error`.
- `shared/llm_client/client.py`: `not_sent_facts`; `log_gateway_failed_attempt` fills connection / wire id / provider
  from it and merges its quality_signal keys.
- Tests: `tests/unit/test_aa1024_l6_not_sent_connection.py` (new; the shadow case reproduces the live NULL row);
  `tests/unit/test_aa1042_sink_refused_rungs.py`: the deferred-rung rows now expect `acc3` instead of None.

## Tradeoffs

- One row names one connection. A model skipped on several connections is still one refused request, not one row per
  connection (that would multiply refusals that are not attempts); the others are in `skipped_connections`.
- A circuit-open row of a known refusal now matches both the expected-refusal rule and the circuit-open mark. The CIS
  readers only know the expected rule (no change). The root `tools/llm/gateway_status.py` counts both columns
  separately: its companion patch counts such a row once, as circuit-open (nothing was sent), and keeps the expected
  column for real calls refused.

## Should know

- No migration, no new column: `connection_key`, `deployment_wire_id`, `provider` and `quality_signal` already exist.
- After deploy the 48 h breakdown of the judge stage should show the shadow model's refused rows with
  `connection_key = 'acc3'` and `quality_signal ->> 'refusal' = 'circuit_open'`; rows written before stay NULL.
- `tests/unit/test_aa804_ledger_v2.py` has two tests that fail only in some long multi-file runs (module timer
  `_s6_missing_at` / 300 s retry window, order dependent); they pass alone and with this lane's files, and are not
  touched here.
