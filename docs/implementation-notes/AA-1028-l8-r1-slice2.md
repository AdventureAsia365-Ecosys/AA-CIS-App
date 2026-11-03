# AA-1028 R1 slice 2 (AA-CIS-App) — the tenant_provision saga, CIS 356 / 357, read-only CRM features

Lane l8-r1-slice2 of S266. Root ADR 0019 d1 / d2 / d9; design: root `docs/audits/2026-11-03-S265-ecosystem-restructure/
README.md` §3.4-3.6; slice 1: `AA-1028-l1-r1-control-plane.md`. Three CIS PRs in this slice: the migration-only PR
(356 + 357, MIG-4) and this code PR (on top of the slice-1 producer PR #1276).

## Decisions

- **The saga is one database transaction plus the job.** Steps: create (or find, attach mode) the tenant, the
  add-ons, the audit row naming the request key. With every step in one transaction the saga needs no
  compensation; idempotency comes from the job key (`tenant_provision:<request_key>`) and, for a retry after a commit
  (the worker died before marking the job), from the audit row (`details.provision_request_key`). External steps
  (CRM agency, owner invite) can be added as later steps with their own markers.
- **No CRM agency creation in the saga.** The CIS relay to crm.ops.v1 uses the signed-in staff's own WORKFORCE token
  (`api/services/crm_console.py`); a worker has none, and crm.ops.v1 has no M2M create route. The CRM projection
  (`crm_entitlement_pull`) makes the agency's plan and features follow the subscription the saga writes.
- **No API key in the saga.** A key is shown once; it must never sit in `shared.job` (payload or result). The tenant
  is created with `api_key_hash` NULL; staff issue the key with `POST /admin/tenants/{id}/generate-key`.
- **The 355 bridge triggers stay.** The task allowed dropping `tenants_content_subscription_ins` / `_upd` only if the
  admin create path moved to the saga; it did not (`POST /admin/tenants` returns the key synchronously, and PATCH /
  offboard / restore still write only `shared.tenants`). The saga works with the bridge: its INSERT gets the content
  row (active) from the trigger.
- **No trial in this slice (fix pass 2, re-review finding 3).** A trial the saga made would never end: nothing moves a
  `trialing` subscription or a `trial` account on at `trial_ends_at`. `parse()` refuses `trial_days` (any value,
  422 `trial_not_supported_yet`; the API field stays so a client that sends it gets the refusal instead of a silent
  active tenant), and the trial code path (`TRIAL_SQL`, account `trial`) is removed. **Slice 3**: the trial-end tick
  (a `shared.job` kind that, at `trial_ends_at`, moves the subscription off `trialing` and the account off `trial`,
  then calls `shared.tenant_entitlement_sync`) and the saga's trial option with it. As a guard against a late tick,
  CIS 356 (migration PR) now treats a trialing subscription past `trial_ends_at` as not live in the resolver.
- **Tiers starter / growth / business only.** Enterprise needs a contract override (no rate-limit value on the plan);
  internal is AA itself. The rate limit comes from the plan's `content.rate_limit_rpm` (the same numbers as
  `PLAN_LIMITS`), `plan_id` from `shared.plan.membership_plan_id` (a warning, not a failure, when it is NULL).
- **Add-on upsert never downgrades a live status** (a trialing add-on stays trialing); a canceled / paused one is active
  again; an unchanged add-on writes nothing (no event).
- **Platform › Agencies features read-only once CIS manages them** (fix pass 2, re-review finding 2): crm.ops.v1 1.11
  keeps `PUT /entitlements-allowed` for an agency that is still `managed_by` ops (never projected: no CIS tenant, such
  as the CRM-seeded E2E agencies, or held by the CRM adoption guard) and answers 409 only once the CRM pull projected
  it. The drawer shows the editor while `managed_by` is not `cis` (a 1.10 server sends none) and the read-only note
  once it is.

