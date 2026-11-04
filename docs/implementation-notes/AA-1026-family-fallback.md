# AA-1026 — family fallback chains (S267, lane llm-family-fallback)

Tech lead request (S267, 04/11): give every route an ordered chain by model family, so a model that is refused or
fails falls to the next one. Live facts of the probe (04/11): on acc3, Claude Sonnet 4.6, Claude Haiku 4.5 and Cohere
Embed v4 answer; every 5.x Claude, GPT-6 Luna, GPT-5.6 Luna and GPT-6 Astra are refused ("not available for this
account", AWS case open). acc2 Nova / Llama / Kimi and the OpenAI API answer; the Anthropic API is off (no key).

Two PRs: (A) the gateway runs a `vendor_direct` route AWS first across all its models, the chain rules as code
(`shared/llm_client/route_chains.py`), the $0 check script and the tests; (B) migration 366: two catalog rows and the
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
  (`route_chains.CHECK_PAIRS`); t10_claims and a3_place_card get no Claude hop.
- **Judges unchanged.** They already chain GPT-5.6 Luna ↔ GPT-6 Luna, then Nova Lite on acc2, then (vendor_direct) the
  OpenAI API; Claude is the writer family (TL-10), so no 4.x hop.
- **Rule set as code.** `route_chains.violations` (no_fallback, vendor_on_aws_only, vendor_not_last,
  judge_writer_family, writer_family, writer_acc2, check_pair_family, attempt_cap) runs in the unit tests on the decided
  table and in `scripts/llm_ops/route_chain_check.py` on the live rows; migration 366 RAISEs on the same rules for the
  rows it changes.

## Changed

- `shared/llm_client/client.py`: `route_attempts` + `ROUTE_AWS_FIRST_ENV`; `_call_route` binds the pass scope per attempt.
- `shared/llm_client/vendor_route.py`: `route_reach` (usable / described scopes of a model, no circuit or budget read).
- `api/routers/admin_llm_ops.py`: `_check_aws_before_vendor` returns early with the AWS-first order on.
- `shared/llm_client/route_chains.py` (new): `violations`, `resolve_stage`.
- `scripts/llm_ops/route_chain_check.py` (new): $0 live check (FIRST model@connection per stage + violations).
- Tests: `tests/unit/test_aa1026_family_fallback_chains.py` (new), `test_aa1026_admin_writer_last_resort.py`
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

## Should know

- Merge order: PR A (code) deployed BEFORE migration 366 is applied. Without the AWS-first order a vendor_direct
  writer would call the Anthropic API before Sonnet 4.6 on acc3 once that connection is switched on (today it is off,
  so nothing would leak, but the order rule must hold before the key arrives).
- The TripPlanner gateway reads tp_compose's route through `catalog.v_llm_gateway_config_v1`; it calls Converse rows
  itself and stays on AWS, so the two new rows need no TripPlanner change.
- After the apply: POST /admin/writing-memory/releases/reverify-scan (writer routes changed outside the admin PATCH)
  and the root route page (`tools/docs/gen_llm_routes.py`).
- Open question for the tech lead: should large-tier writers (s1_generate) prefer GPT-6 Luna on the OpenAI API over
  Haiku 4.5 on acc3? Today's table puts Haiku 4.5 first (AWS first, same family).
