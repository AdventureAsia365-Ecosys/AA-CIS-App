# AA-1026 — family fallback chains (S267, lane llm-family-fallback)

Tech lead request (S267, 04/11): give every route an ordered chain by model family, so a model that is refused or
fails falls to the next one. Live facts of the probe (04/11): on acc3, Claude Sonnet 4.6, Claude Haiku 4.5 and Cohere
Embed v4 answer; every 5.x Claude, GPT-6 Luna, GPT-5.6 Luna and GPT-6 Astra are refused ("not available for this
account", AWS case open). acc2 Nova / Llama / Kimi and the OpenAI API answer; the Anthropic API is off (no key).

Two PRs: (A) the gateway runs a `vendor_direct` route AWS first across all its models, the chain rules as code
(`api/services/llm_route_chains.py`), the $0 check script and the tests; (B) migration 366: two catalog rows and the
new chains.

## Decision table (stage -> old chain -> new chain -> why)

Primary models never change (s1_generate stays Sonnet 5.5, S250; TL-11 eval freeze: no prompt or learning change).
`sonnet-4-6` / `haiku-4-5` are the new Converse catalog rows of migration 366 (the Sonnet 4.6 / Haiku 4.5 models the
legacy keys call, on the same acc3 profiles). "OpenAI" = `gpt-6-luna-openai` (OpenAI API, vendor_direct).

| Stage | Role | Old chain (live 04/11) | New chain | Why |
|---|---|---|---|---|
| s1_generate, supply_content_writer, t2_generate, t8_angle_gen, t9_write | writer (vd) | sonnet-5-5 → OpenAI | sonnet-5-5 → sonnet-4-6 → haiku-4-5 → OpenAI | Claude large tier, its working 4.6 sibling, then the cheaper Claude tier, OpenAI last |
| tp_compose | writer (aws) | sonnet-5-5 (none) | sonnet-5-5 → sonnet-4-6 → haiku-4-5 | had no fallback; TripPlanner stays on AWS (T-11) |
| a0_column_map, s1_flag_fix, s1_itinerary_nudge, t2_flag_fix, t2_itinerary_nudge, t9_repair | writer (vd) | haiku-5-5 → OpenAI | haiku-5-5 → haiku-4-5 → sonnet-4-6 → OpenAI | Claude small tier, its 4.5 sibling, then up one tier, OpenAI last |
| segment_research, segment_research_batch | writer (aws) | haiku-5-5 (none) | haiku-5-5 → haiku-4-5 → sonnet-4-6 | had no fallback |
| a3_atomize, s1_source_facts | validate | gpt-6-luna → haiku-5-5 → nova-lite | gpt-6-luna → gpt-5.6-luna → haiku-5-5 → haiku-4-5 → nova-lite | GPT sibling, Claude siblings, acc2 last (other failure domain) |
| a3_place_card_check | validate | haiku-5-5 → sonnet-5-5 → llama4-scout | haiku-5-5 → haiku-4-5 → sonnet-5-5 → sonnet-4-6 → llama4-scout | siblings; stays Claude + Meta (its producer a3_place_card is GPT + Amazon) |
| a3_place_days | validate | haiku-5-5 → sonnet-5-5 → nova-lite | haiku-5-5 → haiku-4-5 → sonnet-5-5 → sonnet-4-6 → nova-lite | siblings |
| a3_place_locate, t9_faq_answer_fit | validate | sonnet-5-5 → haiku-5-5 → nova-lite | sonnet-5-5 → sonnet-4-6 → haiku-5-5 → haiku-4-5 → nova-lite | siblings |
| a3_place_dedupe | validate (vd) | gpt-6-luna → llama4-maverick → OpenAI | gpt-6-luna → gpt-5.6-luna → llama4-maverick → OpenAI | GPT sibling; stays GPT + Meta (its checker is Claude + Amazon) |
| a3_place_dedupe_check, t9_faq_filter | validate | haiku-5-5 → nova-lite | haiku-5-5 → haiku-4-5 → nova-lite | sibling |
| s1_judge, t2_judge, t7_debate_judge, t10_judge, t10_adjudicate, writing_memory_eval, writing_memory_mine, s1_brand_audit, t2_brand_audit | judge | GPT-5.6 Luna ↔ GPT-6 Luna → nova-lite (→ OpenAI) | unchanged | already a family chain; a judge may not use Claude (writer family, ADR 0009 d12 / TL-10) |
| a3_place_card, t10_claims | validate | gpt-6-luna → gpt-5.6-luna → nova-lite | unchanged | already a family chain; no Claude added: they check Claude-written or Claude-checked text |
| a3_place_embed, f10_embed, tp_search_embed | embed | cohere-embed-v4 → cohere-embed-v4-acc3 | unchanged | already two accounts |
| s1_atom_writer, tp_extract, tp_component_embed | (switched off, 336) | — | unchanged | not called |

Today (acc3 refuses 5.x and GPT): the writers answer on Sonnet 4.6 / Haiku 4.5 on acc3 instead of GPT-6 Luna on the
OpenAI API; the validate stages answer on Haiku 4.5 / Sonnet 4.6 on acc3 before acc2; the judges are unchanged
(Nova Lite on acc2). When AWS reopens 5.x, every stage is back on its primary with no change.

## Decisions

- **Converse catalog rows, not the legacy keys.** `sonnet` / `haiku` work on acc3 but 327 (T-4) moved every route off
  them on purpose: a legacy key runs the hard-coded `_legacy_chain` (no data scope, budget or breaker while
  `LLM_LEGACY_ALIASES` is off, lesson S260), drops `json_schema` (prompt-only schema, lesson S251) and degrades Sonnet
  to Haiku by itself. Migration 366 adds `sonnet-4-6` / `haiku-4-5`: api_style converse, family anthropic, copied
  prices / capabilities / temperature from the legacy rows, ONE deployment on acc3 with the legacy row's acc3 wire id.
  No Anthropic API deployment for them (306 ran before; the Anthropic API path stays the 5.x models).
- **AWS first across the whole route (gateway change).** The gateway tried every deployment of a model before the next
  model, so on a vendor_direct route `sonnet-5-5 → sonnet-4-6` would call Sonnet 5.5 on the Anthropic API (once its key
  is in) before Sonnet 4.6 on acc3 — against T-3 / ADR 0009 d5 ("outside only after every AWS deployment failed").
  That is why 327 / 344 had to drop the same-family degrade on the writers. `client.route_attempts` now splits a
  route of a stage allowed both scopes into an AWS pass (each model on its aws_only deployments, route order) and a
  vendor pass (each model on its vendor_direct deployments, route order). Switch `LLM_ROUTE_AWS_FIRST=off` restores
  the per-model order. With the routes before 366 both orders make the same calls (all route snapshot tests pass
  unchanged).
- **The admin rule `_check_aws_before_vendor` stands down while the AWS-first order is on** (it refused exactly the
  interleaving the gateway no longer makes). It still applies when the switch or deployment routing is off.
- **Large-tier writers skip Haiku 5.5.** Haiku 5.5 is refused together with Sonnet 5.5 (same generation, same AWS
  case), and with the Anthropic API switched on a five-model chain would pass the gateway's 6 attempts per request
  (`errors.MAX_CHAIN_ATTEMPTS`, ADR 0009 d5), so the OpenAI API last rung would never run. The `attempt_cap` rule pins it.
- **Families stay apart where a stage checks another's output.** a3_place_card (GPT, Amazon) / a3_place_card_check
  (Claude, Meta) and a3_place_dedupe (GPT, Meta) / a3_place_dedupe_check (Claude, Amazon) keep disjoint makers
  (`llm_route_chains.CHECK_PAIRS`); t10_claims and a3_place_card get no Claude hop.
- **Judges unchanged.** They already chain GPT-5.6 Luna ↔ GPT-6 Luna, then Nova Lite on acc2, then (vendor_direct) the
  OpenAI API; Claude is the writer family (TL-10), so no 4.x hop.
- **Connection circuit: a model refused on its own no longer blocks its siblings (review fix).** The connection
  circuit (AA-1018) trips on `access_denied` from 2 distinct models newer than the connection's last answered call.
  acc3 refuses Sonnet 5.5, Haiku 5.5, GPT-6 Luna and GPT-5.6 Luna one by one, and a route walks two of them before the
  4.x sibling (a3_atomize: three), so every half-open round the probes re-tripped the connection inside the request
  and Sonnet 4.6 / Haiku 4.5 on acc3 were skipped: the chains would still end on the OpenAI API or acc2, and once
  tripped no 4.x call could ever close it again. Fix in `shared/llm_client/breaker.py`: (1) a failed half-open probe
  keeps `opened_at` = the start of the refusal streak (`after_failure(keep_streak_start=True)`, LLM breaker only; the
  provider circuit keeps the old move because its reopen alerts read it); (2) an `access_denied` row whose streak
  began before the connection's last answered call is one model's refusal and does not count toward the trip. Two
  fresh refusals after an answer (an account-wide block such as Error 002) still trip it at once. The circuit closes
  only after one answered call on acc3: the live step is the admin Test connection on `sonnet-4-6` and `haiku-4-5`
  (`LLMClient.probe` counts on the breaker; a success sets the connection's last answered call). Rows written before
  this change carry the opened_at of their last reopening, which is still before that test, so they stop counting too.
- **Rule set as code.** `llm_route_chains.violations` (no_fallback, vendor_on_aws_only, vendor_not_last,
  judge_writer_family, writer_family, writer_acc2, check_pair_family, attempt_cap) runs in the unit tests on the decided
  table and in `scripts/llm_ops/route_chain_check.py` on the live rows; migration 366 RAISEs on the same rules for the
  rows it changes.

## Changed

- `shared/llm_client/client.py`: `route_attempts` + `ROUTE_AWS_FIRST_ENV`; `_call_route` binds the pass scope per attempt.
- `shared/llm_client/breaker.py`: failed probe keeps the streak start; model-scoped refusals do not trip the
  connection (review fix, see Decisions).
- `shared/llm_client/vendor_route.py`: `route_reach` (usable / described scopes of a model, no circuit or budget read).
- `api/routers/admin_llm_ops.py`: `_check_aws_before_vendor` returns early with the AWS-first order on.
- `api/services/llm_route_chains.py` (new): `violations`, `resolve_stage`.
- `scripts/llm_ops/route_chain_check.py` (new): $0 live check (FIRST model@connection per stage + violations).
- Tests: `tests/unit/test_aa1026_family_fallback_chains.py` (new; its last section runs the REAL breaker with acc3's
  refusals: before a 4.x success the chain ends on acc2, after one it reaches haiku-4-5@acc3 even after a quiet
  night, two fresh refusals still trip), `test_aa1018_connection_circuit.py` (pure cases of the new rule), `test_aa1026_admin_writer_last_resort.py`
  (refusal cases run with the switch off; new cases with it on); snapshots `aa1026_routes_before_366.json` /
  `aa1026_routes_after_366.json`.
- PR B: `api/migrations/366_llm_family_fallback_chains.sql`, `tests/integration/test_aa1026_migration_366.py`.

## Tradeoffs

- Writers degrading to Haiku 4.5 before the OpenAI API: a Claude reply of a smaller tier over GPT-6 Luna of another
  maker. Picked for the family rule (writers stay Anthropic, ADR 0009 d12) and AWS-first; the judges score whatever
  answered, as before.
- No contract smoke gate in 366 (unlike 331-333): the new rows are the same Claude models the legacy keys called on
  acc3 for months, and every chain degrades safely (a refused or unusable reply moves on). A connection test of the two
  rows is a live step after the apply.
- Price: the new rows copy the legacy catalog prices (Sonnet 4.6 3 / 15, Haiku 4.5 1 / 5 USD per 1M tokens), above
  their 5.5 siblings (migration 327) and above GPT-6 Luna on the OpenAI API (0.10 / 0.50, migration 344). While acc3
  refuses 5.x the writers spend that instead of the OpenAI API; the OpenAI API 10 USD cap is no longer their daily
  path. A wave on Sonnet 4.6 costs more per tour than one on GPT-6 Luna: report it per wave as usual.

## Cost estimate (review: quantify before the apply)

No DB read from this lane (no ECS exec), so the per-tour figures scale MEASURED costs by the catalog price ratio
(input and output ratios are equal for each pair below; token counts per call assumed unchanged across models).
Sources: wave of 50 tours on Sonnet 5.5 (`docs/audits/2026-10-24-S249-s1-wave50/README.md`, via the S257 Anthropic API
evaluation): `s1_generate` 0.0600 USD/tour, judge (Luna) 0.0054, flag_fix 0.0030, Jev 0.0032, atomize (Luna) 0.0052;
October gateway totals (`docs/audits/2026-11-01-S262-capacity-review/README.md`). Prices per 1M tokens in / out:
Sonnet 5.5 2 / 10, Sonnet 4.6 3 / 15, Haiku 5.5 0.10 / 0.50, Haiku 4.5 1 / 5, GPT-6 Luna (acc3 and OpenAI API)
0.10 / 0.50, Nova Lite 0.077 / 0.308.

| Per tour (USD) | Intended (5.x / Luna on acc3) | Today, before 366 (acc3 refuses: OpenAI API / acc2) | After 366 while acc3 refuses 5.x (Sonnet 4.6 / Haiku 4.5 on acc3) |
|---|---:|---:|---:|
| S1 writer `s1_generate` | 0.060 | 0.003 (GPT-6 Luna, OpenAI API) | **0.090** (Sonnet 4.6) |
| S1 `s1_flag_fix` / nudge | 0.0003 (Haiku 5.5) | 0.0003 (OpenAI API) | 0.003 (Haiku 4.5) |
| S1 judge + Jev | 0.009 | ~0.007 (Nova Lite + Jev) | ~0.007 (unchanged) |
| A3 `a3_atomize` | 0.005 (Luna) | ~0.004 (Nova Lite) | **0.052** (Haiku 4.5) |
| **S1 + A3** | **~0.075** | **~0.015** | **~0.15** |

`s1_source_facts` is not itemised in the wave report; it is Luna-priced, so the same x10 applies on Haiku 4.5.
Monthly: since 29/10 acc3 answers nothing and LLM spend is ~0. At October's volume (a catalogue rewrite of 810 tours
plus re-runs) S1 + A3 would be ~0.15 x 810 = ~120 USD for one full rewrite (vs ~12 USD on today's fallbacks and ~60 USD
on the intended 5.x path); a routine month of 100-200 regenerated tours is ~15-30 USD. Tenant writers (`t9_write`,
`t2_generate`, October 29 USD on Sonnet) move the same way (x1.5 vs Sonnet 5.5, x30 vs the OpenAI API). acc3 bills the
remaining AWS Lift credit (~250 USD, expires 31/12), then the card; the AWS budget `aa3-llm-monthly-total` (250 USD) only
e-mails. The gateway hard stop is `shared.llm_provider_connection.monthly_budget_usd` of `acc3` (cost policy, 317):
its live value is not checked from this lane. A hard stop on acc3 is itself a safe degrade: the chains then fall to
the OpenAI API (10 USD cap) or acc2.

## Questions for the tech lead (yes / no)

1. **Cost delta.** While acc3 refuses 5.x, the writers spend ~0.09 USD/tour on Sonnet 4.6 instead of ~0.003 on the
   OpenAI API (S1 + A3 ~0.15 vs ~0.015 USD/tour). Accept? (No = keep 366 but put `gpt-6-luna-openai` before the 4.x rows
   on the writer routes, question 2.)
2. **Writer order.** Should large-tier writers (s1_generate) prefer GPT-6 Luna on the OpenAI API over Sonnet 4.6 /
   Haiku 4.5 on acc3? Today's table puts the Claude 4.x rows first (AWS first, same family).
3. **acc3 cap.** Set `monthly_budget_usd` on `acc3` to 100 USD (proposed; hard stop -> chains fall to the OpenAI API /
   acc2) if it is NULL?

## Should know

- Merge order: PR A (code) deployed BEFORE migration 366 is applied. Without the AWS-first order a vendor_direct
  writer would call the Anthropic API before Sonnet 4.6 on acc3 once that connection is switched on (today it is off,
  so nothing would leak, but the order rule must hold before the key arrives).
- The TripPlanner gateway reads tp_compose's route through `catalog.v_llm_gateway_config_v1`; it calls Converse rows
  itself and stays on AWS, so the two new rows need no TripPlanner change.
- After the apply: POST /admin/writing-memory/releases/reverify-scan (writer routes changed outside the admin PATCH)
  and the root route page (`tools/docs/gen_llm_routes.py`).
- After the apply (live steps): admin Test connection on acc3 with `sonnet-4-6` and with `haiku-4-5` (ok=true; this
  records the answered call that closes the connection circuit), then the $0 report
  (`scripts/llm_ops/route_chain_check.py` through the ECS exec runner) must show FIRST = `sonnet-4-6@acc3` on
  s1_generate and `haiku-4-5@acc3` on a3_atomize, with no attempt reading "circuit open: acc3".
- ADR 0009 decision 5 amended (root PR, S267): the AWS-first order lets a same-family AWS degrade sit behind a model
  that has a vendor API deployment; `LLM_ROUTE_AWS_FIRST=off` brings back the old rule.
- Tech lead questions: see "Questions for the tech lead" above (cost delta, writer order, acc3 cap).