### CIS 356 / 357 (migration PR)

- **Reviewer follow-ups go into a new file**: 353-355 are applied on Dev and are never edited (an edit to an applied
  copy is a silent no-op, Infra Copy Guard). So the per-privilege guard, the platform-key rule, the RPM mirror and the
  "353 (expand)" wording note live in 356.
- **Platform keys need a live content subscription** (resolver fix) rather than a reader gate on `account_status`: the
  view, the materialised table and the HTTP snapshot then agree, and a suspended tenant keeps its keys (in_service
  false tells consumers to refuse), while an offboarded one loses them.
- **RPM mirrored, column authoritative**: the limiter keeps reading `shared.tenants.rate_limit_rpm`; the entitlement
  publishes what it enforces (rank 0, above an explicit override, which would publish a limit nobody enforces).
- **Booking reads a catalog view, not HTTP** (357): no Cognito client, scope, secret or Infra change; the established
  catalog recipe (catalog_owner, security_barrier, column grants on `shared.tenants`, SELECT on the resolver).

## Tradeoffs

- One transaction for the whole saga: simple and atomic, but every external step added later needs its own marker.
- Mirroring RPM in the resolver adds a candidate per tenant to every resolution (tens of tenants: negligible).

## Should know

- Order: root PR -> CIS migration PR (356 + 357) -> Infra copy -> **DB Migrate apply cis/356 then cis/357** (Migration
  Apply stops at the pending 336) -> DB Schema Dump bot PR -> the producer PR #1276 -> this code PR. The code does not
  need 356 / 357 (the saga writes 353-355 tables; the AgencyDrawer change needs crm.ops.v1 1.11 only).
- `docs/contracts/tenant.entitlements.v1.openapi.yaml` is the 1.1 byte copy; the pin in
  `tests/unit/test_aa1028_tenant_account_contract.py` changed with it (fix pass 2: re-pinned to 4ca09149..., the
  root file now says a trialing subscription is live only until `trial_ends_at`).
- The saga's routes are in `tests/unit/snapshots/aa882_staff_route_policy.json` and the router sample list.
- On the CI replay `shared.tenants.plan_id` does not exist (made by hand on Dev); the saga's integration test adds it in
  its rolled-back transaction, as test_aa797 does.
- Review fix (S266): the branch was stacked on the pre-squash producer commits; it is rebased onto `origin/main` after
  #1276 (`git rebase --onto origin/main c86bd8b9`), so the diff is the saga, the AgencyDrawer change (on the moved
  path `frontend/app/admin/tenants/agencies/`), the contract 1.1 copy (byte-identical to the root file) and the re-pin.
- Revocation: this slice has no path that removes a CRM feature or cancels an add-on once CIS manages the agency
  (the saga only adds or changes add-ons, the overrides table has no admin endpoint). Slice 3 adds it (a saga detach
  / cancel mode or an audited admin endpoint for `shared.tenant_entitlement_override` with `enabled = false`); until
  then a partial revoke is a saga attach of a crm plan without the feature, or a lock of the agency.
- Deploy order across repos: this CIS saga PR deploys **before** the AA-Partner code PR (crm_entitlement_pull), so
  that once an agency is projected the CIS path to change its features already exists.
- Existing CRM agencies: the CRM pull keeps an agency's L1 features until CIS holds a crm subscription for it (adoption
  guard, AA-Partner). After this PR deploys, attach the crm add-on to each CRM agency that is a CIS tenant with
  `POST /admin/tenants/{id}/provision` (crm.core, seats = its member limit) so the projection takes over. The live
  projection check uses a CRM agency that IS a CIS tenant (Atlas & Hearth 9149d3c6-fcaf-4400-8e7a-4ed5e557e146), not
  'Lữ hành Sen Vàng' (bab16039-..., CRM-seeded, no CIS tenant: the saga answers tenant_not_found, and it stays
  `managed_by` ops, edited on Platform › Agencies).
