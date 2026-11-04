# AA-1026 — family fallback chains (S267, lane llm-family-fallback)

Tech lead request (S267, 04/11): give every route an ordered chain by model family, so a model that is refused or
fails falls to the next one. Live facts of the probe (04/11): on acc3, Claude Sonnet 4.6, Claude Haiku 4.5 and Cohere
Embed v4 answer; every 5.x Claude, GPT-6 Luna, GPT-5.6 Luna and GPT-6 Astra are refused ("not available for this
account", AWS case open). acc2 Nova / Llama / Kimi and the OpenAI API answer; the Anthropic API is off (no key).

Two PRs: (A) the gateway's route order (model first, the tech lead's S267 answer; the AWS-first order kept behind a
switch, default off), the relaxed admin order rule, the connection-circuit fix, the chain rules as code
(`api/services/llm_route_chains.py`, incl. the stage -> product map), the $0 check script and the tests; (B) migration
366: two catalog rows, the new chains, `allowed_scopes = {aws_only, vendor_direct}` on every active text stage, the
acc3 monthly cap (100 USD) and the Anthropic API monthly cap (150 USD).

**Tech lead decisions S267 (04/11/2026), applied in this pass.** They replace decision T-3 (aws_only for tenant content,
TripPlanner and the CRM):

- **Policy: AWS preferred, not AWS only.** Every active text stage (writer, judge, validate) may reach a vendor API
  (the Anthropic API with the single `anthropic-direct` key, the OpenAI API) after its AWS deployments, so each stage
  runs the best available model. Embedding stages stay `aws_only` (no vendor embedding deployment exists).
- **Q2 order: model first** on every route (each model on acc3 by priority, then the same model on its vendor API,
  before the next model: the Anthropic API for Claude, the OpenAI API for a GPT twin if one exists). The AWS-first
  switch stays, default off. GPT-6 Luna's twin exists (`gpt-6-luna-openai`, fix pass 2): it sits right behind
  `gpt-6-luna` on every GPT route.
- **Keys: ONE Anthropic API key** for every product (connection `anthropic-direct`, 150 USD a month, alert 80 %, hard
  stop). Per-product tracking stays in our ledger (`shared.llm_call_log` per stage) and the admin Spend > By app page.
- **Q1 cost delta: ACCEPTED. Q3 acc3 cap: YES**, `monthly_budget_usd = 100` on `acc3` in migration 366 (alert 80 %,
  hard stop).

## Decision table (stage -> old chain -> new chain -> why)

Primary models never change (s1_generate stays Sonnet 5.5, S250; TL-11 eval freeze: no prompt or learning change).
`sonnet-4-6` / `haiku-4-5` are the new Converse catalog rows of migration 366 (the Sonnet 4.6 / Haiku 4.5 models the
legacy keys call, on the same acc3 profiles). "OpenAI" = `gpt-6-luna-openai` (OpenAI API, vendor_direct).

"Scopes" after 366: every active text stage below is `aws_only, vendor_direct` (S267); "was aws" marks the stages T-3
kept on `aws_only` until 366. Attempts = every deployment in scope with the Anthropic API ON (cap 6 per request).

