# AA-900 / AA-1042 — S271 lane l11: refusal noise

Tech lead ask (S271): no error noise for refusals the routing already handles. On Dev the acc3 Bedrock account
refuses seven models (no grant yet); the route control v6 chains already move past them. What was left: real
half-open probe calls to acc3 (refused rows, 0 USD), the daily connection test, warnings in CloudWatch, and 24 h error
numbers that mixed those refusals with real problems.

Three PRs, in merge order: CIS migration 382 (migration-only, contract), the CIS code PR, the root tool PR.

## Decisions

- **access_denied max open period 6 h** (`breaker.ACCESS_DENIED_MAX_OPEN_S`), first period kept at 300 s. A grant is
  a human event, so a long cap is safe; a short first period keeps a transient refusal (an IAM change during a deploy,
  an STS hiccup) cheap. Doubling 300 → 600 → … reaches the cap after about seven failed probes (~10 h of refusals).
  A model refused for weeks already has a long streak, so it moves to 6 h at its next failed probe. Real-traffic
  probes per known-denied model: at most 4 a day (was up to 48).
- **The 6 h cap is for a known refusal only** (review fix, `breaker.failure_rules`). An account-wide refusal (Error
  002, an IAM block) is classed access_denied too. With 6 h for every access_denied row, an outage of ~10 h or more
  would put every row at a 6 h period; the connection circuit stays OPEN until the newest member period ends and
  `admit` turns every real call away, so no real call could answer and trigger the account-recovery re-probe: a
  recovered acc3 would stay off real traffic for up to 6 h (paid fallbacks or failed jobs meanwhile). Rule now, judged
  on the locked row before each failure: another model of the connection answered after this row's streak began
  (`_known`) → `RULES` (6 h); otherwise → `ACCOUNT_DENIED_RULES` (30 min, the cap before AA-900). So the connection
  circuit half-opens within 30 min of its last failed probe, as before this change.
- **The daily probe and Test connection stay the fast path for a grant.** Both go through `LLMClient.probe` →
  `breaker.guarded(admit_call=False)`: they bypass an open circuit, and their success closes it. Verified by tests
  (`test_the_probe_reaches_an_open_circuit_and_its_success_closes_it`); no change was needed there.
- **Account recovery re-probes at once** (new, `breaker._reprobe_after_account_recovery`). Without it, a long
  account-wide outage (Error 002 on every model, 29/10) would leave every model behind a 6 h period after the account
  comes back, and a Test connection on one model would close only that model. Rule: when a model answers on a
  connection, the OPEN circuits of an account-wide class whose refusal began after the connection's previous answer
  become half-open (streak and `opened_at` kept). The known refusals (refused since before another model answered —
  the same AA-1026 rule the connection circuit uses) are not touched. A fresh single-model refusal costs at most one
  extra probe (after it fails again it is a known refusal).
- **Non-LLM provider circuits keep 30 min** (`breaker.PROVIDER_RULES`, the same rule set as `ACCOUNT_DENIED_RULES`): Jev / DataForSEO / Geoapify share the LLM
  breaker's pure transitions; a 401 / 403 there has no daily probe behind the circuit, so the cap stays.
- **Expected refusal (display) = probe state, not breaker state.** `shared/llm_client/expected_refusal.py`: refused +
  access_denied + the deployment's `access_state = access_denied` + the connection's `access_state = ok`. The
  deployment's access_state is written only by probes (daily job, Test connection), so a model that answered
  yesterday and refuses today counts as an error until the next probe records it: that keeps the "a working model
  starts refusing" alert path. Connection `ok` excludes account-wide refusals from ever being expected.
- **Log levels use the breaker's in-process view** (`known_refusal` / `known_model_refusal`): the call path cannot
  query the DB per failure. Same idea (refused since before another model answered), cheaper source. Only the level
  changes (info instead of warning) and `known_refusal=true` is added; nothing is dropped.
- **`llm_deployment_skip` rate limit**: once per (model, connection, reason) per 10 min per process, first occurrence
  always logged, next line after the window carries `suppressed`. Memo bounded at 2048 keys (reasons carry free text).
