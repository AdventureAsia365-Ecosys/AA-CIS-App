# AA-900 / AA-741 — S271 W0, gateway part (lane l13-w0-gateway)

Source: root `docs/audits/2026-11-06-S271-writing-quality-audit/` (`data/design-review.json` arch.roadmap[0] items 1-2,
`data/blog-pipeline-audit.json` family 1 "Output-token cuts and silent model swaps").

## Decisions

- **One owner of the truncation retry: the gateway.** `services/acp_shared/token_budget.py` no longer sets
  `budget_retry=False` and no longer retries. Its old retry only saw a cut reply that came back with text; an EMPTY
  reply cut at max_tokens (a billed unusable reply: Sonnet 5.5's adaptive thinking used every token) raised inside
  the route, which moved silently to Sonnet 4.6 (3 of 3 in the 06/11 ledger). The gateway already detects that case
  (`token_floor.is_cut_failure` reads the billed record's stop reason on every provider path: Anthropic Messages
  `unusable()`, Converse `billed_failure()`, OpenAI `billed_failure()`); it only needed the opt-out gone.
- **`generate_with_budget_retry` keeps its name and shape** (one gateway call, `BudgetedReply`), so the six call sites
  and the AA-927 call-site guard change little. `on_cut` and `cut_signal` are gone: the cut row is the gateway's.
- **`retry_reason`** (`token_floor.RETRY_REASONS`: `cut_reply`, `billed_unusable_cut`, `truncated_refusal`) goes on
  the retry's row (`quality_signal.retry_reason`, through `billed_fields`) and on the gateway's cut row.
- **`LLMResponse.truncation_cut_cost_usd`**: what the retried cut attempt cost, so `BudgetedReply.cost_usd` still
  reports every billed reply of the request (piece cost unchanged). Not a ledger key (the cut row has its cost).
- **token_floor(model, effort) = `token_floor.effort_floor`** in CIS only (not `shared/aa_llm/tokens.py`), so no
  core copy / MANIFEST / consumer-app change. Floors: low 4096, medium / high / default 8192, xhigh / max 16000; the
  catalog's `min_output_tokens` wins when higher; never above the model cap. Evidence in the module comment.
- **Static lint, runtime unchanged.** The runtime floor stays `tokens.floored` (effort-agnostic). The lint
  (`tests/unit/test_aa900_s271_effort_floor_lint.py`) reads each stage's route from the newest `[stage, primary,
  [old], [new]]` row of the route migrations (366, 376), effort rows from `stage_model_params` seeds (381) and
  `min_output_tokens` from catalog migrations (382).
- **Fix pass (review finding): the lint covers every routed stage (34), not a hand-picked 8.** The call sites come
  from an AST scan of api/ services/ shared/ worker/ (`LLMRequest` / `judge_request` / `call_judge` /
  `invoke_judge`); it reads stage and ceiling from literals, module or imported constants, parameter defaults, local
  assignments, both arms of `a if c else b` and `stage_for(state, "s1_x")` (S1 stage + T2 twin); a call without
  max_tokens counts as `LLMRequest`'s default (4096). What it cannot read is declared (`DECLARED_SITES`: stages served
  and lowest ceiling), exempt (`EXEMPT_SITES`: probes, route tests, legacy `model_tier="haiku"` calls), or the stage
  has no call site here (`NO_CALL_SITE`: tp_compose, segment_research(_batch), t10_claims). An unread new site fails,
  a routed stage with no site fails, and every (stage, site) under the effort floor must be on the shrinking
  `UNDER_EFFORT_FLOOR` list with a reason (27 entries today; stale entries fail).
- **Raised, not allowlisted:** `faq_answer_fit.MAX_TOKENS` 1500 -> 4096 and `ISOLATED_MAX_TOKENS` 150 -> 4096
  (t9_faq_answer_fit: Sonnet 5.5 at effort low); `seed_align/adjudicate.MAX_TOKENS` 4000 -> 8192 (t10_adjudicate:
  GPT-5.6 Luna has no catalog floor and was sent 4096). Their `SUB_FLOOR_CEILINGS` lines in
  test_s271_gateway_token_budget.py are gone. The other 27 stay allowlisted: raising them all is a behaviour change
  across A1 / A3 / places outside this lane's scope; each is a one-line follow-up (raise the constant, delete the
  line).
- **Degraded marks** (`services/quality/route_guard.answer_marks`, called by `client._mark_degraded_answer` after the
  route answered, before the reply is traced): `fallback_instrument` (reason `rung` for a judge / validate / decide
  answer at route position > 0; reason `uncalibrated_model` for nova-lite at any position), `same_family_pair`
  (a judge whose family is shared with a writer reply of the same output), and `fallback_writer` on the reply for a
  writer at position > 0 (the AA-1042 writer note is unchanged). Marks go on `LLMResponse.route_degraded` and into
  `quality_signal.degraded`; instrument marks are also noted in the job collector and the bound output collector.
- **The family trace is bound per output by `route_guard.collect_output()`** (T9 per piece and T2 per version already
  bound it; S1 now binds it per tour in `v1_pipeline._rewrite_tour`, A1 only). `family_rule.trace_models(reuse=True)`
  keeps an eval runner's outer trace (R2, the S1 regression eval) so their own family refusal still sees every reply.
- New degraded names `fallback_instrument` and `same_family_pair`, severity `count`.

## Changed

- `shared/llm_client/client.py`: retry reason + cut cost on the retry's reply; `_mark_degraded_answer`; the route
  stamps `route_position` on an answered reply that has none (test doubles; the real `_attempt` already sets it).
- `shared/llm_client/token_floor.py`: `RETRY_*`, `effort_floor`, `cut_failure_reason`, `cut_failure_cost`, docs.
- `shared/llm_client/call_log.py`: `RETRY_KEYS` + `truncation_retry_reason`, `DEGRADED_KEYS` (`route_degraded`,
  sanitised and capped), folded into quality_signal by `record_call*` / `record_calls`.
- `shared/llm_client/models.py`, `family_rule.py`, `services/content_generation/brand_fit.py` (the two new billed keys).
- `services/acp_shared/token_budget.py`: ceiling calculator. **Ceilings raised** to the effort floor the lint found:
  `T8_ANGLE_GEN_MAX_TOKENS` 4096 -> 8192, `T9_WRITE_DEFAULT_MAX_TOKENS` 4096 -> 8192,
  `services/writing_memory/eval_judge.MAX_TOKENS` 4096 -> 8192 (the audit asked for it too).
- Callers: T8 `generate_angles`, T9 `_generate_full` / `repair_sentences_detailed` / `fit_seo_fields` / `write_faq`,
  `faq_filter.llm_filter_questions` (dropped `_log_cut` / `on_cut`).
- `services/quality/degraded.py`, `route_guard.py`, `services/jobs/t2_rewrite_job.py`, `api/routers/v1_pipeline.py`.

## Tradeoffs

- **The gateway's cut row has no `content_piece_id` / `angle_gate_request_id`** (it has job, tenant and the
  `route_trace` the caller's row shares). Before W0 the caller's own cut row carried them for cuts that came back with
  text. A per-piece cost read by `content_piece_id` (api/services/ai_gateway.py) now misses the cut attempt's cost;
  join by `route_trace` or `job_id`. The billed_unusable rows of empty cuts already had this gap before W0. A follow-up
  could bind the piece id in a contextvar the gateway rows read.
- **A retried empty cut costs one more call on the same model** (about $0.13 cut + up to $0.26 at a 24k retry for
  t9_write) instead of a cheaper Sonnet 4.6 answer; the point is that the measured piece comes from the routed
  model. If the retry fails too, the route still moves on (now marked `fallback_writer` / `fallback_instrument`).
- **Marks are `count`**: no new holds in W0 (lane rule). F9 answered by rung 3 (all 72 verdicts since 05/11) will
  flag every T9 piece as degraded (non-blocking flag + repair_log entry, admin-only). That is the intended visibility;
  holding on it is a later W0/W2 decision (map `fallback_instrument` on t10_judge to `f9_judge_unavailable`).
- **The lint parses migrations**: a route changed in another SQL shape is not seen (a stage with a call site and no
  route row is skipped, not failed).
- **The lint reads constants, not call graphs**: a parameter default names one stage; the other stages the callers
  pass are declared by hand in `DECLARED_SITES`. A routed stage the declarations miss still fails the coverage test.
  The contract smoke job and the judge client are skipped (they reuse the production constants / are not sites).

## Should know

- What each caller does with an instrument mark: T9 `_degraded_final` -> `degraded` flag (counts) + repair_log entry,
  never held; S1 `with_a1_entries` -> `state.degraded` (stored in `generated_content.metadata.degraded`),
  `apply_a1_hold` unchanged (hard only); T2 -> `judge_breakdown.route_degraded` (admin-only), `t2_mark` /
  `fallback_writer` held label unchanged (writer notes only); every job -> `shared.job.result.degraded` counts.
- `budget_retry` stays on `LLMRequest` for tests; the guard allowlist of production files is now empty.
- Pre-existing, not from this lane: `tests/unit/test_aa804_ledger_v2.py` has two tests that fail only when run after
  certain other files in one process (same on origin/main); CI's file sharding does not hit that order.
- No migration, no route change, no paid call. Live check after deploy: next T9 / T8 writes on Dev show no
  `fallback_used` row after a `billed_unusable` max_tokens row of the same `route_trace` on sonnet-5-5 (it should be a
  `truncation_retry` row on sonnet-5-5 with `retry_reason`).
