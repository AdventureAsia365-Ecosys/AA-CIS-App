# AA-1028 R1 slice 1 — tenant control plane: CIS 353-355 and the producer of `tenant.entitlements.v1`

Lane l1-r1-control-plane of S266. Root ADR 0019 d1, d2, d12, d16, b2-b4, b19; design: root
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §3.2-3.5 and `clarifications.md` (TL-1, TL-9, BO-7,
BO-19). Two PRs: the migration-only PR (353, 354, 355; MIG-4) and the code PR (`/tenant/v1`, below).
Not in slice 1: the `tenant_provision` saga, the CRM / Booking pulls, the admin UI, the removal of `PLAN_LIMITS` /
10/10/50 / `PLAN_OPTIONS`, the retirement of `shared.tenant_ai_budget`, the Dev guard (slices 2-3).

## Decisions

- **Three files, all contract.** 353 holds only new objects, but the expand lint refuses what the file needs
  (`REVOKE ALL` against the Dev default ACL of `cis_owner` in `shared`, `CREATE OR REPLACE` / `DROP TRIGGER IF EXISTS`
  for a no-op re-run, DO guards). 354 alters `shared.tenants`, 355 inserts policy rows and a trigger on
  `shared.tenant_ai_budget`, so the batch is one migration-only PR anyway; 353 being contract costs nothing.
- **A feature catalogue table (`shared.entitlement_feature`).** Not in the README §3.4 list, added so a feature key
  is an FK (a typo can never silently disable a feature) and each key has a `value_kind` (flag / limit / seats /
  policy), a unit and a product. It replaces README's `shared.product` seed table: product is a CHECK vocabulary
  (`content | crm | tripplanner`, plus `platform` for keys every plan may carry: `ai.budget_usd_monthly`,
  `policy.notice_days`).
