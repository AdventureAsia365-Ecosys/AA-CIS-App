# AA-1026 — LLM availability matrix and stage coverage alarm (S267, lane llm-availability-matrix)

Tech lead request (S267, 04/11): list every feasible model, call each one once ($0 when refused), keep using what
answers, keep waiting on what the AWS case blocks, and make sure every stage always has a model that answers.

Three parts: (1) CIS migration 367, the acc3 candidates as disabled catalog + deployment rows and a per-deployment probe
state; (2) a candidate sweep mode of the probe job; (3) a stage coverage check (bell + e-mail per stage per day, a card
on AI gateway › Routing). Infra: the acc3 invoker role must allow the candidate profiles.

## Decisions

- **Candidates are disabled rows, not routes.** Catalog `enabled = false` and deployment `enabled = false`: the gateway
  never routes to them (`vendor_route.deployment_plan`); only the sweep (and the admin's Test connection with a model
  key) calls them. No `llm_role_config` row changes (the llm-family-fallback lane owns the chains, CIS 366).
- **Candidate list (acc3, us-west-1).** From the acc3 quota survey (applied = default > 0) crossed with
  `bedrock list-inference-profiles` (04/11): Claude Sonnet 4.5, Opus 4.5, Opus 4.6, GPT-6 Sol, GPT-6.1 Sol, GPT-6.1
  Terra (new catalog keys, `global.` profiles, the acc3 convention), plus acc3 deployments of the existing `kimi-k3`
  (`global.` profile, Global price as deployment overrides) and `llama4-maverick` (`us.` profile; no global one), at
  priority 30 behind their acc2 rows.
- **Not catalogued:** Amazon Nova 2 Pro and Nova 2 Omni: acc3 has a quota but no inference profile in us-west-1 or
  us-west-2, so there is no wire id. acc2: nothing missing (its us-west-1 profiles are Nova / Llama 4 / Kimi K3, all
  catalogued by 324; Nova Pro was left out on purpose there).
- **acc3 us-west-2 (DeepSeek V3.2, Mistral Large 3, Qwen3 235B, gpt-oss-120b): followup.** The gateway already takes a
  region per connection (`acc2-usw2`, CIS 329), but the acc3 invoker role allows named model ARNs only, and these need
  new model families (deepseek, mistral, qwen) whose rows several old migration tests would have to set aside. Per the
  lane brief, kept as a followup.
- **Prices** from the AWS Price List API (read only, 04/11/2026): Claude rows from `AmazonBedrockFoundationModels`
  (the Marketplace "Amazon Bedrock Edition" products), Global cross-region usage types in us-west-1; Kimi K3 Global
  from `AmazonBedrock`. GPT-6 Sol / 6.1 Sol / 6.1 Terra have no us-west-1 entry in either offer: unpriced, flagged in
  `price_source` and `blocked_reason` (the `enabled_needs_price` CHECK keeps them off until priced).
- **Per-deployment probe state** (`access_state`, `last_probe_at`, `last_probe_error` on `llm_model_deployment`, cis_app
  column UPDATE). A disabled candidate's probe must not count on the breaker: `CONNECTION_DENIED_MODELS = 2`, so two
  refused candidates would trip acc3's connection circuit (bell + e-mail + every acc3 deployment skipped) while Sonnet
  4.6 / Haiku 4.5 still answer. Its outcome is written to the deployment row instead.

- **Sweep = the probe job in candidate mode, not a new job kind.** `llm_connection_probe` payload `"mode":
  "candidates"` (manual only): every DISABLED text deployment (or any deployment of a switched-off connection), optional
  `connection_keys` / `model_keys`, at most `MAX_PROBES` (30), 16 output tokens each (`CANDIDATE_MAX_TOKENS`: an
  unusable or cut reply still proves the deployment answers). Same switch as a live daily run (`LLM_DAILY_PROBE`, on on
  Dev). Each call goes through the gateway's own `LLMClient.probe` (via `test_connection`), so a refused call is a 0 USD
  `shared.llm_call_log` row (ledger v2 attempt row) and an answered one its billed row with `**billed_fields(resp)`.
- **`LLMClient.probe(count=False)`** keeps the connection limiter but skips `breaker.guarded`: no health row, no
  connection circuit, no access_state write for a candidate. Daily probe and admin Test connection keep counting.
- **The deployment row gets every probe's outcome** (daily and sweep): `access_state` from the breaker class
  (credential -> unconfigured, request / other -> error), `last_probe_at`, `last_probe_error` (300 chars, ARNs /
  account IDs masked). The connection is stamped `last_probe_at` by the daily probe only.
- **Stage coverage = proof, not configuration.** `shared/llm_client/stage_coverage.py`: a stage is live when one
  deployment on its route (aliases expanded, role and data-scope rules as the gateway routes) answered within 36 h
  (`PROOF_MAX_AGE`: daily probe + margin) with no account refusal since (breaker row or probe state) and no open
  deployment / connection circuit. The connection `access_state` is only a fallback for a deployment without
  evidence of its own: `unconfigured` refuses every model (a credential failure), `quota_exhausted` refuses a
  deployment with no answer of its own within 36 h, `access_denied` one with no answer on record at all.
- **Alarm through the existing ops checks** (`shared/ops_checks.py`): one check per stage, `llm_stage_coverage.<stage>`
  (`check_for`, the stage names are data), bell at most once per 24 h per stage (`Check.bell_every`), e-mail on the
  alerts topic once per stage and UTC day. Evaluated on every api sampler tick (5 min, six small reads through
  `pool.fetch`), and summarised in the result of every live probe run.