| Stage | Role | Old chain (live 04/11) | New chain | Attempts | Why |
|---|---|---|---|---:|---|
| s1_generate, supply_content_writer, t2_generate, t8_angle_gen, t9_write | writer | sonnet-5-5 → OpenAI | sonnet-5-5 → sonnet-4-6 → haiku-4-5 → OpenAI | 5 | Claude large tier (acc3, then the Anthropic API), its working 4.6 sibling, then the cheaper Claude tier, OpenAI last |
| tp_compose | writer (was aws) | sonnet-5-5 (none) | sonnet-5-5 → sonnet-4-6 → haiku-4-5 → OpenAI | 5 | had no fallback; S267: same chain as the large writers (TripPlanner's gateway calls Bedrock only today, see Should know) |
| a0_column_map, s1_flag_fix, s1_itinerary_nudge, t2_flag_fix, t2_itinerary_nudge, t9_repair | writer | haiku-5-5 → OpenAI | haiku-5-5 → haiku-4-5 → sonnet-4-6 → OpenAI | 5 | Claude small tier, its 4.5 sibling, then up one tier, OpenAI last |
| segment_research, segment_research_batch | writer (was aws) | haiku-5-5 (none) | haiku-5-5 → haiku-4-5 → sonnet-4-6 → OpenAI | 5 | had no fallback; S267: same chain as the small writers |
| a3_atomize, s1_source_facts | validate (was aws) | gpt-6-luna → haiku-5-5 → nova-lite | gpt-6-luna → OpenAI (twin) → haiku-5-5 → haiku-4-5 → nova-lite | 6 | model first: GPT-6 Luna on acc3, then the same model on the OpenAI API; GPT-5.6 Luna dropped (refused with GPT-6 Luna on acc3, no OpenAI API row; the attempt cap); Claude siblings; acc2 last |
| a3_place_card, t10_claims | validate (was aws) | gpt-6-luna → gpt-5.6-luna → nova-lite | gpt-6-luna → OpenAI (twin) → gpt-5.6-luna → nova-lite | 4 | model first: the twin right behind GPT-6 Luna (no Claude: they check Claude-written or Claude-checked text) |
| a3_place_card_check | validate (was aws) | haiku-5-5 → sonnet-5-5 → llama4-scout | haiku-5-5 → haiku-4-5 → sonnet-4-6 → llama4-scout | 5 | one 5.x tier (see Decisions: attempt cap); stays Claude + Meta (its producer a3_place_card is GPT + Amazon) |
| a3_place_days | validate (was aws) | haiku-5-5 → sonnet-5-5 → nova-lite | haiku-5-5 → haiku-4-5 → sonnet-4-6 → nova-lite | 5 | one 5.x tier |
| a3_place_locate, t9_faq_answer_fit | validate (was aws) | sonnet-5-5 → haiku-5-5 → nova-lite | sonnet-5-5 → sonnet-4-6 → haiku-4-5 → nova-lite | 5 | one 5.x tier |
| a3_place_dedupe | validate | gpt-6-luna → llama4-maverick → OpenAI | gpt-6-luna → OpenAI (twin) → gpt-5.6-luna → llama4-maverick | 4 | model first (twin), GPT sibling; stays GPT + Meta (its checker is Claude + Amazon) |
| a3_place_dedupe_check, t9_faq_filter | validate (was aws) | haiku-5-5 → nova-lite | haiku-5-5 → haiku-4-5 → nova-lite | 4 | sibling |
| s1_brand_audit, t2_brand_audit | judge (was aws) | gpt-5.6-luna → gpt-6-luna → nova-lite | gpt-5.6-luna → gpt-6-luna → OpenAI (twin) → nova-lite | 4 | model first: GPT-6 Luna on acc3 then on the OpenAI API, then acc2; never Claude (writer family, ADR 0009 d12 / TL-10) |
| s1_judge, t2_judge, t7_debate_judge, t10_judge, writing_memory_eval, writing_memory_mine | judge | gpt-5.6-luna → gpt-6-luna → nova-lite → OpenAI | gpt-5.6-luna → gpt-6-luna → OpenAI (twin) → nova-lite | 4 | fix pass 2: the twin moves from last to right behind GPT-6 Luna (333 had put Nova Lite before it under T-3) |
| t10_adjudicate | judge | gpt-6-luna → gpt-5.6-luna → nova-lite → OpenAI | gpt-6-luna → OpenAI (twin) → gpt-5.6-luna → nova-lite | 4 | the same, with GPT-6 Luna as primary |
| a3_place_embed, f10_embed, tp_search_embed | embed | cohere-embed-v4 → cohere-embed-v4-acc3 | unchanged (aws_only) | 2 | already two accounts; no vendor embedding deployment |
| s1_atom_writer, tp_extract, tp_component_embed | (switched off, 336) | — | unchanged (aws_only) | — | not called |

Today (acc3 refuses 5.x and GPT, the Anthropic API key not stored): the writers answer on Sonnet 4.6 / Haiku 4.5 on
acc3 instead of GPT-6 Luna on the OpenAI API; the Claude validate stages answer on Haiku 4.5 / Sonnet 4.6 on acc3
before acc2; every GPT stage (the judges, the brand audits, a3_atomize, s1_source_facts, a3_place_card,
a3_place_dedupe, t10_claims) answers on GPT-6 Luna through the OpenAI API (its twin) instead of Nova Lite / Llama on
acc2 or Haiku 4.5, until the OpenAI API's 10 USD monthly cap (327), then acc2 / Claude 4.x. Once the Anthropic API key is stored and the connection is on,
every stage whose primary is Claude 5.x (writers and Claude validate stages, tenant and TripPlanner included) answers
on that same model through the Anthropic API before its 4.x sibling. When AWS reopens 5.x, every stage is back on its
primary on acc3 with no change.

## Decisions

- **Converse catalog rows, not the legacy keys.** `sonnet` / `haiku` work on acc3 but 327 (T-4) moved every route off
  them on purpose: a legacy key runs the hard-coded `_legacy_chain` (no data scope, budget or breaker while
  `LLM_LEGACY_ALIASES` is off, lesson S260), drops `json_schema` (prompt-only schema, lesson S251) and degrades Sonnet
  to Haiku by itself. Migration 366 adds `sonnet-4-6` / `haiku-4-5`: api_style converse, family anthropic, copied
  prices / capabilities / temperature from the legacy rows, ONE deployment on acc3 with the legacy row's acc3 wire id.
  No Anthropic API deployment for them (306 ran before; the Anthropic API path stays the 5.x models).
- **AWS preferred, not AWS only (tech lead S267, replaces T-3).** Migration 366 sets `allowed_scopes = {aws_only,
  vendor_direct}` on every ACTIVE stage whose role is writer, judge or validate (15 stages were `aws_only`: the tenant
  validate / brand-audit stages, `tp_compose`, the segment research pair, the A3 place and atomize stages). The
  embedding stages and the three routes 336 switched off keep `{aws_only}`. The chain rule `vendor_on_aws_only`
  (S263) is replaced by `aws_only_text_stage` (an active text stage without `vendor_direct`); the admin's
  `_check_scopes` still refuses a model a stage's scopes can never call (it now only matters for embeddings).
  No code path special-cases tenant or TripPlanner stages, so the scope row is the whole switch.
- **Route order: MODEL FIRST (tech lead S267, Q2).** The tech lead's answer: "prefer the same model first, then go
  outside; e.g. acc3 Sonnet 5.5 >> API Sonnet 5.5 >> acc3 Sonnet 4.6 / Haiku ...". So the gateway keeps its per-model
  order as the default: every deployment of a model (`vendor_route.deployment_plan`: acc3 by priority, then the same
  model on its vendor API) before the route's next model. With the Anthropic API on, s1_generate runs Sonnet 5.5 on
  acc3, Sonnet 5.5 on the Anthropic API, Sonnet 4.6 on acc3, Haiku 4.5 on acc3, then GPT-6 Luna on the OpenAI API.
  The lane's first version made the AWS-first order (every AWS deployment of the whole route before any vendor API)
  the default; it is kept behind `LLM_ROUTE_AWS_FIRST=on` (`client.route_attempts`, default OFF) rather than removed:
  a one-line roll-back if the tech lead wants AWS first again, and it costs nothing while off (the function returns
  the plain chain). Since every text stage is vendor_direct (above), only the embedding stages are aws_only. The
  Claude 5.x rows carry both deployments themselves (306 seeded their Anthropic API rows), so the gateway's per-model
  order covers them. GPT-6 Luna's OpenAI API path is a separate catalog row, see the next bullet.
- **GPT-6 Luna's vendor twin (fix pass 2, review finding 1).** The first S267 pass read Q2's "openai-direct for a
  GPT twin if one exists" as "an OpenAI API deployment row on `gpt-6-luna`" (there is none) and kept
  `gpt-6-luna-openai` last on every GPT route, or left it out (a3_atomize, s1_source_facts). But the catalog says the
  two keys are the same model (migration 170: label "GPT-6 Luna (OpenAI API)", wire model `gpt-6-luna`, note "Same
  model as gpt-6-luna, served by the OpenAI API"), so that twin exists, and with acc3 refusing both GPT Luna models
  the GPT stages answered on their weakest model (Nova Lite / Llama on acc2, Haiku 4.5) instead of GPT-6 Luna on the
  OpenAI API. Fix: `data_scope.vendor_twins(models, reach)` names a model reached only outside AWS the TWIN of a row
  reachable inside AWS when both have the same family and the vendor row's `wire_model` names the same base model
  (`data_scope.base_model_id`: no Bedrock region prefix, no vendor prefix, no version suffix) as one of the AWS row's
  Bedrock profiles or its `wire_model`; today exactly `gpt-6-luna-openai` -> `gpt-6-luna`. A twin RIGHT BEHIND its
  AWS model (`data_scope.in_twin_slot`) is part of that model's slot: `llm_route_chains` `vendor_not_last`, the
  admin `_check_aws_before_vendor` and the route PATCH's "vendor-only only as the last fallback" check, and the 366
  guard (the same rule in SQL, twin pairs listed in a NOTICE) accept it ahead of the next AWS model. Anywhere else a
  vendor-only model is still the end of the route, never the primary or the shadow. Derived from catalog data, not a
  new column: no schema change in 366 and nothing to keep in sync; a new twin row (e.g. a future GPT-5.6 Luna on the
  OpenAI API with wire model `gpt-5.6-luna`) is recognised by the same rule. 366 then puts the twin right behind
  `gpt-6-luna` on every GPT route: a3_place_card / t10_claims / a3_place_dedupe / t10_adjudicate (gpt-6-luna, twin,
  gpt-5.6-luna, acc2), the eight judges incl. the brand audits (gpt-5.6-luna, gpt-6-luna, twin, nova-lite: the
  seven judges are now changed routes too) and a3_atomize / s1_source_facts (gpt-6-luna, twin, haiku-5-5,
  haiku-4-5, nova-lite: GPT-5.6 Luna dropped, it is refused with GPT-6 Luna on acc3 and has no OpenAI API row, the
  same reasoning that drops Haiku 5.5 from the large writers; that keeps them at 6 attempts).
- **Admin rule `_check_aws_before_vendor` relaxed (S267).** S263 refused an AWS model behind any model with a vendor API
  deployment: exactly the model-first interleaving now decided. It now refuses only a model reached ONLY outside AWS
  (every deployment on a vendor API, e.g. `gpt-6-luna-openai`) ahead of a model served inside AWS, unless it sits
  right behind its AWS twin (fix pass 2, above), the same rule as `llm_route_chains` `vendor_not_last` and migration
  366's guard; independent of the switch and of deployment routing.
  Both PATCH endpoints already refuse a vendor-only model as primary or non-last fallback, so the rule is the backstop.
- **Large-tier writers skip Haiku 5.5.** Haiku 5.5 is refused together with Sonnet 5.5 (same generation, same AWS
  case), and with the Anthropic API switched on a five-model chain would pass the gateway's 6 attempts per request
  (`errors.MAX_CHAIN_ATTEMPTS`, ADR 0009 d5), so the OpenAI API last rung would never run. The `attempt_cap` rule pins it.
- **Attempt cap re-checked with the Anthropic API ON and every text stage vendor_direct (S267): the CHAINS were
  adjusted, the cap stays 6.** Large writers and tp_compose: sonnet-5-5@acc3, sonnet-5-5@anthropic-direct,
  sonnet-4-6@acc3, haiku-4-5@acc3, gpt-6-luna-openai@openai-direct = 5; small writers and segment research the same 5.
  Opening the validate stages to vendor_direct gives their 5.x Claude rows a second (Anthropic API) attempt each, so
  the first 366 draft's validate chains with BOTH 5.x tiers (a3_place_card_check, a3_place_days, a3_place_locate,
  t9_faq_answer_fit: e.g. haiku-5-5 x2, haiku-4-5, sonnet-5-5 x2, sonnet-4-6, llama4-scout = 7) would never reach their
  acc2 last rung. Each keeps only its primary's 5.x tier (the other 5.x tier is refused together with it on acc3, the
  same reason the large writers skip Haiku 5.5): 5 attempts. a3_atomize / s1_source_facts are exactly 6 (gpt-6-luna,
  its OpenAI API twin, haiku-5-5 x2, haiku-4-5, nova-lite; fix pass 2 swapped GPT-5.6 Luna for the twin). Every other
  stage is at most 5 (the GPT routes 4). Raising `errors.MAX_CHAIN_ATTEMPTS` was not
  chosen: it is ADR 0009 decision 5's bound on a request's time and cost and would need its own decision. The order
  does not change the count; the `attempt_cap` rule (unit test on the snapshot, migration 366 guard, live check script)
  and the captured calls (at most 6 per decided stage in both connection states) pin it.