- **Resolver = a live view; the table is the published state.** `shared.v_tenant_entitlement_resolved` computes
  override (unexpired) > `shared.tenant_ai_budget` (as the `ai.budget_usd_monthly` override, NULL = unlimited, its
  own meaning) > the plan of a live subscription (`trial` source while trialing). `shared.tenant_entitlement` (ADR
  d2's materialised table) is written only by `shared.tenant_entitlement_sync(tenant)`, which diffs the two and emits
  one `entitlement.changed` per changed key. Triggers on every input call the sync, so the table follows at once;
  only a time-based expiry (an override's `expires_at`) waits for `shared.tenant_entitlement_sync_due()`, which the
  daily tick of slice 2 calls. The contract view `shared.v_tenant_entitlement_v1` and the HTTP route read the live
  resolver, so they are right even before that sweep.
- **Override scope.** An override of a crm / tripplanner key applies only while the tenant has a live subscription of
  that product (cancelling the add-on removes its overrides' effect); platform keys always apply. `enabled = false`
  takes a plan feature away.
- **Seats.** A `seats` key takes the subscription's `seats` when set, else the plan's value (crm 10 = today's
  `crm.tenant_entitlement.member_limit` default).
- **Lifecycle on `shared.tenants`: `account_status` leads, the legacy columns follow.** A BEFORE trigger keeps
  `is_active` / `cancelled_at` in step both ways, so `api/routers/admin.py` (PATCH is_active, offboard, restore) works
  unchanged and a new writer of `account_status` alone also locks / unlocks. Live = trial, active, past_due
  (`past_due` keeps service, README §3.2). Existing rows: offboarded -> `offboarding`, inactive -> `suspended`.
- **The 276 membership trigger is re-created with `account_status` in its column list.** A column-list trigger fires
  only for columns the UPDATE names, not for those a BEFORE trigger changes; without this a status-only write would
  never reach the CRM's membership feed. Same WHEN, same function.
- **Pilot marker = columns** `is_pilot` + `pilot_ends_at` (d16 left "feature key or column" to this migration). A
  column is testable in SQL by the Dev guard and the clean-up / wave jobs; a unique partial index allows one pilot,
  a CHECK makes it `kind = 'agency'`. The 8-week cap is policy for the Dev guard (later slice), not a CHECK.
- **`kind` next to `is_canary`, not replacing it.** `tenant.membership.v1` 1.2 serves `is_synthetic` from
  `is_canary`; changing that is a contract change for the CRM. Backfill: the AA tenant / plan_tier internal ->
  `internal`, `is_canary` -> `demo`, else `agency`.
- **Bill-to fields tagged as personal data** (DM-6.1): `legal_entity_name` pii:name, `tax_id` pii:government_id,
  `billing_email` pii:contact, `billing_address` pii:address (a sole trader's are a person's). Never served by
  `tenant.entitlements.v1` (Booking reads them later under its own scope, R4).
- **Starting policy values** (all editable without code): content 299 / 799 / 1,499 USD, Enterprise and internal
  unpriced; AI included 15 / 40 / 80, Enterprise 80 (Business floor until the contract override), internal
  unlimited; content counts carry today's `membership_plans` (50 / 200 / 500 rewrites, 5k / 20k / 100k API calls)
  and `PLAN_LIMITS` rpm (60 / 300 / 1,000; internal 60); `policy.notice_days` 7 (BO-7). CRM and TripPlanner plans
  exist with **no price** (b3 / b4 give the model, not the number); `tp.ai_sessions` has no limit (the AI budget and
  the AA-1015 limiter cap it); `tp.branding` is in no plan (BO-8: off by default).
- **No FK to `shared.membership_plans`.** That table was made by hand on Dev and the CI replay of 223 never builds it;
  `plan.membership_plan_id` is a soft link filled by 355 where the table exists.
- **A bridge trigger keeps the content subscription in step with `shared.tenants` (355 §5b, review fix).** The admin
  writes (create, PATCH plan_tier, offboard, restore in `api/routers/admin.py`) know nothing of
  `shared.tenant_subscription`; without the bridge a tenant created after apply had no entitlements, a tier change kept
  the old plan's limits, and an offboarded tenant kept an active subscription. `shared.tenants_content_subscription_sync()`
  (AFTER INSERT; AFTER UPDATE when plan_tier or account_status changes) creates the content row on the live plan of
  the tier (`shared.plan.tier`, not a `'content.' || tier` string), moves it on a tier change (no live plan for the
  tier refuses the write), cancels the add-ons then the content row on offboarding / closed, and makes a canceled
  content row active again on a restore (add-ons stay canceled: they are sold again). Lock / unlock, past_due and
  trial leave the subscription alone: the contract carries `account_status` / `in_service` next to the entitlements.
  A trigger rather than code in the four handlers: it also covers scripts, E2E fixtures and future writers, and the
  slice-2 saga replaces it (then the trigger is dropped by a forward migration).
- **Writer guards count real grantees only (354 / 355 guard 2, review fix).** A writer of `shared.tenants` is its
  owner, a grantee of INSERT / UPDATE on the table or a column, or a member of one. `has_table_privilege()` over every
  role also reported `rds_superuser` (pg_write_all_data, not a member of `cis_owner`), which would have failed 354 on
  Dev only (the CI replay has no `rds_*` role). The RDS master is a member of `cis_owner` and is checked through it.
- **`tenant_entitlement_resync_plan()` branches per table (353, review fix).** PL/pgSQL resolves `OLD.plan_key`
  against the firing table's row type at run time, so the shared query raised on an `entitlement_feature` update
  (retiring a key). The entitlement_feature branch also re-syncs every tenant holding a materialised row of the key.
- **Feed rows written only through `shared.tenant_account_emit()`** (a transaction-local flag checked by the guard)
  rather than 276's `pg_trigger_depth() >= 2`, because the sync function can also be called directly (sweep).
  `actor` comes from the transaction setting `aa.actor` (code sets it in slice 2; 355 sets `migration:355`).

## Changed (vs. the issue / design text)

- README §3.4's `shared.product` seed table -> a CHECK vocabulary plus `shared.entitlement_feature`.
- `shared.tenant_entitlement.source` keeps `plan | trial | override`; the legacy AI budget row is reported as
  `override`.
- No crm / tripplanner subscription is backfilled: CIS never reads `crm.*`; the CRM projection pull (slice 2) starts
  from the CRM's own agencies.

## Tradeoffs

- Triggers doing the materialisation keep every writer (admin routes, the AI budget PUT, a migration) consistent with
  no code, at the cost of logic in plpgsql. The alternative, a job, would leave the table stale between ticks.
- One CIS-wide event feed (account, subscription, entitlement) instead of one per kind: consumers re-read the
  snapshot on any event; the `change` body is small and typed.
- Trigger-based sync per row means a plan_feature edit re-syncs every tenant on that plan in the same transaction
  (fine at tens of tenants; revisit with a job when a plan holds thousands).

## Should know

- **Order:** root PR (retention rows + contract) -> this migration PR -> bot copy to Infra -> **a person applies 353,
  then 354, then 355 with DB Migrate `apply`** (every job table idle, DM-11.4) -> DB Schema Dump bot PR merged ->
  Infra PR (RLS rules byte copy + `/tenant/v1` gateway route) -> the code PR. Nothing reads the objects before the
  code PR.
- **Why not Infra Migration Apply:** CIS 336 is copied to Infra but not registered and sits below the head (352), so
  the cis owner's auto plan stops there (out-of-order, `plan_pending` in `db/run_migration.py`) and nothing above it
  runs; contract auto-apply is also off until 336 is applied (AA-1024 D8, from 10/11 ~12:00Z). The manual DB Migrate
  `apply` runs the named file with no ordering check, so 353-355 can go first; 336 stays a manual out-of-order apply
  afterwards, as it already is.
- **Writers of `shared.tenants` need INSERT on `shared.tenant_account_event` and EXECUTE on
  `shared.tenant_account_emit()`** (354 guard 2). CI's legacy `app_user` gets EXECUTE in `tests/integration/conftest.py`
  (post-replay block), and the 276 test's `_as_runtime` re-grants cis_app; any new test that writes tenants as
  another role must do the same.
- `test_aa839_erasure_job::test_a_restore_after_the_first_checks...` disables the 354 account trigger next to the 276
  feed trigger for its one restore statement (a restore writes an event, which changes the DRY RUN counts).
- The erasure inventory has the four new tenant tables; the RLS catalogue allowlist has them too (rule a, cis_app
  only) and must be byte-copied to AA-CIS-Infra `db/rls_catalogue_rules.py` after this PR merges.
- On apply, every existing tenant gets one `subscription.changed` and about 11-15 `entitlement.changed` rows.
- Since 355 a tenant INSERT also writes a content subscription and its entitlements as the writer: a test role that
  writes `shared.tenants` needs the 353 cis_app grants on `tenant_subscription` / `tenant_entitlement` and EXECUTE on
  `tenant_entitlement_sync` / `tenant_account_closed` (conftest gives them to `app_user`; the 276 test re-applies 353).
  `test_aa839_erasure_job`'s restore statement also pauses `tenants_content_subscription_upd`.
- Tests that set a tenant's content plan use an upsert (`_subscribe` in `test_aa1028_migrations_353_355.py`): the
  row already exists from the create.

## Code PR: the producer (`/tenant/v1`)

### Decisions

- **Scope `cis.tenant/read`, the CRM client, no new scope in 1.0.** The design suggested a new scope (for example
  `cis.tenant/entitlements.read`). The CRM, the first consumer, already holds `cis.tenant/read` for the membership
  feed of the same agencies, and a new scope is an Infra `identity_m2m.tf` change plus a CRM secret update for no
  added isolation. The Booking client (and, if wanted, its own scope) comes with `bk_tenant_pull`; accepting a second
  scope later is non-breaking.
- **Same shape as `tenant.membership.v1`**: `api/core/m2m_auth.require_m2m`, the main pool in one READ ONLY
  transaction, ETag over the body without `as_of`, the feed's `(txid, seq)` order below the snapshot horizon and its
  cursor helpers (imported from `services/tenant_membership/feed.py`, not copied).
- **`in_service` in the body** so no consumer re-derives the live status set (trial, active, past_due).
- **`limit` as a decimal string** (contracts rule 8: money is a decimal string; `ai.budget_usd_monthly` is USD).
- **The snapshot reads the live contract view**, not the materialised table, so an expired override or trial is
  right before the sweep; the feed reads the events the materialisation wrote.
- **No bill-to fields in 1.0** (data minimisation, the contract policy forbids them). Booking's invoice snapshot reads
  them under its own scope in R4.
- **No readiness raise.** The code answers 503 `unavailable` while 353-355 are missing (UndefinedTable / Column /
  Function), like the membership routes; `REQUIRED_CIS_SCHEMA` stays where it is. Merge it only after 355 is applied
  by hand (DB Migrate, see Order above) and the DB Schema Dump bot PR has merged (MIG-4).

### Should know

- Live verification needs the AA-CIS-Infra route `/tenant/v1/{tenant_v1_proxy+}` (the gateway routes only
  `/tenant/membership/*` today). Before it is applied, verify inside the api task (ECS exec, `curl localhost:8000`).
- `docs/api/openapi.json` was regenerated with fastapi 0.161.1 / pydantic 2.14.1 (the `requirements.txt` pins).
- `tests/unit/test_s241_admin_routes_require_auth.py` lists `tenant_account.require_account_reader` as an M2M
  dependency.
