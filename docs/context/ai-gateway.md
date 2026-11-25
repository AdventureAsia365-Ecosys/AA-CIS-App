# AI gateway and model routing

> Detail page of [CONTEXT.md](../../CONTEXT.md): the gateway, connections, routes, budgets, reliability, data class per stage and the Jev decision layer. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

- **AI/Bedrock** (root ADR 0009, as built: root `docs/architecture/ai-gateway.md`): every paid AI and data call goes
  through the gateway in `shared/llm_client/`, whose control plane (registry v2, routes, scopes, budgets, circuit
  state, ledger v2) this repo owns. Chat models run on Bedrock in **acc3**, the main chat account (refusing every model
  since 2026-10-29, Error 002); acc2 serves embeddings and five candidate chat models (324, ADR 0009 decision 14: eval,
  judge and validate only), two of which are the LAST fallback of the platform judge / validate routes that reached
  acc3 only (migration 331: Nova Lite, Llama 4 Scout for the place card check). Outside AWS: the OpenAI API
  (`openai-direct`, last resort of the judge routes) and the Anthropic API (`anthropic-direct`, deployments seeded
  behind acc3 by 306, no key yet), plus Jev, DataForSEO and Geoapify as registry rows (322). acc1 left the ecosystem
  (AA-998) and came back for ONE thing (AA-1095, root ADR 0001a Note S278): the registry connection `acc1` (migration
  406, assume role, account ID from the task env `BEDROCK_ACCT_PAYER`) serves the Bedrock chat models while AWS blocks
  acc3; no code names it (the guard below still holds), the admin names it by its account ID from the registry. Route
  order per family (one route-control version, `api/services/llm_family_order.py`): Claude = Anthropic API, then
  acc1, then acc3; GPT on Bedrock = acc1, then acc3 (skipped at 0 USD while it refuses), then the model's OpenAI API
  twin (its own catalog model behind it on the chain, CIS 366). The cross-account trust is provisioned in AA-CIS-Infra.
  Embeddings (Cohere Embed v4) run natively on acc2, through the GLOBAL cross-region profile
  `global.cohere.embed-v4:0` since migration 238 (AA-856: the `us.` quota is fixed at 20 rpm, the global
  one is adjustable; one quota shared by f10_embed, a3_place_embed and TripPlanner search). Claude cannot be invoked from a local shell — the satellite roles only trust the
  ECS task role, so real LLM measurements run inside the `api` container via ECS exec.
- **Embedding spaces** (AA-1026 D5, migrations 328 / 329, `shared/llm_client/embedding_space.py`): a space is one
  model at one dimension (`cohere-embed-v4/1536/v1`, served by `cohere-embed-v4` on acc2 and `cohere-embed-v4-acc3`).
  `shared.embedding_space` lists the spaces, `shared.embedding_column` gives every vector column its space and swap
  group (`f10_embed` = atom + question + content piece vectors; `tour_place`; `published_tours`). Writers
  (`content_embedding.compute_embeddings`, `places.llm.embed_texts`) pass the column's space to `embed(..., space=)`:
  only that space's models are tried (the route filtered to them, else the space's own), at its dimension, and the
  ledger row carries `quality_signal.embedding_space`. TripPlanner reads its column's space through
  `catalog.v_embedding_space_v1` and searches in it only. The ready fallback of another vendor is Amazon Titan Text
  Embeddings V2 (`titan-embed-text-v2`, space `titan-embed-text-v2/1024/v1`, standby) on connection `acc2-usw2`
  (acc2, us-west-2, the model is not offered in us-west-1), connection off (its deployment is enabled: switching
  is one audited `PATCH /admin/llm-connections/acc2-usw2`). Moving a column: the owner steps are DDL in the
  AA-CIS-Infra run-script `db/scripts/aa1026_embedding_shadow.py` (DB Migrate mode=run-script, as `cis_owner`, DRY RUN
  by default), not SQL functions (a migration may not switch roles in dynamic SQL; 329 only adds the trigger function
  `shared._embedding_shadow_reset()`): `prepare` (shadow column + trigger `<column>__shadow_reset`, which empties the
  shadow of a row whose serving vector a writer rewrites) → job `embedding_reembed` (dry_run / fill / probe, POST
  /admin/job-runner/embedding-reembed/run, resumable, cost cap per job across retries) → `swap` (the whole group in one
  transaction: renames, rebuilds the dependent views, registry updated) → `rollback` while `__prev` is kept. A group
  read by `catalog_owner`'s views (`tour_place`) swaps with a contract cis/ migration under the catalog recipe; the old
  vectors are dropped by a destructive cis/ migration. Runbook: root `docs/ops/runbooks/embedding-re-embed.md`.
- **Model per stage is DB-driven**: `shared.llm_role_config` (20s cache; `SAFE_DEFAULTS` in
  `shared/llm_client/role_config.py` are only a fallback), editable in admin AI providers › Routing.
- **Model Catalog** (`shared.llm_model_catalog`, AA-658, ADR 0005): the admin dropdown options and
  LLM prices come from it (`pricing.py` is the fallback). Non-legacy Model Keys (e.g. `sonnet-5`)
  are called with the Bedrock Converse API on the acc3 satellite.
- **Connection registry + Anthropic API path** (AA-1002, gateway slices S1 / S2; migrations 304 / 306):
  `shared.llm_provider_connection` (one row per way in: Bedrock account, vendor API; a secret NAME only,
  `^aa-llm/<env>/<name>$`, and a code allowlist of base URL hosts per kind) and `shared.llm_model_deployment` (a
  catalog model on a connection: wire id, priority, `request_defaults`). Only `shared/llm_client/vendor_route.py`
  reads them on the call path: a catalog key whose first enabled deployment (lowest priority) on an enabled
  connection is on an `anthropic` connection is called through the Anthropic Messages API
  (`adapters/anthropic_messages.py`, SDK `anthropic` 2.x: key read from the secret at call time, per-connection
  timeout / retries, `output_config.format` for structured output, explicit `output_config.effort`, no sampling
  parameters, no forced tool, no server-side fallbacks); every other registry state keeps today's path byte for
  byte. A `model_tier` of `<model key>@<connection>` pins one deployment (probes only). The response is labelled
  `anthropic-api-<key>` so `llm_call_log` records `provider = 'anthropic'`; `tokens_in` = uncached + cache read +
  cache write (the Converse convention); a refusal / empty / non-conforming reply is billed-unusable; 401 / 403 are
  `access_denied`, 402 / credit or spend-limit wording (400 / 402 / 403 only) `quota_exhausted`, 429 / 529
  `throttled` (checked first). The key comes from `secret_name` only (a credential env is tied to its own kind).
  Migration 306 seeds Anthropic API deployments behind acc3 (priority 100, `vendor_direct`, every Anthropic Converse
  model, tenant ones included) with the `anthropic-direct` connection off: slice S5 data scopes must land before or
  with S3's spread over deployments, or tenant content leaves AWS when acc3 fails; the only admin write in S2 is
  `PATCH /admin/llm-connections/{key}` `{"enabled": bool}` (Anthropic connections, audited). Breaker, data scopes
  and cost parity are later slices (S4–S6).