- **Families stay apart where a stage checks another's output.** a3_place_card (GPT, Amazon) / a3_place_card_check
  (Claude, Meta) and a3_place_dedupe (GPT, Meta) / a3_place_dedupe_check (Claude, Amazon) keep disjoint makers
  (`llm_route_chains.CHECK_PAIRS`); t10_claims and a3_place_card get no Claude hop.
- **Judges: GPT family first.** They chain GPT-5.6 Luna, GPT-6 Luna on acc3, GPT-6 Luna on the OpenAI API (twin),
  then Nova Lite on acc2 (fix pass 2: the GPT family before Amazon, model first); the two brand audits (aws_only until
  366) get the same chain. Claude is the writer family (TL-10), so no 4.x hop and no Anthropic API on a judge. The GPT
  / Amazon validate stages a3_place_card and t10_claims get the twin right behind GPT-6 Luna (no Claude: they check
  Claude text or are checked by Claude).
- **One Anthropic API key; per-product tracking in our ledger (S267).** Migration 366 sets `monthly_budget_usd = 150`
  on `anthropic-direct` (when NULL or higher) and its cost policy row (alert 80 %, hard stop), as 327 did for the OpenAI
  API. The split by product stays ours: every ledger row carries its stage (and tenant), and
  `llm_route_chains.product_of` / `ledger_app` map every stage to catalogue (A-series, segment research, f10), tenant
  content (T-series), TripPlanner (tp_*), Booking (`supply_content_writer`) or shared (writing memory), and to the
  `shared.llm_call_log.app` the Spend > By app page groups by (`tripplanner` for tp_*, `cis` for every stage the CIS
  gateway calls). A unit test keeps every route stage mapped; the check script lists `unmapped_stages`. Gaps: see
  Should know.
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
- **Rule set as code.** `llm_route_chains.violations` (no_fallback, aws_only_text_stage, vendor_not_last,
  judge_writer_family, writer_family, writer_acc2, check_pair_family, attempt_cap) runs in the unit tests on the decided
  table and in `scripts/llm_ops/route_chain_check.py` on the live rows; migration 366 RAISEs on the same rules for the
  rows it changes.

