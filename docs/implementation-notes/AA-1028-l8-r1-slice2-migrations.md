# AA-1028 R1 slice 2 — CIS 356 and 357 (migration-only PR, MIG-4)

Lane l8-r1-slice2 of S266. The full slice notes are in `AA-1028-l8-r1-slice2.md` (code PR).

## Decisions

- **356 carries the slice-1 review follow-ups** because 353-355 are applied on Dev and never edited: the 355 writer
  guard re-run with one `has_table_privilege` call per privilege (a list of privileges is true when ANY is held);
  a non-plan platform-key candidate needs a live content subscription (an offboarded tenant kept
  `ai.budget_usd_monthly`); `shared.tenants.rate_limit_rpm` mirrored as `content.rate_limit_rpm` (source override,
  rank 0) when it differs from the live content plan, with the re-sync trigger `tenants_rate_limit_resync`; and the
  note that 353's "Order" header line wrongly says "353 (expand)" (353 is contract, as its Class paragraph says).
- **356 also ends a trial at `trial_ends_at` in the resolver (fix pass 2, re-review finding 3).** 353's `live_sub`
  took every `trialing` row, so an ended trial kept serving the plan's keys (`trade.book` included) with an
  `expires_at` in the past, and nothing ends a trial yet. Now a trialing row is live only while
  `trial_ends_at > now()` (353 requires `trial_ends_at` while trialing); the two 356 guards that test for a live
  content subscription use the same rule. Every reader of the live resolver (the tenant.entitlements.v1 snapshot, the
  357 `trade_book`) stops at the trial end, with no job. The materialised `shared.tenant_entitlement` rows and the
  `entitlement.changed` event follow at the next sync of that tenant (`shared.tenant_entitlement_sync_due()` exists
  since 353 but nothing calls it yet): the trial-end tick (move the subscription off `trialing`, the account off
  `trial`, call the sync) is slice 3 work, and the CIS saga refuses `trial_days` until then
  (422 `trial_not_supported_yet`). The CRM pull's hourly reconcile re-reads the snapshot, so the CRM follows within an
  hour without an event.
- **357 is the Booking view part of tenant.entitlements.v1 1.1** (`catalog.v_tenant_account_v1`), through the catalog
  recipe; the conftest replay skips it and `test_aa1028_migration_357` applies it the DB Migrate way.
- Both contract (no `-- migrate:` line): CREATE OR REPLACE of an existing view, a trigger on an existing table, grants.

## Changed

- `test_aa1028_migrations_353_355::test_plan_defaults_and_trial`: a business subscription on a tenant whose own RPM is
  60 now resolves `content.rate_limit_rpm` to the override 60 (356), and to the plan's 1,000 once the column says 1,000.

- `tests/integration/conftest.py`: `APP_USER_356_GRANTS`, prepended to 356 in the replay: CI's legacy `app_user` is a
  writer of `shared.tenants` there (Dev has no such role) and the strict guard refused it (the 355 guard's one-call
  check had let it through).
- `test_aa1017_migration_320_321`: its teardown drops `shared.tenant_ai_budget` CASCADE and re-applies 353 / 355; 356
  joins `LATER_ON_TENANT_AI_BUDGET` (with the app_user prelude), or every later module saw 353's resolver (the lesson
  "a new migration that restates an object a replay test rebuilds goes into that test's LATER list").

- `test_aa1028_migration_356::test_an_ended_trial_is_not_live` and
  `test_aa1028_migration_357::test_trade_book_ends_with_the_trial` (fix pass 2).

## Should know

- `account_status` stays `trial` after the trial end (in_service stays true) until the slice 3 tick; with no live
  content subscription such a tenant has no content, platform or `trade.book` key, so Booking suspends its partner
  row (`trade_book` false); a live add-on (crm, tripplanner) keeps its own plan keys, as before.

- Dev path: Migration Apply stops at the pending CIS 336 (out of order), so a person applies 356 then 357 with
  DB Migrate apply (every job table idle), then the DB Schema Dump bot PR merges.
- 357 needs `catalog_owner` to exist and the catalog schema owned by `cis_owner` (true on Dev since 245).
