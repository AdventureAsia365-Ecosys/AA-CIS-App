# AA-1027 — lane l2-tp-gw-e3 (S264): migration 341 for gateway plan slice E3

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.E row E3. The consumer is AA-TripPlanner-Web
`backend/shared/llm_gateway.py` (dispatch by deployment, breaker per connection); this repo only widens the contract
view it reads.

## Decisions
- Append two columns to `catalog.v_llm_gateway_config_v1` (contract v1: append only): `route_deadline_s` (the E1
  deadline TripPlanner already reads when present) and `deployment_routing` (JSON array aligned with `deployments`).
  Keys inside `deployments` are left as they are, so every existing reader sees the same values.
- `deployment_routing` carries only non-secret connection facts: kind, auth_mode, account_id_env (an env var NAME;
  the CHECK allows only `BEDROCK_ACCT_PRIMARY`), region, data_scope, served_roles, enabled, access_state. Never
  account_id, role_name, external_id, secret_name, credential_env, base_url, notes, budgets, or `last_error` text
  (the closing guard of the file refuses a grant on those columns).
- The connection circuit is `breaker.connection_health` (AA-1018) in SQL, over every health row of the connection
  (other models' rows are read to derive it, their keys are not exposed). The view states no "now": it gives the
  timestamps and TripPlanner compares them with its clock, so its 60 s route cache never freezes a state.
- Class contract: the file grants columns of three existing tables to `catalog_owner` and replaces an existing view.

## Changed
- `api/migrations/341_llm_gateway_view_routing_health.sql` (new), `tests/integration/test_aa1027_migration_341.py`
  (new), `tests/integration/conftest.py` (the replay skips 341, like 323).
- Not changed: `shared/aa_llm/contract.py` (`GATEWAY_CONFIG_COLUMNS` stays the 323 list; TripPlanner appends the two
  names in its own SELECT). Appending them to the core is a follow-up for the next core change, then a sync.

## Tradeoffs
- Deriving the connection circuit in SQL duplicates the Python rule; the integration test pins the four cases
  (2 models denied trips, 1 does not, quota trips alone, a later answer clears). A stored `"*"` row is not possible
  (`llm_provider_health.model_key` references the catalog).

## Should know
- 323 cannot be re-run on a database where 341 is applied (CREATE OR REPLACE VIEW cannot drop columns); DB Migrate
  never re-runs a registered version, and the 323 integration test undoes everything first.
- Number (review fix pass): 339 / 340 are held by the S264 lane l1 migration-only PR (branch
  `feat/s264-l1-migrations-339-340`). On today's main (max 338) the CI migration preflight refuses 341 ("339 = main
  max + 1"), so this PR is queued only after that PR merges. Checked locally: on main + those two commits the preflight
  passes (341, class contract, lint ok) and `test_aa1027_migration_341` + `test_aa642_migration_323` pass on local
  Postgres. Fallback if that PR does not land first: rebase on origin/main, `git mv` the file and the test to main
  max + 1, replace '341' in the file, the test, conftest and these notes (every '341' there is the migration number),
  re-run the preflight. Rehearsed with 339 on today's main: preflight green, the two integration tests pass. Never the
  `migration:out-of-order` label (a labelled file is never auto-applied).