## Changed

- `shared/llm_client/client.py`: `route_attempts` + `ROUTE_AWS_FIRST_ENV` (default off since the S267 answer: model
  first); `_call_route` binds the pass scope per attempt when the switch is on.
- `shared/llm_client/breaker.py`: failed probe keeps the streak start; model-scoped refusals do not trip the
  connection (review fix, see Decisions).
- `shared/llm_client/vendor_route.py`: `route_reach` (usable / described scopes of a model, no circuit or budget read).
- `shared/llm_client/data_scope.py` (fix pass 2): `base_model_id`, `vendor_twins`, `in_twin_slot` (the vendor twin
  of an AWS model, from catalog data only).
- `api/routers/admin_llm_ops.py`: `_check_aws_before_vendor` relaxed (only a vendor-only model ahead of an AWS model,
  not its twin right behind it); the route PATCH's "vendor-only only as the last fallback" check accepts the twin in
  its model's slot (fix pass 2).
- `CONTEXT.md`: the A-series writers paragraph states the model-first order.
- `api/services/llm_route_chains.py` (new): `violations` (S267: `aws_only_text_stage` replaces `vendor_on_aws_only`),
  `resolve_stage`, `product_of` / `ledger_app` (stage -> product / ledger app).
- `scripts/llm_ops/route_chain_check.py` (new): $0 live check (FIRST model@connection per stage, product / app per
  stage, unmapped stages, violations).