- **Migration 382: `capabilities.min_output_tokens = 8192` on gpt-6-luna-openai and gpt-6-luna.** Evidence in the
  header (R2 rerun 06/11: 8 of 24 cut at 4096, answered calls 2,329–6,983 output tokens). 8192 = smallest round
  ceiling above the largest answer, the value the S271 retry already proved. Never lowers a higher existing floor.
  Other reasoning models checked and left on the 4096 code floor (no cut evidence): Claude 5.x (effort-bounded,
  migration 381), gpt-5.6-luna / gpt-6-astra (refused on acc3, no data), gpt-6-sol / gpt-6.1-sol / gpt-6.1-terra (no
  traffic in the rerun).

## Changed

- `shared/llm_client/breaker.py`: RULES access_denied cap, `ACCOUNT_DENIED_RULES` + `failure_rules` (6 h only for a
  known refusal), `PROVIDER_RULES` (= `ACCOUNT_DENIED_RULES`), `rules=` on `after_failure` /
  `open_seconds`, `known_refusal`, `known_model_refusal`, `recovery_candidates`, the re-probe in `record_success`,
  info-level reopening of a known refusal.
- `shared/provider_circuit.py`: passes `PROVIDER_RULES`.
- `shared/llm_client/vendor_route.py`: `_skip_log` (rate-limited `llm_deployment_skip`), `reset_skip_log`.
- `shared/llm_client/client.py`: `llm_deployment_attempt_failed` / `llm_route_attempt_failed` at info for known
  refusals.
- `shared/llm_client/expected_refusal.py` (new): the rule (SQL + Python).
- `api/services/ai_gateway.py`: stats `refused` minus expected, new `expected_refused`; each ledger row
  `expected_refusal`; provider health `failing_models` without expected, `known_refused_models`.
- `api/services/llm_availability.py`: joins the deployment / connection access_state; known models do not make the
  connection open / degraded, are not in `open_models` or `last_error_*`; `known_refused_models`.
- `shared/llm_client/route_watch.py`, `api/services/llm_route_trace.py`: `failed_attempts` without expected,
  `expected_refusals` apart.
- Frontend (AI gateway): Providers stats line "N expected refusals (no access yet)", Connections health "N models with
  no access yet (expected)", Calls tab: failed KPI excludes them, own KPI, outcome badge neutral + note.
- `CONTEXT.md`: "Refusal noise: known no-access models".
- Root `tools/llm/gateway_status.py` (root PR): 24 h ok / err / expected per connection; top error groups without
  expected refusals plus a separate expected line.

## Tradeoffs

- 6 h cap vs. faster pick-up of a grant on real traffic alone: the daily probe (05:00 UTC) and Test connection
  cover the grant; the cap only bounds the worst case when nobody looks.
- The expected-refusal display lags one probe behind a new refusal (by design, see Decisions). The opposite lag after
  a grant does not matter: a granted model stops refusing, so no rows to classify.
- The EXISTS subquery in the stats SQL runs per row of the 7 d window; it is a primary-key lookup on a ~44-row table.
- Fallback alerts (`route_fb.<stage>`) are unchanged: a judge answered by its OpenAI API twin because the acc3 model is
  not granted still counts as a fallback answer. Changing that is a route-policy question, not noise (follow-up).

## Should know

- Migration 382 must merge after #1345 (381): the preflight checks main max + 1.
- Tests: `tests/unit/test_aa900_l11_refusal_noise.py`, `tests/integration/test_aa900_expected_refusal_db.py`,
  `tests/integration/test_aa900_migration_382.py` (migration PR). Updated: `test_aa1018_connection_circuit.py`
  (a success now re-probes account-caused refusals: half-open instead of open), `test_aa1025_c1_providers_page.py`
  (`expected_refused` key), `test_aa1002_no_behaviour_change.py` (module classified).
- No ledger row is changed or dropped; refused rows of real calls are still written.
