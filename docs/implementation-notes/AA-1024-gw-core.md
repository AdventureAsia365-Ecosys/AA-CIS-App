# AA-1024 — gateway core, lane gw-core (S263): A9, A3, A5, A2 rows, D8, I6

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` (slices A2, A3, A5, A9, D8 and open item I6).
Two PRs, MIG-4 order: the migration-only PR (`api/migrations/334`–`337`, branch `feat/aa-1024-gw-core-migrations`),
then this code PR (`feat/aa-1024-gw-core`). Code comments say "AA-642 <slice>" like the earlier gateway lanes (the
plan's slice ids); commits say `Refs AA-1024`.

## A9 — embedding ledger rows (gap OPS-02)

### Decisions
- Live fact (S263): the daily probe's answered `cohere-embed-v4` row had `latency_ms` / `attempt_no` NULL and
  `cost_usd` 0; the refused acc3 row had no `attempt_no`. The catalog price is right (0.12 USD per Mtok since 170);
  the 0 is rounding: a probe of a few tokens costs ~0.0000004 USD and `round(…, 6)` gave 0.
- `embed.price_embedding`: the deployment's `price_in_override`, else the catalog row, else a flagged estimate
  (`EMBED_ESTIMATE_PER_MTOK` 0.12, `pricing_mode` `estimate`), rounded UP to the micro-dollar (`cost_usd` is
  numeric(10,6)), so a billed call is never 0 USD. `pricing_mode` says which source priced it.
- `embed.probe` sets the attempt facts (connection, latency, attempt 1, wire id) on its response like `embed()`;
  the probe job's refused row writes `attempt_no=1`.
- No `units` / `unit_kind`: embeddings are billed per input token, which the ledger already carries.

## A3 — route deadline, retries owned by the gateway (gap RW-11 parts 1–3)

### Decisions
- Migration 334: `route_deadline_s` by role on every row that has none: writer 1200, judge 900, validate 600,
  embed 120 s. Each value is above the longest single attempt of the role (Bedrock read timeout 300 s, an acc2
  embedding 30 s). `SAFE_DEFAULTS` carries the same values (`role_config.ROUTE_DEADLINE_S`). No column default: a
  later stage row states its own (335 does).
- SDK retries off: Anthropic / OpenAI adapters `max_retries=0` (`SDK_MAX_RETRIES`), botocore
  `total_max_attempts=1` on every runtime client (`bedrock_satellite.connection_config` / `native_runtime`, the
  client's acc3 runtime). The connection's `max_retries` column is no longer read.
- At most `errors.MAX_CHAIN_ATTEMPTS` (6) provider attempts per gateway request, counted in the request's attempt
  scope (`LLMClient._attempt`) and in `embed()`. The 7th is not started: a 0 USD `deadline` ledger row and
  `RouteAttemptCapReached` (a subclass of `RouteDeadlineExceeded`, so every path that stops on a passed deadline stops
  on the cap too).
- `request_timeout(cap)`: an API adapter's per-request timeout is the remaining route time but never more than the
  connection's own timeout (a long deadline never lengthens one attempt).
- Legacy paths (`legacy_chain`, `s2_vendor_deployment`, `catalog_fallback_*`) stay, as the brief says: removal after
  30 days with no ledger traffic.

## A5 — served_roles (gap RW-09, CIS part)

### Decisions
- `vendor_route.deployment_plan(model, scopes, role=…)` and `vendor_deployment_for(…, role=…)` leave out a deployment
  whose connection's `served_roles` (migration 322) do not include the stage's role. NULL `served_roles` or no role =
  no filter. Checked before the data scope, so a connection that may not serve the role never counts as the first row
  of the S3 vendor rule nor as a circuit / budget skip.
- `client.stage_role(request)`: the stage row's role, bound once per call with the scopes (`_STAGE_SCOPES` now
  carries `(stage, scopes, role)`) so the gateway still reads the stage config once per call
  (`test_aa900_eval_label_keeps_route`); else the role the request states; else None.
- `embed()` asks with role `embed`. The legacy alias path (`_alias_steps`) passes the role too.
- `/admin/llm-availability`: `invariant.check_stage` counts only connections that serve the role and lists the left-out
  route models in `role_skipped`; the availability card shows one line per stage with skips.

## A2 rows — segment research stages (gaps CIS-7, RW-11 part 4)

### Decisions
- Migration 335 creates `segment_research` (the loop, `services/acp_contract/segment_research.py`) and
  `segment_research_batch` (the batch proposer): role writer, the legacy `haiku` key (the same calls as today), acc3,
  `aws_only`, deadline 1200, degrade `queue_retry`. `SAFE_DEFAULTS` mirrors them; the prompt register gets their
  rows (the `a3_search_demand` row stays until the A2 code lane moves the callers).

## D8 — dead routes (gap CIS-10)

### Decisions
- Migration 336 switches off `s1_atom_writer`, `tp_extract`, `tp_component_embed` with the stage kill switch of 326
  (`is_active` false + `disabled_reason`), nothing deleted. It refuses to run when any of the three has a ledger row in
  the last 30 days. `t10_claims` is untouched.

## I6 — structured output without a forced tool choice

### Decisions
- New catalog capability `structured_output` (`tool` / `any` / `auto` / `prompt`, `client.structured_mode`). Unstated
  = today's rule (tool, or auto for a model that refuses a forced tool choice). `prompt` sends no `toolConfig`: the
  schema goes in the system prompt and the first JSON object of the reply is taken; the reply is still checked against
  the schema (one schema retry, then a billed unusable reply).
- Migration 337 states `forced_tool_choice: false` + `structured_output: prompt` on Llama 4 Maverick and Scout (each
  key only when unstated). `prompt`, not `any`: Bedrock documents the any / tool choices only for some makers (not
  Meta), and under `auto` a Llama model may answer the tool call as text in its own syntax.
- The contract smoke's plan no longer refuses a model whose row refuses a forced tool choice but states a non-forced
  mode; its plan entry shows `structured_mode`.
- `_tool_choice_rejected` also recognises Bedrock's Llama wording ("doesn't support the toolConfig.toolChoice.tool
  field").
- No route change: `a3_place_dedupe` gets its Maverick rung in a later migration, after the live smoke passes.

## Changed (vs. the brief)
- The brief says "send toolChoice 'any' … (or no toolChoice + schema in prompt)": the code supports both; 337 picks
  `prompt` for the two Llama rows (reason above). A different mode is one catalog edit.
- Older integration tests that pin the post-324 / post-326 state learned the later files (304, 320/321, 324, 326,
  259); the 304 and 320/321 fixtures re-apply 334 / 337 after re-running 320 / 324 so later modules see the full chain.

## Tradeoffs
- A route deadline by role, not per stage: one number per role is easy to reason about; a stage that needs more is one
  admin edit (an existing value is kept by 334).
- SDK retries off means one throttle moves on to the next deployment / model at once; the route has fallbacks and the
  job runner retries the job, so a second SDK try inside one attempt bought little and hid latency.
- Rounding an embedding UP to 1 micro-dollar over-states sub-micro-dollar calls (a probe), never a real batch.

## Should know
- The code PR does not need the migrations (NULL deadline = none; unstated capability = today's rule; missing segment
  research rows = SAFE_DEFAULTS), so code and migrations may deploy in any order.
- `SAFE_DEFAULTS` still has `s1_atom_writer` (only used when the table is unreadable).
- TripPlanner's Route table (`backend/shared/llm_gateway.py`) still lists `tp_extract` / `tp_component_embed`: a
  follow-up in AA-TripPlanner-Web.