- `CONTEXT.md`: the S267 policy paragraph (AWS preferred, one Anthropic key, product map, what TripPlanner / CRM still
  lack).
- Tests: `tests/unit/test_aa1026_family_fallback_chains.py` (new; its last section runs the REAL breaker with acc3's
  refusals: before a 4.x success the chain ends on acc2, after one it reaches haiku-4-5@acc3 even after a quiet
  night, two fresh refusals still trip), `test_aa1018_connection_circuit.py` (pure cases of the new rule), `test_aa1026_admin_writer_last_resort.py`
  (the relaxed rule in both switch states; fix pass 2: the twin in its model's slot saves, anywhere else it is
  refused); snapshots `aa1026_routes_before_366.json` /
  `aa1026_routes_after_366.json` (S267: new chains and the text-stage scopes; the route capture now runs each stage
  with the snapshot's own `allowed_scopes` instead of 311's backfill).
- PR B: `api/migrations/366_llm_family_fallback_chains.sql` (S267: the scope widening of every active text stage, the
  adjusted validate chains, GPT-6 Luna's OpenAI API twin right behind it on every GPT route (fix pass 2: the seven
  judges and t10_adjudicate are changed routes too), the acc3 100 USD and anthropic-direct 150 USD caps with their
  cost policy rows, the `aws_only_text_stage` rule and the twin exemption of `vendor_not_last` in its guard),
  `tests/integration/test_aa1026_migration_366.py` (34 decided stages, 15 widened, the caps, an admin's lower cap and
  policy kept, the anthropic-direct guard, the twin NOTICE, and the guard refusing the same chains when
  gpt-6-luna-openai is no twin). Older migration tests that assert the replay's end state follow 366:
  `test_aa1002_migration_311.py` (an active text stage is vendor_direct since 366), `test_aa1026_migration_327.py`
  (the budget test starts from the 327 fixture routes: 327's guard refuses the post-366 scopes),
  `test_aa784_migration_286.py`, `test_aa803_migration_259.py`, `test_aa835_migration_278.py`,
  `test_aa835_migration_282.py` (its restore sets the post-366 row instead of re-running 327); fix pass 2:
  `test_aa917_migration_280.py` (writing_memory_mine's post-366 route) and `test_aa1017_migration_324.py` (an acc2
  candidate still ends a judge / validate route; the OpenAI API twin may now stand before it).

## Tradeoffs

- AWS preferred, not AWS only (S267): tenant, TripPlanner and validate content may now leave AWS (the Anthropic API,
  the OpenAI API) when the AWS paths of a model are refused or fail. Picked for quality (the primary model keeps
  answering); the cost is a wider provider list in the tenant terms (AI Schedule / DPA, AA-820: root AI Use Register
  follow-up for the business owner) and vendor spend that only the connection caps bound.
- Validate chains with one 5.x Claude tier instead of two (attempt cap): when acc3 refuses 5.x and the Anthropic API is
  also down, a3_place_locate loses Haiku 5.5 (refused on acc3 anyway) and a3_place_card_check / a3_place_days lose
  Sonnet 5.5; the 4.x siblings and acc2 still follow.
- GPT stages on the OpenAI API before GPT-5.6 Luna / acc2 (fix pass 2, model first): the judges, the brand audits and
  the GPT validate stages send their content to OpenAI as soon as acc3 refuses GPT-6 Luna, instead of answering on
  Nova Lite / Llama inside AWS (333's "AWS first" order under T-3). Picked because Q2 names that twin; the same model
  keeps the verdicts calibrated (333 shipped Nova Lite on judges with a format smoke only). They now share the
  OpenAI API's 10 USD a month cap (327) with the writers' last rung: at about 0.014 USD per tour (judge ~0.005,
  atomize ~0.005, source facts) a full catalogue rewrite reaches it, and the routes then fall on to acc2 / Claude 4.x
  (a safe degrade, the 333 behaviour). Raising that cap is the tech lead's call, not changed here.
- Writers degrading to Haiku 4.5 before the OpenAI API: a Claude reply of a smaller tier over GPT-6 Luna of another
  maker. Picked for the family rule (writers stay Anthropic, ADR 0009 d12); the judges score whatever answered, as
  before.
- Model first vs AWS first (Q2): the same model on the Anthropic API keeps the primary's writing quality (TL-11 eval
  freeze holds: no prompt change, and the writing-memory release was evaluated on Sonnet 5.5), at the price of
  sending the writer's content to Anthropic before the AWS 4.x siblings are tried. The tech lead chose quality. Until
  the Anthropic API key is stored the connection is off and both orders make the same calls.
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
| S1 judge + Jev | 0.009 | ~0.007 (Nova Lite + Jev) | ~0.009 (GPT-6 Luna on the OpenAI API, twin) |
| A3 `a3_atomize` | 0.005 (Luna) | ~0.004 (Nova Lite) | ~0.005 (GPT-6 Luna on the OpenAI API, twin; 0.052 on Haiku 4.5 once the OpenAI cap is reached) |
| **S1 + A3** | **~0.075** | **~0.015** | **~0.11** |

`s1_source_facts` is not itemised in the wave report; it is Luna-priced and now answers on its OpenAI API twin at the
same price (x10 only when it falls on to Haiku 4.5). Monthly: since 29/10 acc3 answers nothing and LLM spend is ~0. At
October's volume (a catalogue rewrite of 810 tours plus re-runs) S1 + A3 would be ~0.11 x 810 = ~90 USD for one full
rewrite (vs ~12 USD on today's fallbacks and ~60 USD on the intended 5.x path; the first S267 pass, with atomize on
Haiku 4.5, was ~120 USD); a routine month of 100-200 regenerated tours is ~11-22 USD. The GPT stages' OpenAI API
share (~0.014 USD per tour) counts toward the OpenAI API's 10 USD cap. Tenant writers (`t9_write`,
`t2_generate`, October 29 USD on Sonnet) move the same way (x1.5 vs Sonnet 5.5, x30 vs the OpenAI API). acc3 bills the
remaining AWS Lift credit (~250 USD, expires 31/12), then the card; the AWS budget `aa3-llm-monthly-total` (250 USD) only
e-mails. The gateway hard stop is `shared.llm_provider_connection.monthly_budget_usd` of `acc3` (cost policy, 317):
its live value is not checked from this lane; migration 366 sets it to 100 USD (tech lead Q3, S267) when it is NULL or
higher. A hard stop on acc3 is itself a safe degrade: the chains then fall to the Anthropic API (when on), the OpenAI
API (10 USD cap) or acc2. With the Anthropic API on (no key yet), model first sends the writers to Sonnet 5.5 / Haiku 5.5
on the Anthropic API while acc3 refuses 5.x, at the 5.5 catalog prices (~0.060 USD/tour for `s1_generate`, the intended
column above) instead of Sonnet 4.6 on acc3 (~0.090). Since S267 the tenant / TripPlanner / validate stages do the
same for their Claude 5.x primaries (the A3 Claude validate stages at Haiku 5.5 / Sonnet 5.5 prices). Migration 366
sets `monthly_budget_usd = 150` on `anthropic-direct` (one key for every product, tech lead S267; 304 / 306 left it
NULL), alert 80 %, hard stop: at 150 USD a month the Anthropic API leaves every route and the chains go on to the 4.x
rows on acc3, acc2 or the OpenAI API (10 USD cap).