- **No readiness raise.** Every read and write of the 367 columns falls back without them (deployments read without
  probe state; the state write is a logged no-op), so the code PR cannot break before the apply; MIG-4 order still
  applies.
- **No admin route change** (no openapi regeneration): the manual sweep is the job queued by
  `scripts/llm_ops/llm_availability_sweep.py`, run in the api container with `tools/ecs/ecsx.sh`.

## Changed

- `api/migrations/367_llm_candidate_models_probe_state.sql` (contract) + `tests/integration/test_aa1026_migration_367.py`.
- Old migration tests that re-run 304 / 306 or snapshot 324's rows: `test_aa1002_migration_304.py` (367 in `LATER`,
  its rows left out of the 304 comparisons, disabled candidates skipped in the first-path check),
  `test_aa1002_migration_306.py` (a 306 re-run would give 367's Claude candidates an Anthropic API deployment; it is
  removed again and they are left out of 306's expected seed), `test_aa1017_migration_324.py` (acc3 rows of kimi-k3 /
  llama4-maverick are 367's).

- Code PR: `services/jobs/llm_probe_job.py` (candidate mode, `build_candidate_plan`, `record_deployment_states`,
  `matrix`, `coverage_summary`, `enqueue_manual(mode=...)`), `shared/llm_client/client.py` (`probe(count=)`,
  `_call_target(count=)`), `api/services/llm_connections.py` (`test_connection(count_outcome=)`),
  `shared/llm_client/stage_coverage.py` (new), `shared/ops_checks.py` (`check_for`, `Check.bell_every`,
  `coverage_findings`, `stage_coverage_findings`), `api/core/ops_metrics_sampler.py` (the ops checks part),
  `api/services/llm_availability.py` (`stage_coverage` in `GET /admin/llm-availability`), frontend
  `StageCoverageCard.tsx` on the Routing tab + `lib/llmAvailability.ts` helpers + messages (EN / VI),
  `scripts/llm_ops/llm_availability_sweep.py` (force-added: `scripts/` is ignored).
- Tests: `tests/unit/test_aa1026_availability_sweep.py`, `tests/unit/test_aa1026_stage_coverage.py`,
  `tests/integration/test_aa1026_stage_coverage_db.py`, `frontend/lib/llmAvailability.test.mjs`; read-count updates in
  `test_aa900_llm_availability.py` / `test_aa1002_s4_admin_connections.py`; `stage_coverage.py` classified in
  `test_aa1002_no_behaviour_change.py`.

## Tradeoffs

- Disabled acc3 deployments of kimi-k3 / llama4-maverick without touching their catalog `bedrock_profile_ids` (acc2
  only): enabling one later needs the catalog's acc3 profile too (wire drift test), but 367 changes no existing row.

- One alert per uncovered stage (as asked), not one digest: on a bad day several stages bell and mail at once. The
  card shows them on one page.
- 36 h proof window: a model that answered yesterday but is refused now is caught by the breaker row of the refusal
  (newer than the answer), not by the window.

## Should know

- MIG-4: migration-only PR → Infra byte copy → Infra Migration Apply (contract, jobs idle) → schema dump PR → code PR.
- The acc3 IAM statement (AA-CIS-Infra `accounts/acc3-bedrock/main.tf`) must be applied (human, MFA) before the live
  sweep; otherwise every candidate answers AccessDenied from IAM, which says nothing about the account's model access.
- Cost of a live sweep of the eight 367 candidates on acc3: about 0.0068 USD upper bound (60 tokens in, 16 out;
  the three unpriced GPT-6 rows estimated at the GPT-6 Astra Global price 10 / 50 USD per 1M; a refused call is 0 USD).
- The llm-family-fallback lane edits the same CALL_PATH tuple in `test_aa1002_no_behaviour_change.py`
  (route_chains.py): a one-line merge on rebase.
- Migration number: 367 assumes the llm-family-fallback lane's 366 merges first (the preflight refuses 367 while main
  max is 365). If 366 is dropped, renumber to 366 (`git grep '367_'` and `'367'`, `migration-367-aa1026` stays a
  label only if renamed consistently).
- Stacked branches: the code branch carries the migration commit; after the migration-only PR squash-merges, rebase
  the code branch on main (the identical change drops out).

## Review fix (S267)

- **Connection `quota_exhausted` no longer overrides a deployment's own answer.** Reviewer finding: acc3 sets quotas
  per model (Claude 5.x, GPT-5.6 and others at 0, Sonnet 4.6 / Haiku 4.5 / Cohere answering). The daily probe's refusals
  on the 0-quota models trip the connection circuit, and `breaker.note_access_state` stamps the connection
  `quota_exhausted` (no timestamp, once per change and process, so an `ok` from another process may never overwrite
  it). The gateway does not route on that column, so it kept calling Sonnet 4.6 / Haiku 4.5 while the card said NO LIVE
  MODEL and the alarm fired. Now: `unconfigured` stays account-wide; `quota_exhausted` refuses only a deployment with
  no answer of its own within `PROOF_MAX_AGE`; a deployment's own newer refusal still wins over its answer (unchanged).
- Tradeoff: a real account-wide quota cut shows a deployment as live until its own call or the next probe refuses it
  (at most one probe cycle), instead of a false alarm on every acc3 stage every morning. The connection circuit (OPEN
  for minutes after the trip) still marks every model `circuit_open` meanwhile.

