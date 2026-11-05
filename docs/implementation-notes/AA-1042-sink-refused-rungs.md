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

(filled in as the work goes)

## Changed

## Tradeoffs

## Should know