## Questions for the tech lead — answered S267 (04/11/2026)

1. **Cost delta.** While acc3 refuses 5.x, the writers spend ~0.09 USD/tour on Sonnet 4.6 instead of ~0.003 on the
   OpenAI API (S1 + A3 ~0.15 vs ~0.015 USD/tour). Accept? **Answer: ACCEPTED** (Claude 4.x on acc3 while 5.x is
   refused, ~0.15 USD/tour).
2. **Writer order.** Should large-tier writers (s1_generate) prefer GPT-6 Luna on the OpenAI API over Sonnet 4.6 /
   Haiku 4.5 on acc3? **Answer: no; prefer the SAME MODEL first, then go outside** ("acc3 Sonnet 5.5 >> API Sonnet
   5.5 >> acc3 Sonnet 4.6 / Haiku ..."), on EVERY route. Done: model-first is the default route order, the AWS-first
   order is a switch (default off), the admin rule is relaxed, the chains stay as decided (Claude 4.x on acc3 before
   the OpenAI API). Fix pass 2: "openai-direct for a GPT twin if one exists" applied as decided: the twin exists
   (`gpt-6-luna-openai`, the same model per the catalog) and sits right behind GPT-6 Luna on every GPT route. And
   T-3 is replaced: **AWS preferred, not AWS only**, every active text stage vendor_direct (see
   Decisions).
3. **acc3 cap.** Set `monthly_budget_usd` on `acc3` to 100 USD (hard stop -> chains fall to the OpenAI API / acc2)?
   **Answer: YES.** Migration 366 sets `shared.llm_provider_connection.monthly_budget_usd = 100` on `acc3` when it is
   NULL or higher (a lower admin value is kept), and inserts the cost policy row (alert 80 %, hard stop) like the
   OpenAI API cap of 327 (`ON CONFLICT DO NOTHING`; an existing policy row is kept and reported in a NOTICE when it
   is not alert 80 % / hard stop). Recorded in the root AI Use Register.
4. **Keys.** One Anthropic API key per product line or one for all? **Answer: ONE key** for everything
   (`anthropic-direct`, 150 USD a month, alert 80 %, hard stop; migration 366); per-product tracking in our ledger and
   the Spend > By app page (the stage -> product map above).

**Primaries (TL-11 eval freeze: not changed in this pass).** Suggestions for after the freeze, all models already in
the catalog: `a3_place_locate` / `t9_faq_answer_fit` are on Sonnet 5.5 while their siblings `a3_place_days` /
`a3_place_card_check` are on Haiku 5.5 (S247 found Haiku too weak for locating names; days / card check may gain from
Sonnet 5.5 the same way, measure first); `tp_compose` could follow the S1 writer's release evaluation (it writes
customer-facing trip text on the same Sonnet 5.5); `segment_research` phrases keywords on Haiku 5.5, which is fine
unless its eval says otherwise. No change proposed for the judges (GPT-5.6 Luna primary, TL-10).

