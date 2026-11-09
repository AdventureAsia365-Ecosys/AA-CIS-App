# AA-741 / AA-900 — S271 lane l9: the gateway owns the output-token budget

Tech lead S271: stop the recurring `max_tokens` failures for good. Evidence (30 days of `shared.llm_call_log`, cut =
status truncated / billed_unusable with stop reason max_tokens | length): the R2 eval judge (`adhoc_aa900`,
gpt-6-luna-openai, `eval_judge.MAX_TOKENS = 1200`) 33 cut / 0 ok, so every judge call fell back and judge kappa was
0.137; `t9_write` 6 at 12000 (fixed by CIS #1341, untouched here), `t8_angle_gen` 7 at 2048 (fixed by #1330),
`t9_repair` 3 at 4096, `t9_faq_filter` 1 at 3000, `t2_judge` 4 at 16 (the admin route test), `adhoc_conn_test` at 32
(by design). 1.82 USD lost. Root cause every time: each call site hard-codes its own ceiling, and the 5.x / 6.x
reasoning models bill their thinking inside it.

## Decisions
- **Two rules, in the gateway, for every stage.** (1) Floor: a reasoning-capable catalog model is never sent less than
  `REASONING_FLOOR = 4096` output tokens (or its row's `capabilities.min_output_tokens`). (2) One truncation retry on
  the same route model with double the ceiling (capped at 24000 and at the model's `max_output_tokens`) when a reply is
  cut, a provider refuses to return a cut reply (OpenAI 400, class truncated), or a billed unusable reply's stop
  reason is a cut. Never a third call.
- **Pure rules in the core** (`shared/aa_llm/tokens.py`, CORE_VERSION 1.2.0) so TripPlanner / Partner can adopt them
  by the normal core sync; the binding to requests, catalog rows and switches is `shared/llm_client/token_floor.py`.
- **"Reasoning-capable"**: the catalog row's `capabilities.reasoning` (bool) when it states one, else a code rule:
  Claude 5.x and later (sonnet / haiku / opus / fable, with or without `claude-`), GPT-5.x and later. Claude 4.x,
  GPT-4.1, Nova, Llama, embeddings: no floor. No migration: no catalog row states either capability today.
- **The floor overrides a call site's explicit value** (unlike `stage_model_params`): a call site cannot know which
  model the admin routes its stage to, and a ceiling below the floor on a reasoning model only cuts. The floor never
  lowers a higher ceiling and never exceeds the model's own output cap. Applied in `_call_catalog_model` right after
  `stage_params.apply` (every catalog path, incl. pins); the legacy keys (Claude 4.5) need none.
- **Retry placement: per route model** (`LLMClient._call_key_logged`), not around `generate()`. The R2 judge case is a
  cut on the route's first model that the route then "answered" with a weaker fallback: a retry after the route would
  never run. The retry runs before the route moves on.
- **Ledger** keeps the token_budget.py semantics: the cut reply that came back gets the gateway's own row
  (`call_log.cut_reply_row`: status truncated, `budget_cut: true`, `gateway_attempt: true`, `max_tokens`,
  `retry_max_tokens`, no ok-rate key) written before the retry is sent; a cut that raised already had its row (0 USD
  truncated attempt row, or the billed_unusable row). The retry's reply is the caller's, with
  `LLMResponse.truncation_retry_max_tokens`; `billed_fields()` carries it (new `call_log.RETRY_KEYS`) and `record_call*`
  fold it into `quality_signal` as `truncation_retry: true` + `max_tokens`, the same keys as the caller-side retry.
- **Same gateway request, same attempt number.** The retry runs inside the request's attempt scope (same route_trace,
  counts on the attempt cap) and reports the attempt number of the reply it retried (`_AttemptScope.retry_of`), so
  `route_watch` (FALLBACK_RUNG reads `attempt_no > 1`) never counts a retry as a fallback, and the trace is not
  "lost" (the request has an ok row).
- **Opt-outs**: `NO_RETRY_STAGES` = `adhoc_conn_test` (probe, a cut is its expected answer) and `a3_atomize` (re-asks a
  cut read itself with its own ceiling); `LLMRequest.budget_retry=False` (only `services/acp_shared/token_budget.py`,
  which keeps its own two-row retry for T8 / T9 write / repair / FAQ filter because its cut row carries the piece and
  request ids only the caller has); shadow calls; a request already at the model's output cap; past the route deadline
  (the caller gets the cut reply, as before). Switches: `LLM_TOKEN_FLOOR=off`, `LLM_BUDGET_RETRY=off` (ECS env).
- **Call sites**: `eval_judge.MAX_TOKENS` 1200 -> 4096, `drafter.MAX_TOKENS` / `preference.MAX_TOKENS` 400 -> 4096 (the
  writing-memory stages run on the non-Anthropic judge route, a GPT-6 reasoning model), `T9_REPAIR_MAX_TOKENS` 4096 ->
  8192 (3 first replies still cut at 4096 in the S271 ledger; each was retried at 8192, so 8192 is the ceiling that
  answered). `t2_judge` at 16 is the admin route test (`llm_route_trace.TEST_MAX_TOKENS`): the floor lifts it on a
  reasoning model; the literal stays (it still bounds a non-reasoning model). `t9_faq_filter` is already 6000 (#1330).
  Prompt text unchanged: no `prompt_versions.json` bump (`MAX_TOKENS` is not a hashed prompt symbol).
- **Alert**: `route_watch` raises `route_cut.<stage>` (bell + one e-mail a UTC day, ops-check plumbing) when more
  than 5 % of a stage's replies over 24 h were cut at their ceiling (at least the policy's `min_requests` replies),
  `NO_RETRY_STAGES` left out. $0 (ledger reads in the existing `llm_route_watch` run).
- **Review fix (S271): the cut stats have their own filter** (`CUT_FILTER`, not `LEDGER_FILTER`). Eval rows
  (`is_experiment`) are kept and counted under `quality_signal.real_stage` (the R2 judge cuts were all eval rows; with
  `LEDGER_FILTER` they could never alert). A refused cut (OpenAI 400, status truncated, 0 USD, no tokens) counts in the
  numerator and the denominator; `cut_cost_usd` stays the billed part. The stats key is `replies` (was `billed`), plus
  `refused_cut` and `eval_rows`, named in the alert text. Shadow and Jev rows stay out.
- **Review fix (S271): the retry after a cut that raised is sent after the `except` block** (`_call_key_logged`), so
  it runs with no exception being handled. Before, the retry's error got the cut error as implicit `__context__`:
  `_billed_usage_of` / `_ledgered` / `classify` follow that chain, so a billed cut followed by an unbilled retry failure
  wrote a second billed_unusable row of the cut attempt and no row for the retry, and a 400 cut refusal followed by a
  5xx classified the 5xx as truncated (breaker class `request`: the outage was not counted on the circuit).

## Changed
- `shared/aa_llm/tokens.py` (new), `__init__.py` (1.2.0), `MANIFEST.json` (regenerated).
- `shared/llm_client/token_floor.py` (new); `client.py` (floor in `_call_catalog_model`, retry in `_call_key_logged`,
  `_call_key_once` = the old body, `log_gateway_cut_reply`, `estimate_call_usd` uses the floored ceiling,
  `_AttemptScope.retry_of`); `models.py` (`LLMRequest.budget_retry`, `LLMResponse.truncation_retry_max_tokens`);
  `call_log.py` (`RETRY_KEYS`, `retry_signal`, `cut_reply_row`, the fold); `route_watch.py` + `shared/ops_checks.py`
  (`route_cut.`).
- `services/acp_shared/token_budget.py`: sends `budget_retry=False`, uses the core's retry rule; `T9_REPAIR_MAX_TOKENS`.
- `services/content_generation/brand_fit.py`: `BrandFitResult.truncation_retry_max_tokens` (it carries every
  `BILLED_FIELD_KEYS` key).
- Tests: `tests/unit/test_s271_gateway_token_budget.py` (rules, floor on the wire, retry + ledger rows, opt-outs, the
  CI guard, the alert; review fix: a billed cut + throttled retry = one billed row + one 0 USD row, a 400 cut + 5xx
  retry = no context chain, breaker counts it, row status error; refused / eval cuts named in the alert);
  `tests/integration/test_s271_cut_rate_db.py` (refused cut and eval rows counted, other unbilled rows not).

## Tradeoffs
- A retry doubles the cost of a request whose reply was going to be cut anyway; the cut reply was thrown away before
  (or worse: a weaker fallback answered). Bounded: one retry per route model, capped ceiling, attempt cap.
- The floor raises the central budget's worst-case estimate for reasoning models (it now uses the floored ceiling, the
  truth). Dev caps are alert-only. The TPM capacity estimate still uses the call site's ceiling (unchanged).
- The code rule for "reasoning-capable" is by model key; a new family outside it needs `capabilities.reasoning` on its
  catalog row. The guard test fails on a model key it cannot classify among the keys the migrations name.
- The guard ratchets literal sub-floor ceilings (a list of today's, each with a reason) instead of forcing every
  ceiling to 4096: several are on non-reasoning routes and a higher ceiling would only raise their TPM reservation.

## Should know
- Reading the ledger after deploy: cut rows = `status = 'truncated'` or (`status = 'billed_unusable'` and
  `stop_reason IN ('max_tokens','length','model_context_window_exceeded')`); gateway retries =
  `quality_signal ? 'budget_cut' AND quality_signal ->> 'gateway_attempt' = 'true'` (cut) and
  `quality_signal ->> 'truncation_retry' = 'true'` (the retry, written by the caller).
- A stage that wants to own its retry sets `budget_retry=False` and must be added to the guard's allowlist with a reason.
- When the retry raises too, the route's error carries the retry's failure only; the cut attempt's cost is in the
  ledger (its own row) but not in `unusable_reply_cost()` of that error (a caller's run budget reads one attempt less).
- Verified locally: the gateway unit subset (~7.5k tests incl. the six updated for the new behaviour), the new unit and
  integration files, the route trace / ledger v2 / writing-memory integration tests on Postgres, flake8 (CI scope),
  the resource-ID and golden-text guards, the prompt registry check, gitleaks 8.30.1 on the branch.