- **Deployment routing + legacy aliases** (AA-1002 slice S3, migration 307): a catalog key is tried on its
  deployments (`vendor_route.deployment_plan`): the model's callable rows (Bedrock assume_role / task_role for
  converse, anthropic_native and embed rows; Anthropic for Anthropic non-legacy rows; OpenAI only for
  provider='openai' rows), enabled and on an enabled connection, lowest priority first, the same model on each
  deployment before the route's next model. Until slice S5: a `vendor_direct` connection is used only as the FIRST
  enabled row (an admin's deliberate choice), never as a fallback behind an `aws_only` one, decided before the circuit
  check (`vendor_route.circuit_open` = `breaker.is_open`, slice S4). No callable row (or no registry) =
  today's path; callable rows that are all off = the model is refused and the route moves on. A billed unusable reply
  is the model's answer (not retried on another deployment). With the 304 / 306 rows every Dev stage makes exactly
  the calls it made before S3 (`tests/unit/test_aa1002_s3_routes_snapshot.py`, golden from pre-S3 code).
  `LLM_DEPLOYMENT_ROUTING=off` (ECS env) restores the S2 path. Legacy keys: `shared.llm_model_alias` maps `haiku` →
  [haiku], `sonnet` → [sonnet, haiku] (the chain's order); with `LLM_LEGACY_ALIASES=on` (default OFF until a live check)
  each target runs on its deployments with the legacy InvokeModel body (the very requests of `_legacy_chain`); no alias
  row / no registry / `LLM_LEGACY_ACC2_NATIVE=on` = `_legacy_chain`. Accounts are data: `bedrock_satellite` builds
  STS sessions from the assume-role connection (cache key: connection, role ARN, ExternalId, region),
  `catalog.serving_accounts()` and `embed._runtime_for` read the registry; `connections.BUILTIN_CONNECTIONS` (the 304
  seed: acc3, acc2, openai-direct) answers only "how to reach X" when the registry is unreadable or a row is broken.
  Still contract work (not in 307): the logical rows `sonnet-4-6` / `haiku-4-5` and the `account_route` CHECK → FK.
- **Registry v2** (AA-1017 / AA-1016 / AA-900, migrations 320 / 321 / 322, root ADR 0009 §2-§4): the connection table
  also holds the paid providers that are not LLMs (`typesafe` = Jev, role decide; `dataforseo` and `geoapify`, role
  data; `bedrock_guardrails`, role guard, seeded off), with a secret guard per kind (LLM kinds `aa-llm/<env>/<name>`,
  each provider kind only `aa-cis/<env>/<kind>`) and a host per kind, both in code (`connections.check_secret_name`,
  `BASE_URL_ALLOWLIST`) and as CHECKs; plus `failure_domain`, `served_roles` (routing reads it since AA-1024 A5: a
  connection that does not serve the stage's role is not in the plan; NULL = every role; a model left with no
  deployment only for that reason is refused with ledger class `role_not_served`, migration 338), `access_state`,
  `last_probe_at`, `balance_source`, `daily_credit_limit`, `low_balance_usd`. The LLM dispatch never sees a provider
  kind (`connections.fetch_connections` / `registry_rows_sync` return `LLM_KINDS` only; `resolve_api_key` refuses
  them). Jev's base URL / timeout / retries (`decide.jev_endpoint`), the Jev and DataForSEO low-balance thresholds and
  the Geoapify daily credit limit (`geocoder.daily_credit_limit`) come from their rows, else env, else the old
  constants (the seeds equal those). The secret NAME and base URL are migration-only (D9: 322 revoked cis_app's
  UPDATE; `PATCH /admin/llm-connections/{key}` answers 422), and catalog prices too (`set_catalog_price` refuses; the
  admin keeps the switch). Catalog rows carry `family` (`shared.llm_model_family`, the maker; NULL = vendor; the closed
  vendor CHECK is gone) and `capabilities` read as data (`forced_tool_choice`, `prompt_cache`, `cache_min_tokens`,
  `effort_default`), each falling back to the old code rules when unset (every row today). `shared.stage_model_params`
  (stage x model: max_tokens, min_output_tokens, temperature, effort, reasoning_mode; `shared/llm_client/stage_params.py`)
  applies with the precedence call site -> row -> protocol default on every catalog path; empty = the same request
  object. **Thinking budget per stage (AA-741 S271):** effort + reasoning_mode are the bound, because the Claude 5.x
  models take no `budget_tokens` (a 400) and bill adaptive thinking as output inside max_tokens. On the Anthropic
  Messages API effort is `output_config.effort` and reasoning_mode the one `thinking` form the family takes
  (`adapters/anthropic_messages.resolve_thinking`: 'off' = `between_tools` on Sonnet 5.5, `disabled` on Haiku 5.5 /
  Opus 5 / Sonnet 5, never at xhigh / max, nothing on Opus 5.5 / Fable; 'adaptive' explicit; nothing on 4.5); on
  OpenAI / Azure effort is `reasoning_effort` on a reasoning model only (low / medium / high as is, xhigh / max as
  high); on Converse effort goes in `additionalModelRequestFields` only in the form the catalog row states
  (`capabilities.converse_effort`: `output_config` for Claude, `reasoning_effort` for GPT; no row states one, AA-803:
  set it after a probe; a ValidationException on it drops the field once), and reasoning_mode is never sent there.
  **One effort rule (AA-1053):** the stage x model row is the only source of a stage's effort and
  `shared/llm_client/effort_rule.py` the only module that maps it to a provider field (guard
  `tests/unit/test_aa1053_effort_rule.py`); `LLMResponse.effort` (ledger `effort`) is the value sent, NULL = none
  (the model's default). A field a path does not send is logged once (`stage_params_not_applied`). CIS 381 seeds effort medium for t9_write on sonnet-5-5 and low for the Claude
  5.5 validate stages; t2_generate and t8_angle_gen keep their default (high) until a paid quality read backs a
  change (since AA-900 W0 the writer release fingerprint `config_fp` covers these rows: a change flags reverify). A
  deployment may state its own `api_style` (NULL = the
  catalog's). `errors.TRUNCATED` is the recorded, non-failure class of a reply cut at its token limit (ledger status
  `truncated`; `llm_provider_health` accepts it). `shared.tenant_ai_budget` (AA-1016, no rows = no budget) and
  `shared.prompt_version` (AA-900, sha256 + path, never prompt text) exist for the enforcement / prompt-gate slices.
- **Circuit breaker + limiter + Connections tab** (AA-1002 slice S4, migrations 309 expand / 310 contract):
  `shared/llm_client/breaker.py` keeps one circuit per deployment (connection × catalog model) in
  `shared.llm_provider_health`, shared by every api / worker task (10 s snapshot; writes lock the row and apply pure
  transitions). Class rules (`breaker.RULES`): access_denied / quota_exhausted / `credential` (a key that cannot be
  read) open at once, throttled after 3 in a row, anything else after 5; a refusal of one request's own content
  (input too long, malformed, content filter: breaker-only class `request`) is never counted; open periods are short (30 s – 5 min first,
  doubled on each failed half-open probe, capped). Half-open = exactly one call across tasks takes a probe lease.
  `deployment_plan` leaves out open deployments (`DeploymentPlan.circuit_skips`); a model none of whose deployments
  may be called raises `breaker.CircuitOpen` carrying the class that opened it (the legacy alias path too), so an
  account-level refusal still ends in `LLMAccessDenied` without a provider call. `LLMClient._call_target` and `embed` wrap every registry call in
  `breaker.guarded` (probe admission, the connection's limiter, outcome; an unusable reply counts as an answer). The
  limiter is per process: `max_concurrency` (semaphore) and `rpm_limit` (rolling minute) from the connection row; no
  slot within 30 s = `LimiterSaturated` (throttled, not counted). Table missing / DB down = the same breaker in
  process. `LLM_CIRCUIT_BREAKER=off` (ECS env) = the S3 behaviour. Admin (ai.admin, audited):
  `PATCH /admin/llm-connections/{key}` switches bedrock / anthropic / openai connections and edits the secret NAME
  (`aa-llm/` only), base URL (kind allowlist), timeout, retries, limits, budget, notes (310 grant; identity and data
  scope stay migration-only); `POST /admin/llm-connections/{key}/test` sends one small real call through
  `LLMClient.probe` (works on a switched-off connection; stage `adhoc_conn_test`, role `validate`, logged + audited;
  its outcome counts on the circuit). `GET /admin/llm-availability` adds `connections` (status per connection and the
  circuit of each deployment). UI: AI providers › Providers › Connections (`frontend/app/admin/ai-gateway/ConnectionsPanel.tsx`; secret name
  and base URL read-only there since AA-1017, changed by migration).
- **Cassette recorder** (AA-900, S271 lane l17, migration 385; root ADR 0020 d8), record-only:
  `shared/llm_client/cassette.py`. `LLMClient._attempt` hands every provider attempt that returned a reply (every role,
  shadows, the probe, the truncation retry) to `cassette.record_attempt`, which writes one `shared.llm_cassette` row
  on a background write (best-effort: never raises, `cassette.stats()` counts failures; UndefinedTable before 385 =
  a 5-minute skip). A row holds the raw reply (256 KiB cap, `reply_truncated`; a `pii` stage keeps the sha only),
  `request_sha256` (canonical JSON of messages + params as SENT, `cassette.request_sha`), the system prompt's sha and
  version (never prompt text), model key, provider-reported model, wire id, connection, provider request id, rung,
  attempt number, effort, max_tokens, stop reason, and the links: bound job / tenant / eval run and
  `cassette.bind_owner(content_piece_id, tour_id, eval_item)`. Its ledger row is found through `route_trace` +
  `attempt_no` (`get_by_call_log_id`; no FK, the ledger insert is unchanged); `get_by_request_sha` is the replay
  read. Switch `LLM_CASSETTE=record|off` (default record; unit and integration tests default off). Retention 30 days
  proposed (root data standards DM-7.1), no TTL job yet; the tenant erasure deletes a tenant's rows.
  S272 (no migration): an attempt that ends **billed but unusable** gets its row too (`cassette.record_unusable_attempt`
  from `_attempt`'s except path): the reply the `BilledUnusableReply` carries (`reply_facts`, set by the Converse,
  Anthropic API and OpenAI-format adapters; empty when none), marked `stop_reason = 'billed_unusable[:<provider stop
  reason>]'` (`cassette.is_unusable`); replay by request sha never serves it. Billed records carry `route_trace` +
  `route_position`, so every `billed_unusable` ledger row (the probe's too) joins its cassette row. Link rule: one
  cassette row per attempt that got a reply, joined on (`route_trace`, `attempt_no`), never on the model name (the
  ledger names the answering label: acc3's legacy `haiku` and catalog `haiku-4-5` both log `haiku-4-5`). Embeddings
  and Jev write no cassette row, by design (a vector is not text; replay re-embeds).
- **Refusal noise: known no-access models** (AA-900, S271 lane l11, no migration). The ledger rule stays: every real
  provider call has one `shared.llm_call_log` row, refused included. What changes is how often a model with no grant
  is called and how its refusals are counted:
  - **Open period.** The access_denied period of a **known refusal** (another model of the connection answered after
    this model's streak began) doubles from 5 min up to **6 h** (`breaker.RULES`, `ACCESS_DENIED_MAX_OPEN_S`, was
    30 min): a fresh refusal still costs minutes (5, 10, 20, 40 min, ...), a model refused for ~10 h is re-probed by
    real traffic at most every 6 h. Any other access_denied (nothing on the connection answered since the streak
    began: the account itself may be refusing) keeps the 30 min cap (`breaker.ACCOUNT_DENIED_RULES`); the choice is
    made per failure on the locked row (`breaker.failure_rules`). quota_exhausted / credential / throttled / other are unchanged, and the non-LLM
    provider circuits (`shared/provider_circuit.py`) keep a 30 min access_denied cap (`breaker.PROVIDER_RULES`).
  - **How long until traffic returns to acc3 after AWS grants a model**: at the latest the next of (a) the daily
    `llm_connection_probe` (05:00 UTC, `LLM_DAILY_PROBE=on`), (b) an admin Test connection of that model, (c) the end
    of its open period (at most 6 h for a known refusal), then one real call probes it. (a) and (b) go through `LLMClient.probe` =
    `breaker.guarded(admit_call=False)`: an open circuit is not in their way and their success closes it (the
    10 s health snapshot of the other tasks then picks it up). **To force it**: AI gateway › Providers › Connections ›
    the connection › Test connection with that model (`POST /admin/llm-connections/{key}/test`
    `{"model_key": ...}`), or a live run of `POST /admin/job-runner/llm-probe/run` (every enabled deployment).
  - **Account recovery.** While a whole account refuses (Error 002, an IAM block: every model access_denied and
    nothing answering), no row is a known refusal, so every reopening is capped at 30 min and the connection circuit
    (OPEN until the newest open period of its member rows ends; `admit` turns every real call away meanwhile)
    half-opens within 30 min of its last failed probe: after AWS fixes the account, real traffic finds it within
    30 min by itself, sooner through the daily probe or a Test connection. When any model of a connection answers,
    the OPEN circuits of an account-wide class whose refusal began after the connection's previous answer (refusals
    the account itself may have caused: Error 002, a lapsed credit) turn half-open at once
    (`breaker._reprobe_after_account_recovery`, streak kept). A model refused since before another one answered (a
    **known refusal**, `breaker.known_refusal`) keeps its period (up to 6 h).
  - **Logs.** A known refusal's reopening (`llm_circuit_opened`), its failed deployment attempt
    (`llm_deployment_attempt_failed`) and a route rung refused only for that reason (`llm_route_attempt_failed`) log
    at info with `known_refusal=true`, not as warnings. `llm_deployment_skip` is logged once per (model, connection,
    reason) per 10 min and process (`vendor_route.SKIP_LOG_INTERVAL_S`); the next line carries `suppressed=<n>`.
  - **Expected refusal (display)**, one rule in `shared/llm_client/expected_refusal.py` (`EXPECTED_SQL` /
    `is_expected`): a row `status = 'refused'`, `error_class = 'access_denied'`, whose deployment's `access_state` is
    `access_denied` (its last probe found no access) on a connection whose `access_state` is `ok` (the account answers).
    Counted apart from errors in the Providers stats (`expected_refused`, left out of `refused`), the Calls tab
    (`expected_refusal` per row, own KPI), Route trace (`expected_refusals`, left out of `failed_attempts`), the route
    digest (`shared/llm_client/route_watch.py`) and `GET /admin/llm-availability` (a known model is
    `expected_refusal: true` and in `known_refused_models`; it does not make the connection `open` / `degraded`).
    An account-wide refusal is never expected (the connection's access_state is not `ok`), and a model whose last
    probe answered still counts as an error when it starts refusing, until the next probe records the refusal.
  - **Route digest scope** (AA-1141, S292): the daily route digest judges only routed stages
    (`route_watch.routed_stage`: a `shared.llm_role_config` row or a code route in `role_config.SAFE_DEFAULTS`, never
    an `adhoc_` name). The connection probe `adhoc_conn_test` (every model of every connection, refused ones included)
    and ad-hoc scripts are listed in the digest's `unrouted` field only and never raise its bell.
- **Data scopes per stage** (AA-1002 slice S5, migration 311 contract, app ADR 0012): `shared.llm_role_config.
  allowed_scopes` (`{aws_only}` or `{aws_only, vendor_direct}`, default `{aws_only}`, migration-only: `cis_app` has no
  INSERT / UPDATE on the column) says which connections (`data_scope`) a stage's content may use. Backfilled from the
  routes (both for the stages whose route ends on `gpt-6-luna-openai`, the S235 last resort), so no call changed
  (`test_aa1002_s3_routes_snapshot.py` `*_scoped` cases). `shared/llm_client/data_scope.py`; the scopes are bound per
  call in `LLMClient._generate` (`client.stage_scopes`) and filter `vendor_route.deployment_plan` /
  `vendor_deployment_for`, the OpenAI adapter (`openai_scope_allowed`, second gate after the S235 catalog gate) and
  `embed`. NULL / no row / column missing = no stage policy = the pre-S5 rules (S3 first-row rule, S235 gate). Admin:
  `GET /admin/llm-config` returns `allowed_scopes` read-only (Route editor › Data), options outside the scope are
  unavailable with a reason, and the PATCHes refuse such a model (422) and keep the S235 position rule in scope terms
  (a model reached only outside AWS: last fallback only). A new stage seeded with an OpenAI-API last resort must set
  both scopes in its seed migration.
- **Data class per stage** (AA-1039 R6, CIS 373 contract, root ADR 0019 d12): `shared.llm_role_config.pii_class`
  (`content` | `pii`, NOT NULL default `content`, migration-only). A `pii` stage must have `allowed_scopes = {aws_only}`
  (CHECK `llm_role_config_pii_scope_check`), and the gateway calls it only on a US deployment on our AWS accounts
  (`data_scope.pii_allows`: a `us.` profile or an in-region `<provider>.<model>` id on a `us-*` connection, never
  `global.`, a full ARN, an unknown prefix or a vendor API) in `vendor_route.deployment_plan`, a pinned
  `model@connection`, `embed`, the route invariant and the route preview; a path the registry does not describe
  (legacy chain, catalog fallback, the OpenAI adapter and the GPT-4.1 last resort check the class themselves) is
  refused for it, and so is Bedrock Batch (`s1_batch`: on demand instead). `pii_allows` also needs an AWS connection
  kind (`bedrock`, `bedrock_guardrails`, `mock`), not only the `aws_only` scope. A failed stage read with no cached row
  keeps the class of the stage's last successful read in the process (`role_config._pii_known`, never cleared by
  `invalidate()`: a class changes only by migration); a stage never read reads as `data_scope.PII_UNREAD` (`pii`, with
  `allowed_scopes = {aws_only}`; logs and errors say "stage class unknown (role_config read failed): US AWS
  deployments only"). The decision path never sends a `pii` stage's state to TypeSafe (a vendor outside our accounts):
  `decide()` reads the pii stages with its questions and answers every question `skipped` ("pii stage: no vendor
  decision"; a class read that fails before any good read skips every stage the same way, a later failed refresh keeps
  the last set), so the stage keeps its own rule. The route preview plans a legacy key of a pii stage through its alias
  targets, as `_legacy_call` does. Every stage is `content` today (no CIS stage takes
  personal data; the CRM's PII stages run in the CRM gateway). Admin: `GET /admin/llm-config` returns `pii_class`
  (null before 373) for AI gateway › Routing › "Routing by data class"; the route PATCHes and Route control refuse a
  model with no US deployment for a pii stage.
- **Free-text PII scrub** (AA-1113, root ADR 0009 d10, accepted S283; `shared/llm_client/free_text_scrub.py` +
  `pii_patterns.py`, a copy of the CRM log scrub and its F3 detectors, never imported across repos): the stages that
  carry tenant or traveller free text send only a scrubbed copy of their request (system, user, few-shots). Which
  stages: the registry column `shared.llm_role_config.free_text` (CIS 451, migration-only, read with the stage row as
  `StageConfig.free_text`; every tenant stage marked by 451) plus a fail-safe floor by name, every `t<n>_*` stage
  whatever its row says; a stage whose row could not be read on a cold cache is scrubbed too. A pii stage is never
  marked (CHECK): it needs its data. E-mails, phones, cards, CCCD numbers, passports, OTPs after a cue, JWTs,
  credentials and signed links become bracket labels (`[email]`, `[phone]` ...); dates, UUIDs and plain links stay;
  nothing is restored in the reply.
  `LLMClient._generate_with` runs it after the switch and budget gates and before the first attempt (so the route,
  legacy keys, the truncation retry, the shadow and the cassette see only the scrubbed copy); the admin `probe` too.
  **Fail closed**: a scrub that raises or leaves an e-mail / `+` phone refuses the call (`FreeTextScrubFailed`, one
  0 USD row refused / `other`, `quality_signal.pii_scrub.status = failed`). The receipt (counts per label and
  `scrubbed_hash`, the sha256 of the text the model gets; never a value) goes on the gateway's own rows of the request
  (`quality_signal.pii_scrub`: refusals, failed attempts) and the log line `llm_free_text_scrubbed`; the call site's
  success row is unchanged. Other stages get the same request object back. $0 live check:
  `scripts/llm_ops/aa1113_scrub_probe.py` (a planted e-mail and phone, a model key the catalog lacks, the row's hash).
  AI gateway › Routing › Routing by data class lists the free-text stages. A marked stage never batches (the batch
  lane sends prompts as built). Not covered yet: TripPlanner's planning chat (`tp_plan_chat`, no row, its own gateway).
- **Cost parity** (AA-1002 slice S6, migration 312 contract): `pricing.quote(model, Usage, deployment)` prices one
  billed request from its full usage (uncached input, output, cache reads, 5-minute writes 1.25x, 1-hour writes 2x,
  Message Batches 50% off every token, Anthropic `inference_geo: "us"` 1.1x, Claude Haiku 5.5 x5 above 100K prompt
  tokens) on the deployment it ran on (`llm_model_deployment.price_*_override` first, then the catalog, then
  `COST_TABLE`, then a flagged estimate at Sonnet rates: never 0) and returns `Quote(cost_usd, pricing_mode)`.
  `calc_cost` is a wrapper with the pre-S6 numbers (golden `tests/unit/snapshots/aa1002_s6_pre_s6_costs.json`, the
  Haiku 5.5 tier the only change). The Converse, Anthropic API and OpenAI paths price through `LLMClient._quote`
  and set `LLMResponse.pricing_mode`; OpenAI cached prompt tokens (`prompt_tokens_details.cached_tokens`) are billed
  at the cache-read rate and reported as `cache_read_tokens`. `llm_call_log.cache_read_tokens / cache_write_tokens /
  pricing_mode` (`override|catalog|code_table|estimate[+batch][+geo_us]`) are written only for a row that has
  something there (cache tokens, a stated mode, or an unknown model: `estimate` even when the caller is silent);
  other rows keep the pre-S6 INSERT, an AA-729 relabelled INSERT is never replaced, and before 312 the write falls
  back to the old statement. `llm_call_daily` gains the cache sums and `estimate_calls` (rollup switches to the S6
  statement once both tables have the columns). Every call site that logs `model=resp.model_used` also passes
  `**call_log.billed_fields(resp)` (cache split, pricing_mode, vendor `connection_key`; AST guard
  `tests/unit/test_aa1002_s6_billed_fields.py`); the judge paths carry the same keys (`brand_fit.RUN_BILLING_KEYS`,
  T10 `invoke_judge`). A vendor-API row stores its connection key as `account` (`openai-direct`,
  `anthropic-direct`; `LLMResponse.connection_key`, never `satellite_account`, which the pipeline copies to
  `generated_content` under an AWS-account CHECK); the admin names it by vendor ("OpenAI API",
  `accounts.VENDOR_CONNECTION_LABELS` = `frontend/lib/awsAccounts.ts`). **Prompt caching on Converse**: a Claude
  profile (`anthropic.` in the wire id) whose system prompt reaches the model's minimum cacheable length (512 tokens
  on the 5.x models, 1024 Sonnet 4.6 / 5, 4096 Haiku 4.5; chars / 4) ends its system list with
  `{"cachePoint": {"type": "default"}}`, so tools + system are cached and the user message is not; Luna / OpenAI on
  Bedrock never get one; a ValidationException naming the cache point is retried once without it and remembered;
  ECS env `LLM_CONVERSE_CACHE_POINT=off` restores the pre-S6 body.
- **OpenAI-format connections** (AA-1002 slice S9, no migration): `shared/llm_client/adapters/openai_chat.py` builds the
  SDK client of each `openai` / `azure_openai` connection at call time (`LLMClient` no longer builds one in
  `__init__` from `OPENAI_API_KEY`): the key from the connection's `aa-llm/` secret when it names one, else its
  allowlisted credential env (openai-direct's `OPENAI_API_KEY`, a fallback until `secret_name` is set on the
  Connections tab); endpoint from `base_url` on the kind's host allowlist (openai default `https://api.openai.com/v1`;
  Azure `https://<resource>.openai.azure.com` + `api_version` from `request_defaults.api_version`, default
  `AZURE_API_VERSION`; wire id = the Azure deployment name); per-connection timeout (NULL = 300 s, never the SDK's
  600 s; SDK retries off since AA-1024 A3); a 401 drops the cached key and client. An `azure_openai` connection
  serves `openai_chat` rows of an OpenAI model (S235 gate keyed on the catalog row; the stage scope reads the called
  connection's data scope); its reply is labelled `azure-openai-<key>` (llm_call_log provider `azure_openai`, account = the connection). A client set
  on `LLMClient._openai` (tests, scripts) is used for every OpenAI-format call.
- **Connection budget split** (AA-1113, tech lead S284; CIS 453, AA-Partner crm-077; root ADR 0009 d6 / 0008a): a
  model connection has one budget at its provider; each app that spends on it has its OWN monthly cap there and the
  shares together must stay under the provider budget. The CIS share is the connection's `monthly_budget_usd` (below;
  a limit only while the connection's cost policy is a hard stop: with `hard_stop` false it is alert only and the page
  counts CIS as uncapped. TripPlanner writes into the same ledger but does not enforce the cap yet, see the contract
  views below); the CRM share is the CRM setting `crm.ai.connection_monthly_cap_usd` (the CRM enforces it on its
  own ledger); the provider budget is the setting `cis.spend.connection_provider_budget_usd` ({connection key: USD},
  informational). Neither app reads the other's ledger, view or table: `GET /admin/ai-gateway/connection-split`
  (`api/services/ai_gateway_split.py`, pure rule in `api/services/llm_connection_split.py`) reads the
  CRM share through the CRM settings relay Platform › Settings already uses (crm.ops.v1 `/settings`, the staff
  member's own token) and AI gateway › Budgets shows provider budget, CIS share, CRM share, headroom and a flag when
  the shares add up to more than the provider budget (also when a share is missing or alert only on a budgeted
  connection, or the CRM could not be asked).
- **One budget per connection as rows** (AA-1143, tech lead decision G3 S285; CIS 455 / 456; root ADR 0009 d6 / d13,
  0008a): `shared.llm_connection_budget` (append-only versions, reason 20..500 characters, audited
  `llm_connection_budget_changed`) holds per connection its total (app NULL: the provider budget) and one fixed-USD share
  per app of `shared.llm_app_profile` other than cis; **CIS takes the remainder** (total minus the other apps' shares).
  The table's statement trigger refuses shares above the total and writes the remainder to `monthly_budget_usd`, so the
  CIS gateway keeps enforcing that column (no new read on the call path) and `PATCH /admin/llm-connections/{key}`
  refuses a direct edit of it on a connection with a total. The CIS month spend of a connection
  (`shared/connection_pool_spend.py`) counts every app without its own share whole and an app with a share
  only above it. Edited on ONE screen, AI gateway › Budgets › Connection budgets (`GET / PUT
  /admin/ai-gateway/connection-budgets[/{key}]`, `…/{key}/history`, `POST …/crm-sync`;
  `api/services/llm_connection_budgets.py`, rules in `llm_connection_split.py`); the Connections drawer shows them
  read-only with a link. TripPlanner reads its row through `catalog.v_llm_connection_cap_v2` (log-only first:
  setting `tripplanner.spend.connection_share_enforce`); an app with no share spends inside the CIS remainder. The CRM
  gets its copy over crm.ops.v1 `PUT /llm/connection-shares` (every connection with a total, its share or 0; a lower
  share is sent before the CIS rows are written, a higher one after) and falls back to its old setting for one
  release. The split read above and its two settings stay until the contract step.
- **Budget per connection + nightly cost reconciliation** (AA-1002 slice S10, migration 317 expand):
  `llm_provider_connection.monthly_budget_usd` is enforced per connection, independent of the job budgets
  (`shared.spend_budget`) and wave caps, which still apply. The policy is `shared.llm_connection_cost_policy` (no row =
  alert at 80 %, hard stop at 100 %): `shared/cost_guard.py` (`ConnectionBudget`, month-to-date spend = the
  connection's `llm_call_log` rows, UTC month) and, on the call path, `shared/llm_client/connection_budget.py` (60 s
  cache; a connection without a budget is never read; unreadable state fails open; `LLM_CONNECTION_BUDGETS=off`).
  `deployment_plan` leaves a connection over its hard budget out (`DeploymentPlan.budget_skips`, after the circuit
  check); a model / route with no other way in raises `connection_budget.ConnectionBudgetExceeded` (a
  `cost_guard.BudgetExceeded`: the job stops as `stopped_budget`, never retried). The paths that call a connection
  without a plan check the same rule first (`connection_budget.check`): the legacy chain (`haiku` / `sonnet` on acc3,
  the acc2-native tiers, the `gpt-4.1` tier and the T3 last resort on openai-direct) and the catalog path with
  `LLM_DEPLOYMENT_ROUTING=off` (Converse on the satellite, `openai_chat`; an Anthropic deployment over budget falls
  through to the satellite unless that is over budget too). At `alert_pct` one admin bell +
  ops e-mail per connection per 24 h. Job kind `llm_cost_reconcile` (`services/llm_cost/reconcile.py`, nightly from
  02:00 UTC via the ops metrics sampler, last 3 days, `LLM_COST_RECONCILE=off`; manual
  `POST /admin/llm-connections/reconcile`) writes one `shared.llm_cost_reconcile` row per connection and UTC day:
  logged cost vs the provider's figure — Bedrock from the STORED Cost Explorer snapshot of the connection's account (no
  live CE call; the GROSS usage from the snapshot's RECORD_TYPE split, since the net amount has credits netted in:
  acc3 is billed against a credit; a day stored before the split, or any figure below zero, is `no_data`, never
  alerted; one connection's failure never stops the others), Anthropic Admin API cost report / OpenAI organization costs only with a read-only usage key NAMED in
  the policy (`usage_secret_name`, `aa-llm/` only, read by `connections.resolve_usage_key`; hosts are code
  constants), Azure unsupported. Tolerance max($0.05, 5 %); a settled mismatch raises one bell per 24 h; rows older
  than 400 days are pruned by the job. Admin: `GET /admin/llm-connections/costs`, the policy fields in the same
  PATCH; UI: Connections tab (Budget column, drawer "Budget and cost check").
- **Reliability part 1** (AA-1018, no migration): a **connection-level circuit** derived from the deployment rows of
  `shared.llm_provider_health` (`breaker.connection_health`, shown as `model_key "*"`; not stored, because
  `model_key` references the catalog). It trips only on account-wide signals newer than the connection's last answered
  call: one deployment refused for a key (`credential`) or credit (`quota_exhausted`: OpenAI `insufficient_quota`,
  the Anthropic tier spend cap), or `access_denied` on two or more distinct models (one refused model never stops the
  connection, so acc2 embeddings survive a Claude refusal on acc2). While open, `deployment_plan` (checked before each
  deployment's own circuit, `vendor_route.connection_circuit_open`) and `breaker.admit` (every `guarded` call) skip
  every deployment of the connection; afterwards the deployment circuits decide; any success closes it. On the
  transition to open: one admin bell (`llm_circuit_open`, one unread per connection per 24 h) and the ops e-mail
  (template `llm_circuit_open`, once per connection, class and UTC day). Since part 2 the legacy chain (`haiku` /
  `sonnet` keys) goes through `guarded` too (see "Reliability part 2"). Jobs: backoff
  gets up to 25 % jitter (`queue.backoff_seconds`); `POST /admin/job-runner/jobs/retry-llm-denied` (ops.write, DRY
  RUN by default, audited `jobs_retry_llm_denied`) re-queues `failed` jobs whose error starts with
  `llm_access_denied` unless their kind refuses retries or their connection still refuses
  (`queue.retry_by_reason` / `breaker.connection_refusing`: the connection circuit is tripped, open or half-open, with
  no answered call since; at most 50 per apply); the jobs list takes `reason` (error prefix). The alert's provider
  text is masked (`errors.redact_ids`: ARNs and 12-digit account IDs) before it reaches the bell or the e-mail.
  `shared/llm_client/invariant.py`: the report-only failure-domain rule (domain = kind + account + data scope until
  migration 321), in `GET /admin/llm-availability` (`invariant`, and per connection `circuit` + `failure_domain`) and
  the OPS-12 digest provider section. Job kind `llm_connection_probe` (`services/jobs/llm_probe_job.py`: one Test
  connection per enabled text deployment of every enabled, configured connection, at most 30; max_attempts 1,
  background, deploy-gate exempt): daily from 05:00 UTC only with `LLM_DAILY_PROBE=on` (default off; **on** on Dev
  since AA-CIS-Infra #574, set on the api and worker task definitions);
  `POST /admin/job-runner/llm-probe/run` (ai.admin) queues a DRY RUN by default (deployments + estimated cost, no
  call). Bedrock runtime clients take the connection's `timeout_s` when set; since AA-1024 A3 every runtime client makes
  one attempt per request (botocore `total_max_attempts` 1, SDK `max_retries` 0: the gateway owns retries, at most
  `errors.MAX_CHAIN_ATTEMPTS` = 6 provider attempts per request, the 7th refused as a 0 USD `deadline` row).
- **Model lifecycle as data** (AA-1024 D2, OPS-04; migration 352): `shared.llm_model_catalog` carries
  `lifecycle_status` (active | legacy | unlisted | unknown | untracked), `eol_date`, `legacy_date`,
  `lifecycle_checked_at`, `lifecycle_detail` (per Bedrock connection: access, error class, wire id, matched model).
  Job kind `llm_model_lifecycle_sync` (`services/jobs/llm_model_lifecycle_job.py`, core in
  `shared/llm_client/model_lifecycle.py`): one free Bedrock `ListFoundationModels` per enabled Bedrock connection (acc2
  on the task role, acc3 through the satellite session; IAM in AA-CIS-Infra), then every catalog row; a model with no
  Bedrock deployment is `untracked`, a refusing account keeps the earlier answered status (else `unknown`), so no routed
  model stays NULL. Nightly from 04:00 UTC (`LLM_MODEL_LIFECYCLE_SYNC`, default on), background, deploy-gate exempt;
  `POST /admin/job-runner/llm-model-lifecycle/run` (ai.admin, `dry_run` optional). A routed model (active routes,
  fallbacks, shadows, aliases expanded) whose end of life is within 90 days, or that is LEGACY or unlisted, gets an
  admin bell `llm_model_lifecycle` (once per 7 days) and an OPS-12 digest attention line (section "Model lifecycle").
- **Reliability part 2** (AA-1018 / AA-1016 / AA-900, no migration; reads 320 / 321 / 322): **route deadline**
  `llm_role_config.route_deadline_s` (set on every row by migration 334: writer 1200, judge 900, validate 600, embed
  120 s; NULL = nothing bound): checked before every
  attempt after the first (route position, plan deployment, alias step, legacy tier), `RouteDeadlineExceeded` (ledger
  `error` / `deadline`, 0 USD); the OpenAI and Anthropic API adapters get the remaining time as their per-request
  timeout, never more than the connection's own (Bedrock keeps its client read timeout). Structured output on Converse
  follows the catalog capability `structured_output` (tool / any / auto / prompt, AA-1024 I6; unstated = tool, or auto
  for a model that refuses a forced tool choice). The paths that bypass the plan (`_legacy_chain` tiers on the catalog
  key they call, the S2 `vendor_deployment_for` path, the catalog-fallback Converse / OpenAI calls) run inside
  `breaker.guarded` and log `llm_deprecated_path_used` once per path and stage (`client.DEPRECATED_PATHS`: since
  2026-10-31, proposed removal 2026-12-31 after S11). **Tenant AI budget** (`shared/llm_client/tenant_budget.py`,
  rules in `cost_guard.TenantAIBudget`): before any attempt, for the job's tenant with a `shared.tenant_ai_budget`
  row only (no row = nothing read beyond a cached tenant set): bell + ops e-mail at `alert_pct`; at `approval_pct`
  with `hard_stop`, a T-stage call (`t<digit>_…`) raises `TenantBudgetApprovalRequired` (a BudgetExceeded: the job
  ends `stopped_budget` with error prefix `tenant_ai_budget_approval`, ledger `refused` / `budget`); staff raise the
  budget or switch the hard stop off, then re-queue (`retry-llm-denied` with `reason: tenant_ai_budget_approval`,
  held per tenant until it no longer needs approval). The T2 graph / `_rewrite_tour` and the T9 write re-raise the
  stop (`tenant_budget.approval_stop_in`; the worker reads the explicit cause chain too), and the `t2_rewrite` /
  `t9_write` on_terminal hooks leave the version `pending` / piece `processing` for such a job (the tenant's live view
  says "on hold"), so the re-queue writes it; an admin Cancel of the `stopped_budget` job is the decline (the hook then
  marks it failed). `LLM_TENANT_BUDGETS=off` switches it off. **Central LLM budgets** (AA-1025 A7, migration 340;
  `shared/llm_client/central_budget.py`): `shared.spend_budget` rows of provider `llm` cap every LLM provider together
  per scope: `app:<app>` (`llm_call_log.app`, NULL = cis), `stage:<stage>`, `job:<kind>` (that kind's jobs; its
  `per_run_usd` = the running job's spend), `eval` (an eval label bound, or `writing_memory_eval`; spend = the eval log
  stages) and `adhoc` (`adhoc_*` stages); `per_day_usd` (UTC day), `per_month_usd` (UTC month, llm rows only). Checked
  in `_generate_with` after the stage switch and the tenant budget: no `llm` row = nothing more read; a row with
  `hard_stop` whose cap spent + the call's worst case (`client.estimate_call_usd`: the route's first model, the prompt
  at ~4 chars per token, the whole `max_tokens`) would pass refuses the call with `CentralBudgetExceeded` (a
  BudgetExceeded: job `stopped_budget`, ledger `refused` / `budget`, 0 USD, nothing sent); an alert-only row never
  refuses (tech lead T-8: Dev caps alert-only). Bell + ops e-mail at `alert_pct` or on a refusal
  (`cost_guard.maybe_alert_central`, entity `llm:<scope>`). Rows 60 s, spend 30 s cached per process; unreadable =
  not enforced; `LLM_CENTRAL_BUDGETS=off` switches it off. 340 seeds one alert-only `app:cis` daily cap = p95 of the
  last 30 days x 1.5 (T-8). Edited on AI providers › Budgets (`PUT /admin/budgets/llm/{scope}`, `DELETE
  /admin/budgets/llm/{scope}`); the tab shows the central tier and the coverage per call site (which cap covers each
  CIS stage). **Prompt register**:
  `prompt_hash` = sha256 of the system prompt; `prompt_version` = `prompt_registry.version_for(stage)` (the manifest
  `shared/llm_client/prompt_versions.json` hashes the prompt symbols' source with `ast`; the version counts only when
  `shared.prompt_version` holds the same hash; migration 325 registered the 32 v1 rows), on every reply and attempt
  row; CI gate `tests/unit/test_aa900_prompt_registry_gate.py` / `scripts/prompt_registry_check.py` (`--bump`,
  `--sql --unregistered`; it also fails when no file in `api/migrations/` inserts a manifest entry). The
  breaker writes `llm_provider_connection.access_state` (open connection circuit → `access_denied` /
  `quota_exhausted` / `unconfigured`; any answer → `ok`; once per change and process, one write per connection at a
  time, none during the breaker's DB back-off, `DB_RETRY_S` back-off after a failed write); the probe stamps
  `last_probe_at`; `invariant.py` reads the stored `failure_domain`. OpenAI's 400 "max_tokens or model output limit
  was reached" is `truncated` (never counted on a circuit); an adhoc stage logs `LLMRequest.role`. Ratchets:
  `test_aa642_model_tier_allowlist.py` (fixed / passed-through `model_tier` sites), `test_aa642_s11_alias_equivalence.py`
  (the 17 legacy stages send the same wire ids through aliases; the switch stays off).
- **App profile** (AA-1025 H1a, migration 345, root ADR 0009 decision 4; `shared/llm_client/app_profile.py`):
  `shared.llm_app_profile` holds one row per app (`cis`, `tripplanner`, `crm`, `booking`, fixed by migration;
  `cis_app` has SELECT and column UPDATE only, 345 revokes `cis_owner`'s default privileges):
  `ai_enabled`, `bulk_paused`, `default_daily_budget_usd`, `degrade_mode` (tech lead T-21: CIS `queue_retry`,
  TripPlanner `basic_narration`, CRM `manual_reply_needed`, Booking `none`) and `disabled_reason`. The CIS gateway reads
  its own app's row (20 s cache per process; no table, no row or unreadable = AI on) before anything else: AI off
  refuses `generate` (before the stage switch), the connection test (`probe`), `embed()`, the daily embedding probe
  (`embed.probe`) and Jev `decide()` / the Jev canary with one 0 USD ledger row each (status refused, error_class `disabled`). The refusal is `LLMAppDisabled`, an
  `LLMStageDisabled` whose message names the stage, so jobs fail without a retry, T2 / T9 keep their version / piece to
  do, and the bulk re-queue holds every stage while the app's AI is off (`shared/jobs/queue.disabled_stages` returns
  `EveryStage`); Jev fails open as on its own switch. `bulk_paused` holds the bulk job kinds and pauses the open waves
  (H2, below); the default budget is stored and shown only (CIS enforces its central cap `llm/app:cis`; TripPlanner
  and the CRM read their row in H1b / H1c). `LLM_APP_PROFILE=off` skips the check. Admin: AI providers › Providers › App profiles
  (`AppProfilesCard.tsx`; `GET /admin/ai-gateway/app-profiles` spend.read, `PATCH /admin/ai-gateway/app-profiles/{app}`
  ai.admin, a reason to switch off, audited `llm_app_ai_switched_off` / `_on` / `llm_app_profile_updated`).
  **The other apps (AA-1025 H1b / H1c):** TripPlanner reads its row through `catalog.v_llm_app_profile_v1` (migration
  347, `tripplanner` only, six columns: no reason, no `updated_by`) and acts on it within its own 20 s cache (AI off =
  a 0 USD refused row and the basic narration / keyword search; `default_daily_budget_usd` = its daily cap over every
  `tp_*` stage). The CRM reads no `shared.llm_*` table (root ADR 0008a §6): `PATCH .../app-profiles/crm` first relays
  the whole profile to the CRM (`crm.ops.v1` 1.10 `PUT /llm/app-profile`, the staff member's own token, so it needs
  `crm-platform-admin`; `api/services/crm_console.ALLOWED`) and changes the CIS row only after a 200 (a refusal answers
  its status and changes nothing). The card's "Takes effect" (`enforced_here`) is true for cis, tripplanner and crm.
  The ops check `llm_spend_alert.tripplanner.near|cap` (`shared/ops_checks.app_spend_findings`, sampler tick): when
  TripPlanner's row has a budget, today's (UTC) `shared.llm_call_log` spend of app `tripplanner` against it at 80 % and
  100 %, admin bell + one ops e-mail a UTC day per threshold; the CRM's spend has no such check (its ledger is
  `crm.llm_call`, readable only with a staff token).
- **Capacity: lines, TPM limiter, backpressure, wave pause** (AA-1025 H2 = capacity S12, migrations 348 expand /
  349 contract; `shared/llm_client/capacity.py`). Every LLM call has a line: `bulk` (rerun waves and catalogue
  backfills), `prod` (tenant work, admin actions, anything outside a job) or `dev` (stage `adhoc_*`, eval runs, the
  probes); a job's line is its kind's (`capacity.KIND_LINES`, every registered kind listed, guarded by a unit test;
  the worker binds the kind, `LLMClient` binds the request's line and token estimate = prompt chars / 4 + max_tokens).
  `shared.llm_capacity_limit` (PK connection_key + model_key, `*` = the whole connection) holds `tpm_limit`,
  `bulk_pct` (default 50), `dev_pct` and `prod_guard`. `breaker.limit` reserves the estimate on every covering limit
  before the concurrency / rpm slot; a call that does not fit WAITS (prod / dev 65 s, bulk 180 s; bulk and dev also
  wait while a prod call waits = bulk yields to prod), then `LimiterSaturated` (throttled). Per process like
  `rpm_limit` (set a task's share). No estimate (embeddings, the connection test) = no reservation. Backpressure: the
  worker's claim leaves out held kinds (`capacity.claim_hold`, 10 s cache): a `prod_guard` circuit OPEN (connection
  circuit for `*`, else the deployment's) holds prod + bulk kinds; still refusing (half-open, nothing answered),
  `bulk_paused`, or a staff-paused wave hold bulk kinds; this process's TPM waits hold that line's kinds for 30 s;
  dev kinds (probes) are never held. Wave pause: while a guard refuses or the bulk pause is on the worker sets
  `shared.rerun_wave.paused_at` / `paused_by 'capacity'` / `pause_reason` on every open wave (status stays `open`, so
  the deploy gate, retention and the miner still see an open wave; the wave check does not end a paused wave), one
  bell `platform.rerun_wave.paused` per wave, and resumes them when it clears; `POST /admin/waves/{id}/pause|resume`
  (ops.write) for a staff pause, which the worker never resumes. The backlog sampler does not count held kinds as
  starved (`held_kinds` in its output). Admin: `GET /admin/ai-gateway/capacity` (spend.read: rows, hold, held kinds,
  kind lines, this api task's windows), `PUT` / `DELETE /admin/ai-gateway/capacity/{connection_key}/{model_key}`
  (ai.admin, audited `llm_capacity_limit_set` / `_cleared`; the model must be deployed on the connection). No row =
  no limit and no hold; tables missing or unreadable = fail open. `LLM_CAPACITY=off` (ECS env) turns it off. Live
  drill at $0: `scripts/aa1025_h2_tpm_drill.py` (ECS exec; a drill model key no route uses). No UI card yet.
- **Wave stop flags** (AA-1024 G17): the quality read of an S1 rerun wave and its six stop flags live in
  `services/waves/quality.py` (gate codes `services/quality/s1_gates.py`, figure coverage
  `services/quality/s1_coverage.py`); `scripts/aa645/wave_report.py` is its CLI (exit 3 a flag true, 5 pending while
  any queued / running `shared.job` row names a wave tour: `tour_id`, a `tour_ids` list, or a t2_rewrite
  `published_tour_id` through `published_tours.id`). Code-only, $0, no write. Never run inside an API request: the
  code checks take 16-75 s per wave (S272), past the API Gateway's 29 s; `POST /admin/waves` does not read them.
- **Non-LLM provider circuit and the vendor API credit ledger** (AA-1025 B3 / B5, migrations 342 / 343):
  `shared/provider_circuit.py` keeps ONE circuit per decide / data provider connection (`typesafe`, `dataforseo`,
  `geoapify`) in `shared.provider_health` (PK `connection_key`; the breaker's pure transitions and `RULES`, a 10 s
  snapshot, row-locked writes, per-process fallback when the table cannot be read), so both ECS tasks see the same
  state. Each client asks `admit` before a call (open = nothing sent, its own 0 USD refused ledger row: Jev
  `llm_call_log` with the class that opened the circuit and `quality_signal.refusal = circuit_open` (S275; the class
  CHECK has no circuit class; the Jev health on AI providers counts it under `refused`, never `errors`), DataForSEO
  `dfs_call_log` meta `circuit_open`, Geoapify `GeocoderUnavailable`) and records one outcome
  per call after its own retries: HTTP 401 / 403 `access_denied`, 402 `quota_exhausted`, 429 `throttled`, 404 / 405 /
  408 / 410 / 5xx / timeout `other`, any other 4xx not counted. On opening: one admin bell `provider_circuit_open` (one unread per
  connection per 24 h) and one `ops_alert_email` job (template `provider_circuit_open`, once per connection, class and
  UTC day); a Jev 402 keeps only its own exhausted alert. `PROVIDER_CIRCUIT_BREAKER=off` turns it off. Decide's
  per-process credit breaker stays as the fast local stop. **Credit ledger**: `shared.provider_credit_topup` holds the
  top-ups and balance reads of every connection whose `balance_source` is `topups_minus_spend` (343: the anthropic /
  openai kinds; Jev keeps `shared.jev_credit_topup`); `shared/provider_credit.py` estimates newest balance read (or
  first top-up) + later top-ups − `llm_call_log` spend since (connection_key, else account) and, below the
  connection's `low_balance_usd`, raises `platform.provider_credit.low` (one bell per connection per 24 h) + the
  `provider_credit_low` e-mail (the api sampler every 30 min, after each entry, `POST /admin/ai-gateway/credit/check`).
  Admin: AI providers › Providers › Credit (`ProviderCreditPanel.tsx`, `GET/POST/DELETE /admin/ai-gateway/credit…`,
  spend.read / spend.write, audited); the Providers rows carry `circuit` and the vendor API `balance`.
- **Route governance** (AA-1024 A2 / A4 / A6, AA-1026 D7; root ADR 0009 decisions 5 and 12, no migration):
  **family rule** (`shared/llm_client/family_rule.py`): a judge never scores text of its own model family
  (`CatalogModel.family_key`, migration 320); writers stay on `WRITER_FAMILY` (`anthropic`). The admin options and both
  PATCH routes read the family, not the vendor, with English reasons. The eval runners check it twice: before the run
  (`stage_pair_conflicts`: the writer stage's primary against every model of the judge route; R2
  `check_judge_family` refuses, its DRY RUN reports `judge_family_check`; the S1-old regression raises
  `SameFamilyJudge`) and at call time (`trace_models`: `LLMClient.generate` records `(role, model label)` of each
  reply while a trace is bound; an R2 item judged by the writer's family is an `error` row with `same_family`, a
  regression tour makes the run INVALID with `same_family_tours`). **Evidence** (`shared/llm_client/route_evidence.py`):
  a change of a writer / judge stage's primary model or fallback chain needs `eval_run_id` (a finished
  `shared.prompt_eval_runs` row that measured the change: not INVALID (`details.invalid`), a writing-memory run with
  verdict `pass` and stop reason `completed`, a legacy regression row with no regression) or `break_glass_reason` (≥ 20 characters):
  422 otherwise, 503 when the run cannot be read; the evidence goes into the `acp_shared.audit_log` row
  (`details.evidence`) and a break glass writes an admin bell (`platform.llm_route.break_glass`) in the same
  transaction. A shadow-only, an account-only change and validate / embed stages need none. **Warnings**: both PATCH
  routes answer `warnings[]` from the failure-domain rule on the saved route (`invariant.read` with the refusing
  circuits, as the digest runs it; report only, never blocks). **No per-request model**: the rewrite / ingest / batch
  request bodies have no `model_tier` (a value = 422, `api/core/model_tier.py`; an explicit null is ignored); the
  `s1_rewrite` idempotency key is tour + batch, the batch ingest runs on the route; eval and probe callers still pass
  a model to `_rewrite_tour` / `LLMRequest`.
- **Shared core `aa_llm`** (AA-642 S8, root ADR 0009 decision 13): `shared/aa_llm/` is the pure, standard-library
  core of the gateway (error classes, `quote` with the catalog passed in, the ledger v2 row and INSERT, the budget
  decision, the 323 contract SELECTs) that TripPlanner (`backend/aa_llm/`) and AA-Partner (`app/aa_llm/`) vendor byte
  for byte; `check_copy.py` + `MANIFEST.json` pin each copy. Never edit a copy: change it here, run
  `python3 shared/aa_llm/check_copy.py --write`, merge, and the **Llm Core Sync** workflow (`llm-core-sync.yml`, bot
  branch `bot/llm-core-sync`, never merges) opens the consumer PRs; `llm-core-consumers.yml` runs each consumer's tests
  on a CIS PR that touches the core. **CIS runs on the core** (AA-1024 A10): `shared/llm_client/errors.py`,
  `pricing.py` and the ledger v2 values of `call_log.py` import it (`attempt_outcome` asks the core's
  `ledger.failed_outcome` / `outcome_of` first and adds only the CIS-typed refusals: breaker, limiter, budget
  exceptions, OpenAI platform block); `tests/unit/test_aa1024_a10_core_identity.py` checks the names are the core's
  own objects and compares a CIS ledger row with the core's row column by column. The core (1.1.0) also owns the
  outcome classes `truncated` (from the provider's text too), `disabled`, `role_not_served`, `deadline`, `budget`.
- **Contract views for TripPlanner** (migration 323, contract class): `catalog.v_llm_gateway_config_v1` (routes,
  models, prices and deployments of the `tp_*` stages only) and `catalog.v_llm_app_spend_today_v1` (today's ledger
  spend of `app = 'tripplanner'` per stage), owned by `catalog_owner` with column-level SELECT on the four gateway
  tables they read, SELECT to `tripplanner` only. 341 (AA-1027 E3) appends the route deadline and the deployment
  routing facts and circuits; 350 (E4) appends the model's `supports_temperature` and `stage_params` (the
  `shared.stage_model_params` row of the stage and model, NULL = none), which TripPlanner applies with this gateway's
  precedence. 351 (E5) revokes the grandfathered SELECT of CIS 173 on `llm_role_config` / `llm_model_catalog`:
  TripPlanner reads the views only (fail closed) and keeps INSERT on `llm_call_log`. 452 (AA-1113) appends
  `free_text` (the stage's scrub mark) and `over_cap_connections` (the connections of the model's deployments this
  gateway leaves out for their monthly cap), and adds `catalog.v_llm_connection_cap_v1` (each capped connection's
  month-to-date ledger spend, `over_cap`, `stops`; SELECT to `tripplanner` only): the CIS share of each connection,
  published for TripPlanner. **TripPlanner does not read them yet**: its gateway (`backend/aa_llm`) still checks only
  its daily app budget, so TripPlanner calls go on past the cap and, with the CRM share added, the provider budget can
  be exceeded on the TripPlanner path. Follow-up: TripPlanner reads `v_llm_gateway_config_v1.over_cap_connections`,
  leaves those deployments out and refuses with `budget` when every rung is over cap (hard_stop mode). The CRM reads no
  CIS view (root ADR 0008a, tech lead S284): see "Connection budget
  split".
- **acc2 candidate models** (migration 324, contract class, AA-1017 w4a): Amazon Nova Lite, Nova 2 Lite, Meta Llama 4
  Maverick, Llama 4 Scout and Moonshot Kimi K3 as catalog rows with prices from the AWS price list and one enabled acc2
  deployment each (priority 20); no role config or alias names them, and the file refuses one that does. They answer
  the probe and `POST /admin/llm-connections/acc2/test` (body `max_tokens` up to 512 for the reasoning model). "Add a
  model" = one migration PR like this one.
- **acc2 fallback of judge / validate stages** (AA-1026 I6, migration 331 contract): the eight platform judge / validate
  stages whose route reached acc3 only (`s1_brand_audit`, `s1_source_facts`, `a3_atomize`, `a3_place_card`,
  `a3_place_card_check`, `a3_place_days`, `a3_place_locate`, `a3_place_dedupe_check`) end their route on an acc2 model:
  `nova-lite` (the cheapest acc2 model that meets every one of their output contracts), `llama4-scout` for
  `a3_place_card_check` (draft and check stay on two makers). The primary, earlier fallbacks, shadow and scopes are
  unchanged; writers never name an acc2 model. The tenant judge / validate stages (`t2_brand_audit`, `t10_claims`,
  `t9_faq_answer_fit`, `t9_faq_filter`) wait for migration 332 and the tech lead's yes: ADR 0009 decision 14 keeps acc2
  off tenant stages until decision 10 and AA-820 are done. **Migration 333** ("AWS first, outside last", tech lead
  S263): every judge / validate route that ends on the OpenAI API hop (`gpt-6-luna-openai`) gets an acc2 pick right
  BEFORE it, so the OpenAI API stays the last rung: `s1_judge`, `t2_judge`, `t7_debate_judge`, `t10_judge`,
  `t10_adjudicate`, `writing_memory_eval`, `writing_memory_mine` on `nova-lite`. `a3_place_dedupe` was left out of
  333: it forces a tool choice, its check stage falls back to Nova Lite (Amazon), and Llama 4 Scout refused Converse
  `toolChoice.tool` in the live smoke (333 records `forced_tool_choice: false` on `llama4-scout`). **Migration 339**
  (S264) closes it: `llama4-maverick` (Meta) right before its OpenAI API hop, asked for the verdict in the prompt
  (`structured_output: prompt`, 337); the place pair keeps two makers (dedupe OpenAI + Meta, check Anthropic +
  Amazon), and its live smoke passed on 02/11. These
  judges' acc2 verdicts are not held by code (only the brand audits' `pass` is); the revert is the admin route PATCH. **Contract smoke** (job kind `llm_contract_smoke`,
  `services/jobs/llm_contract_smoke_job.py`; `POST /admin/job-runner/llm-contract-smoke/run`, ai.admin, DRY RUN by
  default, optional `stages` and `models` = stage -> model key override): per stage one invented fixture through the
  stage's own prompt pieces and parser, with the stage's acc2 model forced through `LLMClient.probe` on its acc2
  deployment (the override, else the route's acc2 model, else the planned one), ledger stage `adhoc_aa1026` with the
  stage's role, `quality_signal.contract_smoke` / `parser_ok`; refused over 0.10 USD estimated, about 0.01 USD for the
  twelve stages of 331 / 332 (estimate 0.02 USD for all twenty cases: the eight judge / validate stages whose route
  ends on the OpenAI API have cases too, seven of them planned for migration 333, which puts an acc2 pick before that
  hop; since 339 `a3_place_dedupe` is planned on `llama4-maverick`). It proves format only (ladder rung 3).
  A stage that got no usable reply (a refused or failed provider call, a billed unusable reply) is FAILED in the
  summary, never absent, and writes a 0 USD smoke row with `parser_ok` false and the error (the order gate reads the
  latest row). A structured stage (its request carries a JSON schema, so Converse forces the tool choice) is not run on
  a model whose catalog row states `forced_tool_choice: false` (`requires` / `why_not` in the plan), unless that row
  also states a non-forced `structured_output` mode (any / auto / prompt). **Order gate:** 331 / 332 / 333 refuse to give a stage its acc2
  fallback unless the LATEST smoke row of that stage on that model (connection acc2) passed, so the smoke runs before
  the apply; a stage that fails is re-picked (smoke the next model with `models`, change the migration's pick) or
  dropped before the apply, and after the apply re-pointed with the admin route PATCH. (A database that never logged
  an LLM call, a CI or local replay, skips the gate with a NOTICE.) The validate stages go live on the format smoke
  alone (the tech lead's S263 yes skips calibration, A/B and shadow for them); the brand audit is the one judge, and
  **an acc2 brand-audit `pass` is held**: `brand_audit_node.hold_uncalibrated_pass` turns it into `manual_check` +
  `BRAND_AUDIT_UNCALIBRATED_JUDGE` (connections in `UNCALIBRATED_JUDGE_CONNECTIONS`), so an uncalibrated judge can flag
  or hold a tour but never publish one. `draft_card` / `check_card` (place cards) take an optional `client` for the
  smoke. **Reply parsing:** the stage parsers without a forced schema read the reply's FIRST complete JSON value
  through `shared/llm_client/json_reply.first_json_value` (prose or a fence before it, any text after it ignored, a
  cut-off reply never yields a nested value; the item / shape checks stay in each parser): `t9_faq_filter` and
  `t9_faq_answer_fit` and `t10_adjudicate` (which also take the bare items list: both acc2 models answered the S263 smoke that way),
  `a3_place_days`, `a3_place_locate`, `a3_atomize` (`atom_extraction.atoms_from_reply`), the brand fit judges
  (`brand_fit._judge`: still no json_repair), and before their json_repair salvage `s1_source_facts`, the place card
  draft / check and the T10 F9 judge (`judge.parse_judge_json_checked`: a first value read as is is not "repaired").
  The schema-forced judges / validators (brand audit, `t10_claims`, `a3_place_dedupe(_check)`, the writing memory
  judges) keep `json.loads`.
- **Structured output (`LLMRequest.json_schema`) per path**: Converse = one tool whose input is the schema, reply
  conformed (`_checked_json`, AA-659/714); OpenAI = strict `response_format`. The legacy `haiku` / `sonnet` keys
  (Anthropic Messages API, `invoke_claude`) have neither: since S251 (AA-835) the gateway appends a "one JSON object
  matching this schema" instruction with the schema to the system prompt, takes the reply's JSON object (a ``` fence
  is tolerated, prose is not), conforms it, and raises `BilledUnusableReply` otherwise (gateway billed_unusable row,
  the route moves on). It warns `llm_legacy_key_structured_output` once per stage. A structured stage belongs on a
  catalog key; `tests/unit/test_s251_legacy_structured_output.py` lists the structured stages and the one known
  legacy primary (`a3_place_dedupe_check`). A structured request never streams live text (any path). A bad reply is
  typed: `UnusableReply` (`BilledUnusableReply` from one key, `RouteUnusableReply` when every model of a route
  answered unusably; an outage stays a plain `RuntimeError`); callers that must not retry or stop on a bad reply catch
  it, charge `unusable_reply_cost(e)` to their run budget and do not log again (supply writer → `DraftUnusable`;
  place dedupe check → `unsure` for that pair).
- **Stage Route** (AA-659, ADR 0006): each stage has a model plus an ordered fallback list and an
  optional shadow model (A/B, `shared.llm_shadow_log`). Only models the route lists are ever
  tried. No judge has its own hardcoded model path any more. **S235 rule (Nghiệp, 14/10/2026,
  migration 224): Bedrock first; an OpenAI platform API model may only be the LAST fallback of a
  route (a Bedrock outage), never a primary or a shadow; GPT-4.1 is retired.** Judge routes on Dev:
  - `s1_judge`, `t2_judge`, `t7_debate_judge`, `t10_judge`: GPT-5.6 Luna (Bedrock acc3) → GPT-6 Luna
    (Bedrock acc3) → GPT-6 Luna (OpenAI API, last resort); shadow GPT-6 Luna at the admin-set rate.
  - **One judge client** (AA-765): every judge request — S1/T2 brand fit and the T7 Debate candidate fit
    (`brand_fit._judge`), the T10 F9 judge (`quality_gates` via `invoke_judge`) — is built and sent by
    `shared/llm_client/judge.py` (`call_judge`; it also holds the judge JSON parsers). It writes no
    `llm_call_log` row: the callers (judge_node, debate, quality_gates) do, once they have the verdict.
  - `s1_brand_audit`, `t2_brand_audit` (and every judge since migration 366): GPT-5.6 Luna → GPT-6 Luna (acc3) → GPT-6
    Luna on the OpenAI API (its vendor twin, model first, S267) → Nova Lite (acc2).
  - **A-series writers** (AA-1026, migration 327, tech lead T-3 in S263): `s1_generate`, `s1_flag_fix`,
    `s1_itinerary_nudge`, `a0_column_map`, `supply_content_writer` have `allowed_scopes = {aws_only, vendor_direct}`
    and end on GPT-6 Luna (OpenAI API) as the LAST fallback. Route order is MODEL FIRST (tech lead S267, root ADR 0009
    decision 5 amendment, AA-1026; it replaced S263's "every AWS path before any vendor API"): the gateway tries every
    deployment of a model (acc3 by priority, then the same model on the Anthropic API once its key exists) before the
    route's next model, so the family chains of migration 366 run e.g. Sonnet 5.5 on acc3, Sonnet 5.5 on the
    Anthropic API, Sonnet 4.6 on acc3, Haiku 4.5 on acc3, then GPT-6 Luna on the OpenAI API (5 attempts, within the
    6 per request). The order mode `aws_first` (`client.route_attempts`) runs every AWS deployment of the route first
    instead. AA-1042 (S268, CIS 370): the order mode is a DB setting (`shared.llm_route_settings`, read by
    `shared/llm_client/route_settings.py`, 20 s cache; `LLM_ROUTE_AWS_FIRST` is only the bootstrap default while the
    row cannot be read), and the admin's **Route control** tab (`/admin/ai-gateway?tab=controls`, router
    `admin_llm_route_control.py`, service `api/services/llm_route_control.py`) edits the order mode, every stage's
    chain (drag and drop) and every model's provider order (`llm_model_deployment.priority`), with three presets
    ("Prefer Anthropic API for Claude", "Prefer AWS", and AA-1079's "Serving model first on the judge routes": for
    `t2_judge` / `t7_debate_judge` / `t10_judge`, the model that serves now moves to the front while every rung ahead
    of it is refused or circuit-open; the only route change allowed to put a vendor-only model first, shown as a
    warning; revert it when the closed rungs answer), a $0 server preview (`llm_route_chains.resolve_stage` under
    context-variable overrides; "serves first" is the deployment that would SERVE: one the account refuses on record,
    by the Stage coverage card's own rule `stage_coverage.refusal`, is skipped and listed with its reason) and the
    rules on every save (the PATCH rules, judge family, check pairs, attempt cap,
    a live model per stage; a rule the live config already breaks is only a warning). Every route change, the Routing
    tab's PATCHes included, is a version in `shared.llm_route_config_version` (who, when, snapshot, diff;
    `shared/llm_client/route_versions.py`), revertible in one click; a change made outside the admin is recorded as
    an `external` version; each CIS ledger row carries `route_config_version` (a BEFORE INSERT trigger). A model reached ONLY outside AWS stays the end of the route: both admin PATCH endpoints refuse
    an AWS model behind it (`_check_aws_before_vendor`, relaxed S267), except its VENDOR TWIN right behind the same
    model on AWS (`data_scope.vendor_twins`: same family and base model, today `gpt-6-luna-openai` behind `gpt-6-luna`,
    the "openai-direct for a GPT twin if one exists" of S267 Q2: every GPT route of migration 366 runs GPT-6 Luna on
    acc3, then on the OpenAI API, then its next model); the route PATCH accepts the one other-maker
    last fallback on a vendor_direct writer (`_writer_last_resort`); the writer / judge family rule holds for the
    primary, every other fallback and the shadow. A draft written by that last resort is judged by a GPT judge (same
    maker): its ledger row says `provider = 'openai'`, `fallback_used = true`; an eval run refuses that verdict
    ("Route governance" above).
  - **No uncalibrated rung (S271 W0, tech lead D7, CIS 383, break-glass):** since 383 no writer route names a model of
    another family than its primary (the OpenAI API rung `gpt-6-luna-openai` left all 13 writer routes: a writer
    whose Claude rungs all fail stops and re-queues, `degrade_mode` queue_retry), and no judge route nor the
    `t10_claims` instrument names Nova Lite (`nova-lite`, kappa 0.137 against the human labels): a judge whose GPT
    rungs all fail returns no verdict instead of an uncalibrated one. The FAQ checks and the A3 / S1 evidence producers
    keep Nova Lite (decided per row, migration 383 header). CI lint `tests/unit/test_aa900_w0_route_lint.py` checks
    both rules on the latest route snapshot and `SAFE_DEFAULTS`. The text above on the writers' OpenAI API last
    resort describes the 327-376 routes; the admin route PATCH still accepts that last resort
    (`_writer_last_resort`) until a code change refuses it.
  - **AWS preferred, not AWS only** (tech lead S267, root ADR 0009 decision 5 amendment; it replaced T-3, which kept
    tenant content, TripPlanner, the CRM and the validate stages inside AWS): migration 366 gives EVERY active text
    stage (writer, judge, validate) `allowed_scopes = {aws_only, vendor_direct}`, so each one reaches the Anthropic API
    (one key, connection `anthropic-direct`, 150 USD a month, alert 80 %, hard stop) for its Claude 5.x primary and,
    the OpenAI API (GPT-6 Luna's twin on the GPT routes, the last rung on the writers). Only the embedding stages stay `aws_only`
    (no vendor embedding deployment). The data protections stay as they are: the tenant AI budget (fail closed,
    `tenant_budget`), the per-connection monthly caps, the tenant `data_region` (ADR 0019; the per-stage `pii_class`
    routing it plans is not in this gateway yet) and the CRM's PII scrubbing in its own gateway. `llm_route_chains.violations` reports a text
    stage left on `aws_only` (`aws_only_text_stage`). TripPlanner's own gateway still calls Bedrock only, so it skips
    a vendor deployment on `tp_compose` until it gains the vendor adapters; the CRM routes in its own gateway
    (AA-Partner), outside `shared.llm_role_config`. Per-product spend stays in the ledger:
    `llm_route_chains.product_of` / `ledger_app` map every stage to catalogue, tenant content, TripPlanner, Booking or
    shared, and to the `app` the admin Spend > By app page groups by.
  - acc3 is the only chat account a route uses today (AA-998; the acc2 candidates of 324 have no route). The legacy Haiku/Sonnet chain is acc3 Sonnet → acc3 Haiku
    (no cross-account tier); `account_route` is CHECKed to acc3 / NULL (migration 295) and the gateway ignores any
    other value. `CatalogModel.account_for` only picks acc3 or acc2. They no longer try acc2-native Bedrock first (AA-843:
    acc2 is a channel-program account and always refuses Anthropic models); the env var
    `LLM_LEGACY_ACC2_NATIVE=on` restores that first attempt if acc2 ever gains model access.
  - A call served by the OpenAI API logs `llm_openai_fallback_used` and is recorded in `llm_call_log`
    with `provider = 'openai'`, `fallback_used = true`.
  - **Brand audit fails closed** (AA-714): a model/schema error gives `manual_check` +
    `BRAND_AUDIT_UNAVAILABLE` (held for review), never `pass`. A `pass` answered on an uncalibrated judge connection
    (acc2, AA-1026 I6) gives `manual_check` + `BRAND_AUDIT_UNCALIBRATED_JUDGE`.
  - **Where a GPT model runs**: on Bedrock only on acc3 (acc2 has no OpenAI models), or on the
    OpenAI platform API (`gpt-6-luna-openai`, last resort only). `gpt-4.1` is disabled (migration 224)
    and admin cannot re-enable it; `LLMClient._call_openai` refuses any model whose catalog row is not
    enabled, so the legacy Haiku/Sonnet chain no longer falls back to GPT-4.1. Admin rejects an
    OpenAI-API model as primary, shadow or non-last fallback. AI providers › Routing shows each route.
- **Account-level refusals fail fast** (AA-900, `shared/llm_client/errors.py`): a provider error is classified as
  `access_denied` (Bedrock "Error 002: Access to Bedrock models is not allowed for this account", AccessDenied, IAM,
  OpenAI 401/403), `quota_exhausted` (no OpenAI credit), `throttled` or `other`. When EVERY model a request tried was
  refused at account level (route, single key or the legacy Haiku/Sonnet chain), the gateway raises `LLMAccessDenied`
  (a RuntimeError; a mixed or throttled failure stays the plain RuntimeError). The job runner
  (`worker.failure_outcome`) fails such a job at once, no retry, error prefixed `llm_access_denied`; the S1/T2 writer
  re-raises it out of the graph (`graph.generate_node`, `s1_writer_v2_live._call`, `_rewrite_tour`) and
  `_run_tour_safe` stops its internal retries. Every other `_rewrite_tour` caller (eval regression, writing-memory
  eval writers, the T3 QA gate) also gets the exception instead of a `failed` dict, so an eval run aborts rather than
  record one refused tour after another. A refusal after a paid draft keeps the draft: the S1 sonnet upgrade
  (`_sonnet_upgrade` returns None) and the writer-v2 meta calls. Per-item loops that swallow LLM errors (A3 atomize
  days) are unchanged.
  AI providers › Routing opens with the **LLM availability** card (`GET /admin/llm-availability`,
  `api/services/llm_availability.py`): answered calls per provider / account from `llm_call_log` plus classified
  errors of failed (dated by `finished_at`) or re-queued jobs from `shared.job`; no probe call. Answered calls only:
  the gateway's 0 USD rows of refused / failed attempts (AA-804, `status` refused | error) are left out of it, so job
  errors stay the refusal side of the signal there.
- **Every model call goes through the gateway** (`shared/llm_client/`, AA-685): text through
  `LLMClient.generate(stage=...)`, embeddings through `shared.llm_client.embed.embed(stage, texts)`
  (catalog `api_style='embed'`, stage role `embed`). `tests/unit/test_aa685_no_raw_model_calls.py` fails
  on any raw model call outside `shared/llm_client/` in `api/`, `services/`, `shared/`, `worker/` and `scripts/`
  (AA-642 L6): Bedrock `invoke_model*` / `converse*` / `apply_guardrail` / `create_model_invocation_job` (called
  or passed uncalled), vendor SDK `.completions|.messages|.responses|.embeddings .create|.stream|.parse`,
  importing `openai` / `anthropic` or a gateway internal, and a vendor endpoint string (`api.anthropic.com`, `api.openai.com`, `bedrock-runtime`). Its
  `ALLOWED` list (path + hit, with a reason, ratcheted) holds only the cost reconcile's billing API URLs.
  **One `llm_call_log` row per billed call** (AA-927):
  - one row per `generate()` / `embed()` response returned to a caller, written by the caller, also when
    the reply then turns out unusable. Canonical pattern: write the row right after `generate()` returns
    (T9 `_generate_full`); where the row needs post-processing, keep a `logged` flag and call
    `call_log.record_unusable_sync(resp, ...)` in the except branch;
  - one row per billed provider attempt the gateway throws away (Converse schema retry failing again,
    empty content, invalid JSON / schema on OpenAI) — written by the gateway itself
    (`quality_signal.gateway_attempt = true`), fire-and-forget, before the route moves on or raises;
  - a gateway-internal retry that succeeds (the AA-714 Converse schema retry) is ONE response whose cost
    and tokens cover both requests (`LLMResponse.provider_requests = 2`);
  - the **truncation retry is the gateway's only** (S271; since S271 W0 / AA-900 also for T8 angles, T9 write /
    repair / FAQ filter, whose `services/acp_shared/token_budget.py` is now a ceiling calculator: no own retry, no
    `budget_retry=False`, because it never saw an EMPTY reply cut at max_tokens and the route swapped the model
    silently). It is TWO responses and two rows: the reply cut at its token ceiling (the gateway's row) and the retry
    with double the ceiling (the caller's row, `truncation_retry: true`, `retry_reason` = `cut_reply` |
    `billed_unusable_cut` | `truncated_refusal`); never a third call. No production request says
    `budget_retry=False` (**gateway's own truncation retry**, S271,
    `shared/llm_client/token_floor.py`, rules in the core `shared/aa_llm/tokens.py`): per route model in
    `LLMClient._call_key_logged`, a cut reply / a refused cut reply (OpenAI 400) / a billed cut reply is asked once
    more on the same model with double the ceiling (cap 24000 and the model's `max_output_tokens`) before the route
    moves on; the cut reply that came back gets the gateway's row (`call_log.cut_reply_row`: status truncated,
    `budget_cut`, `gateway_attempt`), the retry's reply carries `LLMResponse.truncation_retry_max_tokens` and the
    caller's row gets `truncation_retry: true` + `max_tokens` through `billed_fields` (`call_log.RETRY_KEYS`); same
    route_trace, the retry reports the cut attempt's `attempt_no` (never read as a fallback rung). Not retried:
    `token_floor.NO_RETRY_STAGES` (`adhoc_conn_test` only; AA-1053: a3_atomize's own re-ask is gone, it stops on a read
    still cut and stays out of the floor, `NO_FLOOR_STAGES`; guard `tests/unit/test_aa1053_one_retry_owner.py` fails on
    any re-ask after a cut outside the gateway), shadow calls, a request at the model's cap,
    past the route deadline, `LLM_BUDGET_RETRY=off`. And the **floor**: a reasoning-capable catalog model (Claude 5.x,
    GPT-5.x / 6.x, or `capabilities.reasoning`) is sent at least 4096 output tokens (or `capabilities.
    min_output_tokens`), over a call site's explicit value, after the stage x model row (`_call_catalog_model`;
    `LLM_TOKEN_FLOOR=off`). Guard: `tests/unit/test_s271_gateway_token_budget.py` (literal sub-floor ceilings are a
    shrinking allowlist, no production `budget_retry=False`, every migration model key classified) and
    `tests/unit/test_aa900_s271_w0_gateway.py` (every budgeted call-site ceiling >= `token_floor.effort_floor(model,
    effort)` of every model of the stage's route, read from the migrations). Alert:
    `route_cut.<stage>` (route_watch, > 5 % of a stage's replies cut over 24 h, setting
    `cis.ops.reply_cut_alert_pct` since AA-1171, refused cuts and eval rows included). A ceiling is head-room, not the expected size: since t9_write v2 (AA-741 S271) every blog prompt carries a word band (950-1,300 since v6, a section plan since v7, never over
    1,600, never padded), a section cap and a one-pass rule (`prompts._BLOG_LENGTH_INSTRUCTIONS`), because the
    adaptive thinking of the 5.x writers is billed as output inside the same ceiling (bounded by the stage x model
    effort since CIS 381: medium for t9_write on Sonnet 5.5);
  - a judge verdict built from several calls (AA-714 near-band repeat, AA-801 median3) writes one row per
    call in one transaction (`record_calls_sync`): the verdict row (run 1's cost, `judge_calls: n`) plus
    `{judge_run, of, verdict_row: false}` rows;
  - rows for unusable / discarded / extra calls carry `billed_unusable: true` (or `verdict_row: false`)
    and none of the ok-rate keys admin LLM ops reads (`call_log.OK_RATE_KEYS`), so ok-rates do not move.
  - **Ledger v2 (AA-804, migration 319, root ADR 0009 §8):** every row also carries `app` ('cis'), `status` (ok |
    refused | error | billed_unusable | truncated, derived from the reply when the caller says nothing), and the
    attempt facts the gateway sets on `LLMResponse` and `billed_fields(resp)` passes on: `latency_ms`, `attempt_no`
    (1-based over the request's provider attempts), `connection_key`, `deployment_wire_id`, `request_id`, `effort`,
    `reasoning_tokens`, `prompt_hash` (sha256 of the prompts); plus `is_shadow`, `is_experiment` (eval label),
    `tour_id`, `units` / `unit_kind` when the caller states them. **Every refused or failed attempt with nothing
    billed gets one gateway row** (`client.LLMClient._attempt` / `log_gateway_failed_attempt`): cost 0, no tokens,
    `status` refused (provider or gateway said no: access denied, no credit, missing key, rate limit, open circuit,
    budget, data scope) or error, `error_class` (the breaker class, or budget), `gateway_attempt: true`, no ok-rate
    key. So a success is one row (the caller's) and each failed attempt before it one more at 0 USD. Jev failed /
    refused asks (breaker open, daily cap, 402, outage) and DataForSEO failed calls and budget refusals
    (`shared.dfs_call_log.status / latency_ms / error`) get rows too; Geoapify counts `geo.geocode_call
    .failed_requests` and the job's `tenant_id`, and since AA-1025 D3 (migration 346) each batch also carries `status`
    (ok | refused | error; NULL before 346 = derived: every request failed = error), `latency_ms` (the sum over its
    requests), `app` and `stage` (place_geocode / place_relocate / place_shape / adhoc_conn_test). The contract is
    read federated (root ADR 0009 decision 8 as amended in S264, DM-10.4): `llm_call_log` for LLM and Jev calls (a Jev
    row has role `decide` since D3, `validate` before), the domain ledgers for DataForSEO and Geoapify, read together
    by the Calls tab, the trace and Providers (the geocoder answers the stage / app / status filters; a Jev ask refused
    before an answer is `refused` there, `api/services/ai_gateway.JEV_STATUS_SQL`). Readers take `coalesce(app, quality_signal->>'app', 'cis')` and
    `coalesce(is_shadow, quality_signal @> '{"shadow": true}')` (no backfill); `llm_call_daily` adds
    `refused_calls`, `error_calls`, `latency_ms_sum` / `latency_ms_count` (`calls` = every row of the group).
  Exclusions: a Jev verdict-cache hit (no provider call; counted in `decision_cache_hits_daily`), a
  stream that broke before its usage event (cost unknown), and embeddings (`embed()`: the caller logs a success,
  a failed embed writes no row yet). S1 Bedrock Batch: `ingest_s1_batch` writes one row per record of the batch
  (AA-642 S7, priced at the batch rate, `pricing_mode` '<basis>+batch'; a record with no usage or no output line is a
  0 USD error row); the gateway batch lane does the same per request (AA-1076, `batch_lane.ledger_rows`), and the
  caller writes no row for a reply it took from a batch (`batch_lane.already_ledgered`). `tests/unit/test_aa927_llm_call_site_registry.py` fails on a
  new LLM call site that is not registered with the function writing its row. Stages added: `a0_column_map`, `f10_embed`, and `tp_compose` /
  `tp_search_embed` for AA-TripPlanner-Web, which reads the same tables (migration 172).
  Current choices, each decided from a real A/B run:
  - AA-1026 (migration 327, tech lead T-4 in S263): no route names the legacy `haiku` / `sonnet` keys any more; each
    moved to the Converse key of the same tier (`haiku-5-5`, `sonnet-5-5`; a CIS `sonnet` became `sonnet-5-5` →
    `haiku-5-5`, the legacy chain's degrade, except for an A-series writer, see above; `tp_*` one to one). The model names below are the history of each choice.
  - `s1_generate` (A1 admin rewrite) = Haiku 4.5; `s1_flag_fix` / `s1_itinerary_nudge` = Haiku.
  - `t2_generate` (T2 tenant rewrite, AA-620) = Sonnet — follows a tenant's brand style guide
    noticeably better, at ~11-13x Haiku's cost per call.
  - `t5_atomize` (A3 atomize — the stage key is a pre-AA-526 name) = **GPT-6 Luna as a `validate`
    stage, Haiku fallback** (AA-757, migration 213, ADR 0008): S224 offline A/B on 477 days, no
    invented detail, ~6x cheaper than Haiku. History: Sonnet → Haiku at AA-619 (same quality, ~1/4
    the cost). Atomize goes through the gateway since AA-757 (#642); before that it called
    `invoke_claude` directly.
  - Judges = OpenAI models (a deliberately different vendor from the Anthropic writer): GPT-5.6 Luna →
    GPT-6 Luna on Bedrock acc3, GPT-6 Luna on the OpenAI API only as the last resort (see Stage Route).
  - Tour writer output ceiling is `GENERATE_MAX_TOKENS=8192` (AA-639); at 4096 long tours were
    truncated and rewritten.
- **Jev decision layer (AA-660, design `docs/architecture/at-series-v2-design.md`)**: stages ask
  Jev (TypeSafe System One) typed questions through `shared/llm_client/decide.py` —
  `decide(stage, subject_key, state, question_keys, tenant_id=)`, plain HTTP, never raises.
  - Questions are rows in `shared.decision_question` (wording, `off`/`shadow`/`enforce`,
    `accept_floor`/`reject_ceiling`, `calibration_ref`). A DB CHECK refuses `enforce` without a
    calibration record.
  - Each verdict lands in a zone (accept / grey / reject / error / skipped). Only an enforce-mode,
    confident verdict changes what a stage does; grey and error keep the stage's existing rule
    (fail-open).
  - Tenant content goes to TypeSafe only for tenants in `shared.jev_tenant_allowlist` (the AA test
    tenants) until the DPA/ZDR is confirmed.
  - Every verdict is logged in `shared.decision_log`, and each call in `llm_call_log` (provider
    `typesafe`, role `decide` since AA-1025 D3, `validate` before), so Jev shows in Cost Operations (Jev tab). Admin page: `/admin/decisions`.
    Key: secret `aa-cis/dev/typesafe`. Migration 181.
  - Transport (S275): `_call_jev` keeps ONE keep-alive `httpx.AsyncClient` per event loop (the api and worker loops
    one each; a job body in its own `asyncio.run` gets its own, closed by that run's async-generator shutdown) and
    passes the connection row's timeout per request. A failed call whose text says nothing is classified by its HTTP
    status (`jev_failure_class`: 401 / 403 access_denied, 402 quota_exhausted, 429 throttled), so a refused key moves
    the row's access_state. A timed-out attempt may still be billed by TypeSafe; the ledger writes it at 0 USD.