## Should know

- Gaps the S267 decisions leave outside this repo (follow-ups, not done here):
  - **TripPlanner** calls Bedrock only in its own gateway (`_uncallable`: "TripPlanner calls Bedrock only", T-11 in
    code): with 366, `tp_compose` lists the Anthropic API deployment of Sonnet 5.5 and the OpenAI API rung, and
    TripPlanner's gateway skips both (no error, a logged note). Reaching them needs vendor adapters in
    AA-TripPlanner-Web.
  - **CRM** routes in its own gateway (AA-Partner, Bedrock profiles, PII scrubbing before any call) and logs to
    `crm.llm_call`, not `shared.llm_role_config` / `shared.llm_call_log`: the policy needs a CRM change (its own lane)
    and it keeps its scrub. Its spend reaches the By app page through `crm.ops.v1`.
  - **The connection caps are enforced by the CIS gateway only** (review finding 2, fix pass 2). `connection_budget`
    (CIS `vendor_route.deployment_plan`) sums `shared.llm_call_log` by `account` for the month, with no app filter,
    so it COUNTS TripPlanner's acc3 rows, but only CIS calls are STOPPED at 100 %: TripPlanner's own gateway
    (`AA-TripPlanner-Web/backend/shared/llm_gateway.py` `deployment_plan`) checks circuits, roles and scopes, never
    `monthly_budget_usd`; its only stop is its daily app cap (`budget_status`, `TP_DAILY_SPEND_CAP_USD`). The CRM logs
    to `crm.llm_call`, so its acc3 spend (Claude entries off since crm-042, ready for acc3's return) is neither
    counted toward the acc3 cap nor stopped by it. No cap spans every product today: the AWS budget
    `aa3-llm-monthly-total` on acc3 (250 USD) only e-mails. Follow-ups: (a) TripPlanner: a per-connection
    `budget_stops` flag through `catalog.v_llm_gateway_config_v1` (fail closed like its spend cap), read in its
    `deployment_plan`; (b) CRM: count `crm.llm_call` toward the connection caps (or a CRM-side stop on the same
    figure) before its acc3 entries come back. The 150 USD Anthropic API cap has the same scope: only the CIS gateway
    calls `anthropic-direct` today; if another product's gateway ever uses the same key, the vendor-side limit (the
    Anthropic Console spend limit on the key's workspace, set to the same 150 USD) is the only cap over all products.
    Set it when the key is created.
  - **TripPlanner's acc3 connection circuit comes from a SQL mirror that this change does not update** (review
    finding 3, fix pass 2). The connection-circuit fix (a refusal whose streak began before the connection's last
    answer no longer counts) is Python (`breaker._tripping`). TripPlanner reads the circuit from
    `catalog.v_llm_gateway_config_v1` (CIS 341, redefined by 350), whose `conn_signal` CTE still counts every
    `access_denied` row with `circuit_open_until` set and `last_error_at > last_ok_at` on 2+ models, and its
    `llm_breaker.shared_skip_class` skips every acc3 deployment while the class is set. A CIS request whose half-open
    probes hit two refused acc3 models with no acc3 answer after them (a judge: gpt-5.6-luna, gpt-6-luna; a3_atomize:
    gpt-6-luna, haiku-5-5) leaves acc3 tripped in the view until the next CIS answer on acc3, so tp_compose's new
    Sonnet 4.6 / Haiku 4.5 rungs are skipped in that window. Not a regression (tp_compose fails today anyway).
    Follow-up: a CIS contract migration (own MIG-4, the X1 S22 catalog recipe of 350; number = owner main max + 1 at
    the time, 367-369 are held by S267 lanes) that recreates the view with `AND NOT (h.last_error_class =
    'access_denied' AND o.last_ok_at IS NOT NULL AND h.opened_at IS NOT NULL AND h.opened_at < o.last_ok_at)` in
    `conn_signal` and `GRANT SELECT (opened_at) ON shared.llm_provider_health TO catalog_owner`, with its own
    integration test (apply 323, 341, 350 then it, the DB Migrate way). Not done in this pass: it is a separate
    contract change of TripPlanner's contract view with its own MIG-4, and it cannot ride in PR B (an out-of-order
    number would hold 366 back).
  - **By app shows CIS as one row** (catalogue, tenant content, Booking supply text and writing memory all write
    `app = 'cis'`): the per-product split inside CIS is by stage (and tenant) in the ledger, not a By app row. A
    "by product" grouping on the page (stage -> `product_of`) is a small follow-up if the business owner wants it
    there; `supply_content_writer` is Booking's product but CIS's app.
  - **`pii_class` per stage** (ADR 0019) is not in the CIS gateway yet; nothing in this change relaxes a PII control.
