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

## Changed

- `api/migrations/367_llm_candidate_models_probe_state.sql` (contract) + `tests/integration/test_aa1026_migration_367.py`.
- Old migration tests that re-run 304 / 306 or snapshot 324's rows: `test_aa1002_migration_304.py` (367 in `LATER`,
  its rows left out of the 304 comparisons, disabled candidates skipped in the first-path check),
  `test_aa1002_migration_306.py` (a 306 re-run would give 367's Claude candidates an Anthropic API deployment; it is
  removed again and they are left out of 306's expected seed), `test_aa1017_migration_324.py` (acc3 rows of kimi-k3 /
  llama4-maverick are 367's).

## Tradeoffs

- Disabled acc3 deployments of kimi-k3 / llama4-maverick without touching their catalog `bedrock_profile_ids` (acc2
  only): enabling one later needs the catalog's acc3 profile too (wire drift test), but 367 changes no existing row.

## Should know

- MIG-4: migration-only PR → Infra byte copy → Infra Migration Apply (contract, jobs idle) → schema dump PR → code PR.
- The acc3 IAM statement (AA-CIS-Infra `accounts/acc3-bedrock/main.tf`) must be applied (human, MFA) before the live
  sweep; otherwise every candidate answers AccessDenied from IAM, which says nothing about the account's model access.