- The AA-CIS-Infra byte copy of 366 (`db/migrations/cis/`) must be taken from the FINAL file of PR B (S267 rewrote it:
  scopes, chains, caps); an older copy fails the Copy Guard.
- Tests run in this lane (S267 fix pass 2, private Postgres 16 under the scratchpad, CI's integration env): the related
  unit files (92 files, 7 731 tests after the rebase) green; the full integration suite 2 370 passed, 3 skipped, 16 errors, all in
  `test_007_rls_isolation` (CI excludes it: local role passwords). CI's flake8 (`api/ services/ shared/`) clean;
  gitleaks 8.30.1 clean on the three branches. Code and root branches rebased on the latest `origin/main` /
  `origin/master`; the migration branch on the code branch.
- Merge order: PR A (code) deployed BEFORE migration 366 is applied: without the connection-circuit fix acc3's
  refusals of the 5.x / GPT rows keep the acc3 connection circuit open and the new 4.x rows are skipped; without the
  relaxed admin rule the admin route PATCH refuses 366's writer chains (sonnet-5-5 has an Anthropic API deployment).
  The route order itself needs no code (model first is the gateway's existing per-model order).
- acc3 hard stop at 100 USD a month (366): the CIS gateway stops every CIS call on acc3 at 100 % (writers, validate,
  cohere-embed-v4-acc3, the judges' acc3 rows): vendor_direct routes fall to the Anthropic API (when on) / the OpenAI
  API (10 USD cap), aws_only routes to acc2 or stop as a budget stop. TripPlanner's acc3 calls count toward the 100
  USD but are NOT stopped by it, and the CRM's are not counted (see the gaps above).
- The TripPlanner gateway reads tp_compose's route through `catalog.v_llm_gateway_config_v1` and calls Converse rows
  itself, so it can call the two new rows without a code change; whether it does depends on the view's acc3
  connection circuit, which still uses the old rule (see the gaps above: a view migration is the follow-up).
- After the apply: POST /admin/writing-memory/releases/reverify-scan (writer routes changed outside the admin PATCH)
  and the root route page (`tools/docs/gen_llm_routes.py`).
- After the apply (live steps): admin Test connection on acc3 with `sonnet-4-6` and with `haiku-4-5` (ok=true; this
  records the answered call that closes the connection circuit), then the $0 report
  (`scripts/llm_ops/route_chain_check.py` through the ECS exec runner) must show FIRST = `sonnet-4-6@acc3` on
  s1_generate and `gpt-6-luna-openai@openai-direct` on a3_atomize and s1_judge (GPT-6 Luna's twin, while acc3
  refuses GPT), with no 4.x attempt reading "circuit open: acc3".
- ADR 0009 decision 5 amended (root PR, S267, tech lead): AWS preferred, not AWS only (replaces T-3; every text stage
  vendor_direct, embeddings aws_only); the order is model first (the same model on its vendor API before the next
  model: the Anthropic API for Claude, the OpenAI API twin for GPT-6 Luna); any other vendor-only model stays the end
  of the route; one Anthropic API key. `LLM_ROUTE_AWS_FIRST=on` gives the AWS-first order.
- Tech lead questions: answered S267 (see above): cost delta accepted, model first, acc3 cap 100 USD, one Anthropic key
  (150 USD), AWS preferred not AWS only.

## Review fix pass 2 (S267, independent verifiers)

1. **GPT routes not model first (major, accepted).** `gpt-6-luna-openai` is GPT-6 Luna's twin; 366 now puts it right
   behind `gpt-6-luna` on every GPT route, and the three rule sites accept it there (`data_scope.vendor_twins`, see
   Decisions). The root ADR 0009 amendment says what was decided ("the OpenAI API for GPT-6 Luna through its twin
   row"), not "if one is ever added".
2. **acc3 hard stop not enforced on every path (major, accepted as documentation).** TripPlanner / CRM are outside
   these worktrees: the register, the ADR and these notes now say the gateway stop covers CIS calls only, TripPlanner
   is counted but not stopped, the CRM is neither, and no cap spans every product (the AWS budget only e-mails);
   follow-ups listed in the gaps.
3. **TripPlanner's view keeps the old connection-circuit rule (major, accepted as documentation + follow-up).** The
   366 header and these notes no longer claim TripPlanner reliably reaches the new rows; the exact view change is
   written down as a follow-up migration (gaps), and the register / ADR list it.
