# Tenant control plane, lifecycle and DB role

> Detail page of [CONTEXT.md](../../CONTEXT.md): tenants, plans and entitlements, lifecycle and the tenant DB role. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

### Tenant control plane (AA-1028 R1, root ADR 0019 d1 / d2)

CIS `shared` is the master of the tenant account, lifecycle, plans, subscriptions and entitlements; `crm.tenant` and
`booking.b2b_partners` become read-only projections (their pull jobs, R1 slices 2-3). Storage (CIS 353-355):

- `shared.tenants.account_status` (lead, applied, trial, active, past_due, suspended, offboarding, closed) leads the
  lifecycle; the trigger `tenants_account_status_sync` keeps the legacy `is_active` / `cancelled_at` in step both ways,
  so the AA-797 routes below keep working (PATCH is_active false -> suspended, offboard -> offboarding, restore ->
  active). In service = trial, active, past_due. Also `suspend_reason`, `data_region` (us), `kind` (agency, demo,
  internal; `is_canary` stays the CRM's `is_synthetic`), `time_zone`, the bill-to fields (DM-6.1 tagged, never served
  to other apps), `segment`, and the one Dev pilot marker `is_pilot` + `pilot_ends_at` (ADR 0019 d16).
- `shared.entitlement_feature` (the feature keys, a value kind, unit and product), `shared.plan` (content, crm per
  seat, tripplanner per site; starting prices are policy data, NULL = not priced), `shared.plan_feature`,
  `shared.tenant_subscription` (one per tenant and product, Stripe status vocabulary; an add-on needs a live content
  row), `shared.tenant_entitlement_override` (per-tenant exceptions with a reason, optional expiry, `enabled = false`
  takes a feature away).
- Until the slice-2 saga writes subscriptions, the bridge trigger `tenants_content_subscription_ins` / `_upd` (CIS 355)
  keeps the content subscription in step with `shared.tenants`: created at tenant create on the live plan of
  `plan_tier` (`shared.plan.tier`), moved on a tier change, canceled with the add-ons on offboarding / closed, active
  again on restore. The admin routes write no subscription themselves.
- The resolver `shared.v_tenant_entitlement_resolved` (live): override > `shared.tenant_ai_budget` (as the
  `ai.budget_usd_monthly` override; the table is not retired yet) > the plan of a live subscription.
  `shared.tenant_entitlement` is its materialisation, written only by `shared.tenant_entitlement_sync(tenant)`, which
  triggers on every input call; `shared.tenant_entitlement_sync_due()` re-syncs expired overrides (called by the hourly
  `tenant_account_tick`, slice 3). `shared.v_tenant_entitlement_v1` is the contract view.
- `shared.tenant_account_event`: the append-only feed, written only through `shared.tenant_account_emit()` (the
  writer's label from the transaction setting `aa.actor`, never served). A new writer of `shared.tenants`,
  `tenant_subscription`, `tenant_entitlement_override`, `plan_feature` or `tenant_ai_budget` needs the CIS 353 grants
  (the triggers run as the writer, MIG-6).
- `shared.tenant_addon_request` (CIS 374, AA-1036 R3, ADR 0019 d7): the add-on requests an agency sends from the agency
  app ("Request add-on": `POST /v1/account/add-on-requests`, Content studio owner only; `GET /v1/account/add-ons` is the
  app's active / requested / available list) and the staff decision in Tenant 360 › Subscription
  (`api/routers/admin_tenant_addon_requests.py`; admin, or the tenant's account manager for its own book:
  `tenant.book.write` + `require_tenant_scope`, clarifications TL-7 step 4): an approval (refused 409
  `tenant_not_in_service` unless the account is in service: status, `is_active`, `cancelled_at`; 409
  `already_subscribed` when the add-on is live by now) attaches the plan through the `tenant_provision` saga (attach
  mode with `only_new`: a live add-on is never changed, its plan and seats stay `tenant.write` / `billing.write`;
  request key `addon-request:<id>`, enqueued in the same transaction) and then shows its `setup` from the job
  (in_progress / done / failed), a decline carries a reason the agency sees. Every attach saga closes the pending
  requests of the products it made live (approved by the saga actor). "View as tenant" reads
  `GET /v1/account/add-ons` (admin and customer_success only, `ROUTE_CAPS`; never the POST). At most one pending request per tenant and product; audited
  (`tenant.addon_requested`, `tenant.addon_request_approved` / `_declined`); a new request rings the staff bell
  (`tenant.addon.requested`, admin + customer_success). Rules: `services/tenant_account/addon_requests.py`.
  AA-1115 (ADR 0019 d10): on the studio list (not the CRM relay `tenant.addons.v1`), an ACTIVE tripplanner entry
  carries `planner` {url, domains, launcher}: the hosted `<TripPlanner origin>/<slug>/plan` (origin from
  `TRIPPLANNER_REVALIDATE_URL`, else the Dev host; only when in service and `tp.hosted` resolves, else null), the ACTIVE
  `shared.tenant_tripplanner_domain` hosts, and the `tp.launcher` flag. The Content studio's Add-ons page (AA-Partner)
  shows the launcher snippet from it. Code: `services/tenant_account/planner_site.py`.

- CIS 356 (slice 2, review fixes): a non-plan candidate of a platform key (an override, the legacy AI budget) needs a
  live **content** subscription (an offboarded tenant keeps no `ai.budget_usd_monthly`); `shared.tenants.rate_limit_rpm`
  is mirrored as `content.rate_limit_rpm` (source override, rank above an explicit override) when it differs from the
  live content plan's value, and the trigger `tenants_rate_limit_resync` re-syncs on an RPM change. The column stays
  the limiter's source (`api/middleware/rate_limit.py`) until slice 3 moves the limiter onto the entitlement.
- CIS 357 (slice 2): `catalog.v_tenant_account_v1` (catalog recipe, SELECT to `booking` only; contract
  tenant.entitlements.v1 1.1): one row per tenant (id, name, account_status, suspend_reason, in_service, kind,
  is_pilot, trade_book) for Booking's `bk_tenant_pull`, which projects every tenant into `booking.b2b_partners`.
- **One-action provisioning (slice 2):** the `tenant_provision` saga on `shared.job`
  (`services/tenant_account/provision.py`, `services/jobs/tenant_provision_job.py`, routes in
  `api/routers/admin_tenant_provision.py`, admin role, `tenant.write`): `POST /admin/tenants/provision` creates a tenant
  (the same rows as `POST /admin/tenants`; no tenant API key, AA-1036) with its content subscription
  (the 355 bridge, active, or trialing with `trial_days` since slice 3) and optional crm / tripplanner add-ons;
  `POST /admin/tenants/{id}/provision` adds or changes add-ons of an existing tenant; `GET
  /admin/tenants/provision/{job_id}` reads the job. Idempotent per `request_key` (job key `tenant_provision:<key>`; the
  database steps are one transaction that writes the audit row naming the key, so a retry after a commit returns the
  stored outcome). The feed events carry `aa.actor` = `job:tenant_provision:<job id>`. The CRM (`crm_entitlement_pull`,
  plan / seats / allowed features / CIS lock) and Booking (`bk_tenant_pull`) projections follow by themselves. AA-1116
  (S282 G5 / G11): a create or attach with a `crm` add-on also creates the CRM agency and, with `crm_owner_email`,
  sends its first-owner invitation in the same staff request (`services/tenant_account/crm_onboarding.py`): the CRM
  takes those writes only with the staff member's own WORKFORCE token, which the job does not have, so the route runs
  the saga's idempotent transaction itself under the job's id (the job then returns it `resumed`) and relays
  `GET /tenants/{id}`, `POST /tenants`, `POST /tenants/{id}/owner-invite` (already there = done), nudges the account
  feed with one unchanged `account.status_changed` event when it created the agency, and audits `tenant.crm_onboarded`
  (never the address). The response carries `crm_onboarding` {status done / partial / failed / skipped, steps, error};
  `POST /admin/tenants/{tenant_id}/crm-onboarding` (admin, `tenant.write`) runs it again. A reused `request_key` whose
  first job or stored outcome names another tenant (or slug) never reaches the CRM (`request_key_reused`). The New
  tenant dialog of Platform › Tenants runs this saga with its add-on choices and the owner address. Platform › Agencies keeps
  its buttons for the cases the relay refuses (no `crm-platform-admin` group, CRM down). Platform › Agencies shows the CRM allowed features read-only once the CRM
  projected the agency from CIS (crm.ops.v1 1.11 `managed_by` cis) and still edits them while it is `managed_by` ops. The bridge triggers of 355 stay: `POST /admin/tenants`, PATCH, offboard and
  restore still write only `shared.tenants`.
- Platform › Agencies shows the CRM's allowed features read-only (crm.ops.v1 1.11: the CRM refuses the L1 write with
  409 `entitlements_managed_by_cis`; they follow the tenant's CRM subscription and overrides here).

**Slice 3** (`docs/implementation-notes/AA-1028-l9-r1-slice3.md`):
- **Plans & add-ons** (`/admin/tenants/plans`; API `api/routers/admin_plans.py`, rules
  `services/tenant_account/plans.py`): list / create / edit / retire plans and their feature rows, admin only,
  versioned (`shared.plan.version`, a stale write is 409 `version_conflict`), audited (`plan.created` / `.updated` /
  `.retired`, before / after). An edit re-syncs every subscriber at once; a retired plan takes no new subscription,
  current subscribers keep it. Product, key and tier never change; a tier plan must give `content.rate_limit_rpm`
  (a whole number) and list `ai.budget_usd_monthly` (a number, 0 = none included; NULL only on `internal`) and
  `content.rewrite`. A limit the plan has is never dropped by an edit (`limit_removal_refused`: "no row" = no limit),
  platform keys (`ai.*`, `policy.*`) go on content plans only, counts / seats / days / RPM are whole numbers within
  int4 and the RPM at most 10,000. An RPM edit also moves `shared.tenants.rate_limit_rpm` (what the limiter enforces) of every
  tenant on the plan whose column still equals the old value; a tenant with its own RPM (PATCH) keeps it.
- **Tenant 360 › Subscription** (`frontend/app/admin/tenants/_components/Tenant360Subscription.tsx`; API
  `api/routers/admin_tenant_subscription.py`, `services/tenant_account/subscription.py`): subscriptions, the resolved
  entitlements with their source, overrides (PUT / DELETE `…/entitlement-overrides/{key}`, reason required, optional
  end, audited `tenant.entitlement_override_set` / `_removed`; `enabled = false` is how staff take a flag away; a
  limit is stopped with 0, never "off", which would remove it; `content.rate_limit_rpm` is not overridable (PATCH the
  tenant) and `content.rewrite` needs a number),
  add-ons through the saga (attach / change, and `POST /admin/tenants/{id}/provision/cancel`, saga mode `detach`).
- **Trials**: the saga's `trial_days` (1-60) starts the account in `trial` and the subscriptions `trialing`; the hourly
  job `tenant_account_tick` (`services/tenant_account/lifecycle.py`, queued by the api's ops sampler, key per UTC hour,
  `TENANT_ACCOUNT_TICK=off` stops it) moves an ended trial to `active` (finance recorded `paid_through` covering
  today) or `past_due` (grace, service goes on) and the account out of `trial`, calls
  `tenant_entitlement_sync_due()`, and reports a pilot past its end (no change). Until the tick moves it, an ended
  trial stays live in the resolver (CIS 361: source `trial`, `expires_at` in the past), so nothing (the AI budget
  included) drops at `trial_ends_at`; CIS 361 also takes a platform key from the content plan only (tie-break on
  `plan_key`).
- **Billing from Booking** (AA-1037 R4, root ADR 0019 d9): every agency invoice is issued in AA-Booking. Booking reads
  `catalog.v_tenant_billing_v1` (CIS 377: subscriptions with plan prices and the bill-to fields, SELECT to `booking`
  only) and publishes `booking.v_tenant_billing_status` (bk-042, SELECT to `cis_app`). AA-1116 contract 1.10 (CIS
  443-444): the view also serves `discount_pct` / `discount_kind` per subscription line, the share the CIS quote
  (`services/tenant_account/quote.py`: founding or combo, the larger) takes off that line, stored in
  `shared.tenant_subscription.quote_discount_pct` / `_kind` by `services/tenant_account/quote_discount.py` (every
  hour in `tenant_billing_tick`, and at once after a Tenant 360 billing details / terms write). The hourly job
  `tenant_billing_tick` (`services/tenant_account/billing.py`, queued by the api's ops sampler, key per UTC hour,
  `TENANT_BILLING_TICK=off` stops it) writes a newer `paid_through` to the live subscriptions, moves `warn` -> account
  `past_due` + the bell `tenant.billing.overdue` (once a day; tenant, admin, finance), `past_due` -> `suspended` with
  `suspend_reason` `billing` (`tenant.billing.suspended`), and `ok` again -> back in service (only a billing suspension
  is lifted). On Dev only `demo` tenants move. Fail-soft without the Booking view (`skipped`). Tenant 360 › Billing
  shows the same view row for one tenant above the usage meters (`GET /admin/tenants/{id}/booking-billing`,
  `billing.tenant_snapshot`: status, overdue amount per currency, paid through, warn / suspend days, next to the
  account and live subscriptions; read only, `available: false` without the view; the Billing tab's rule:
  billing.read, or tenant.book.account.read on the own book) with "Open in Booking" (`lib/apps.ts
  bookingAgencyInvoicesUrl`).
- **Currency rule: USD only** (AA-1191, tech lead S295; CIS 497-498): one settings-registry key,
  `cis.billing.enabled_currencies` (Platform › Settings, area billing, maker-checker, audited; allowed `USD` / `VND`,
  default `["USD"]`), says which currencies the platform shows and accepts; USD is the base currency and always enabled.
  Reader `services/tenant_account/currency.py` (`enabled()`, `effective()`, `problem()`). While VND is not enabled:
  Plans & add-ons hides the VND list (column, plan drawer fields) and the commercial terms leave out the reference rate,
  the VND review period and the VND review / drift columns; Tenant 360 hides the billing-currency and agreed-currency
  pickers; a tenant whose stored `billing_currency` is VND is served and quoted in USD (`billing_currency_hidden` on
  `GET …/billing-details`; the quote has no VND reference and an agreed VND price does not apply); the Content studio
  billing page (`/v1` billing) prices plans and the quote in USD. Every write in a currency that is not enabled is
  refused with 422 `currency_not_enabled` (billing currency, agreed currency, a new or changed VND plan price). The VND
  columns keep their values (no DROP, no conversion), so enabling VND again on Platform › Settings brings them back.
  Booking reads the value through `catalog.v_billing_currencies_v1` (CIS 498, SELECT to `booking` only, contract
  tenant.entitlements.v1 1.12); the partner CRM through `GET /catalog/v1/currencies` (catalog.http.v1 1.5). Supplier
  cost currencies (Booking FX, CRM supplier lines) are out of this rule.
- **TripPlanner tenants** (AA-1038 R5 slice 2, root ADR 0019 d10; `docs/implementation-notes/AA-1038.md`): CIS 379
  adds `shared.tenant_tripplanner_site` (per tenant: `theme` = kit token name -> `#rrggbb`, CHECKed, at most 4 kB;
  `locale` en | vi; no RLS, cis_app DML, rule a allowlist) and `catalog.v_tenant_tripplanner_v1` (catalog recipe,
  SELECT to `tripplanner` only; contract tenant.entitlements.v1 1.4): one row per tenant with a TripPlanner subscription
  (kind internal excluded) with slug, trading name, kind, account status, `in_service`, `served` (in service and
  `tp.hosted` live), `tp_launcher`, `tp_branding` (BO-8 badge), theme and locale. TripPlanner answers `/{slug}` only for
  a served row. It also seeds the demo tenant `aa-tripplanner` (`00000000-0000-0000-0000-000000000002`, kind demo,
  plan_tier internal, a `tripplanner.hosted` subscription), the soft key of every TripPlanner row since tp-025. A bound
  tenant reads AA master catalogue rows only (`catalog._live_tour` keeps the master tenant). No admin editor for the
  site row yet (kit brand token slice).
  **Slice 3 (CIS 390, contract tenant.entitlements.v1 1.5; `docs/implementation-notes/AA-1038-r5-slice3.md`):** the site
  row gains the lead route of a tenant without the CRM (`lead_channel` none | webhook | email, `lead_webhook_url` https,
  `lead_email`; CHECKed, no secret); the view appends `crm_core`, the resolved `lead_channel` (crm when `crm.core` is live)
  and the target of that channel only; the new view `catalog.v_tenant_ai_budget_v1` (SELECT to `tripplanner` only) serves
  the tenant's resolved `ai.budget_usd_monthly` (`budget_source` entitlement / disabled / none) and its ledger spend this
  UTC month on every app, so TripPlanner caps its AI stages per tenant (failing closed) and its ledger rows (now carrying
  the tenant) count in the same pot as the T-stages. The **runtime brand-token layer** is kit core
  `frontend/app/_kit/core/brandTheme.ts` (tokens profile, import-free; root ADR 0016 amendment): a tenant theme becomes
  one CSS block over the accent family of `--aa-*` (light: every accent token that passes the WCAG pairs, the label
  colour picked for contrast; dark: the accent only when it keeps 3:1 on the dark card), checked by
  `lib/kitBrandTheme.test.mjs`. The lead route has a Tenant 360 editor (S272 follow-up): Subscription tab › TripPlanner
  lead route, `GET` / `PUT /admin/tenants/{id}/tripplanner-site/lead-route` (read: the Subscription tab's rule; write:
  tenant.write, admin; audited `tenant.tripplanner_lead_route_changed`; `services/tenant_account/tripplanner_site.py`).
  The PUT refuses (422 with the field) a webhook URL that is not https, not the 390 CHECK's shape, or names a host that
  cannot be public: an IP literal outside global unicast (IPv4 / IPv6, mapped / NAT64 / 6to4 unwrapped, legacy IPv4
  spellings) or an internal name (one label, `localhost`, `*.local`, `*.internal` ...). No DNS lookup in CIS:
  TripPlanner checks the resolved addresses again before storing a lead and before each POST. Still no staff editor
  for the theme (set by a reviewed `run-script` on Dev, or by the agency owner below).
  **The agency app's module (AA-1139, contract tenant.addons.v1 1.1; `docs/implementation-notes/AA-1139.md`):** the
  CRM relays its agency owner's edits over M2M (`api/routers/tenant_addons.py`, scope `cis.tenant/addons`): `GET
  /tenant/v1/tenants/{id}/tripplanner` (planner addresses, every custom domain with its state but `removing`, the
  stored route and the channel in force, theme, locale, the `tp.ai_sessions` quota), `PUT .../tripplanner/lead-route`
  and `PUT .../tripplanner/appearance` (theme = the kit brand tokens of `brandTheme.ts` only, `#rrggbb`, stored lower
  case; locale en | vi). The Tenant 360 editor's checks apply, plus an e-mail domain that is plainly internal is
  refused (`lead_email_not_public`, for staff too); a write needs an active Trip planner (409 `tripplanner_not_active`)
  and is audited as `crm-member:<id>` (`tenant.tripplanner_lead_route_changed` / `_appearance_changed`). Leads, usage
  and the webhook key are TripPlanner's (lead.submitted.v1 1.1); CIS reads none of them for this. An agency WITHOUT
  the CRM add-on has no CRM agency, so its Content studio owner edits the same route and look from the studio with its
  own tenant token (`api/routers/v1_account_tripplanner.py`: `GET /v1/account/tripplanner`, `PUT .../lead-route`,
  `PUT .../appearance`; every studio role reads, only the owner writes through the tenant role middleware, audited as
  `membership:<id>`; no view-as access). That is where the e-mail / webhook route takes effect (`effective_channel`
  is `crm` while `crm.core` is live and, since AA-1160, the CRM proved the tenant's agency lately).
  **CRM agency marker (AA-1160, CIS 474, contract tenant.entitlements.v1 1.11 / tenant.addons.v1 1.2;
  `docs/implementation-notes/AA-1160.md`):** `shared.tenant_crm_agency` records when the CRM last proved a tenant's
  agency (its M2M read of the tenant.entitlements.v1 snapshot, which it makes only for an agency it holds, at least
  hourly; the CRM onboarding finding or creating the agency; `services/tenant_account/crm_agency.py`, throttled, best
  effort). A sighting counts only inside the setting `cis.tenant.crm_agency_seen_hours` (default 3), so a deleted or
  offboarded agency ages out; the agency module's `effective_channel` and Tenant 360's lead route read it. The
  TripPlanner view appends only the fallback route (`fallback_channel` + target): TripPlanner routes each lead on its
  own CRM pull beacon (crm while the CRM lists the tenant, else the fallback).
  **R8 own domains (AA-1090, CIS 428 + 429, contract tenant.entitlements.v1 1.8; `docs/implementation-notes/AA-1090.md`):**
  `shared.tenant_tripplanner_domain` (one row per custom host of a tenant site; status pending / validating / attaching
  / active / removing / failed; no RLS, rule a allowlist) is edited on Tenant 360 › Subscription › TripPlanner own
  domains (`GET` / `POST /admin/tenants/{id}/tripplanner-site/domains`, `DELETE …/{domain_id}`, `POST …/{domain_id}/retry`;
  write: tenant.write; audited `tenant.tripplanner_domain_*`; `services/tenant_account/tripplanner_domain.py`). The
  job kind `tripplanner_custom_domain` (concurrency 1, polls by rescheduling) requests the host's ACM certificate in
  us-east-1, shows its validation record, then keeps ONE edge certificate on the TripPlanner AGENCY CloudFront
  distribution (its own distribution: the public host never shares a certificate with an agency) naming every attached
  host and swaps aliases + certificate in one UpdateDistribution (`services/tenant_account/tp_edge.py`); with no host
  left the distribution is back on no alias and the default certificate (the Terraform state). A host whose
  validation record is gone is failed and detached (`validation_record_gone`, alert `tripplanner_custom_domain_dns`)
  by the pending-certificate guard or the daily edge check (same kind, payload `check = edge_health`, queued by the ops
  sampler), so one agency never blocks or expires another's certificate. Env `TP_AGENCY_DISTRIBUTION_ID`
  (AA-CIS-Infra; absent = off, 409). Limits are settings (`cis.tripplanner.custom_domain_*`). TripPlanner reads the active hosts in
  `catalog.v_tenant_tripplanner_v1.custom_domains` and serves such a Host as the tenant.
- **No hard-coded plans**: `PLAN_LIMITS`, the ACPv1 quota ledger write (10/10/50, no reader) and the UI
  `PLAN_OPTIONS` / `PLAN_CHOICES` are gone; RPM per tier, the rewrite quota and the UI tier choices read the plans /
  entitlements (`useContentTiers`: the live public content plans).
- **AI budget = the entitlement** (`shared/cost_guard.py`): the gateway's tenant budget is `ai.budget_usd_monthly`
  (thresholds from the AA-1016 row if any, else alert 80 % / approval 100 % / hard stop), and `adhoc_*` stages count as
  tenant spend like T-stages (`shared/llm_client/tenant_budget.is_tenant_spend_stage`). It fails closed: a 0 USD
  budget stops the first call whatever the AA-1016 row's `hard_stop` says, a disabled override of the key reads as 0,
  and a tenant with no resolved key and no AA-1016 row falls back to its (not canceled) content subscription plan's
  value, never "no budget". Tenant 360 › Cost shows this effective
  budget and its source (`tenant_ai_budget.read_one` → `effective`).
- **Dev guard** (root ADR 0019 d16, `services/tenant_account/dev_guard.py`): on Dev (`ENVIRONMENT` exactly `dev`) a new
  tenant defaults to `demo` (synthetic) in `POST /admin/tenants` and the saga; an `agency` is refused unless the saga
  creates it as the one pilot (`pilot_weeks` 1-8). Existing rows keep their kind.
- **Leaving pilot mode** (AA-1086 G2, root ADR 0019 d16): `POST /admin/tenants/{tenant_id}/pilot/end` (administrator
  only, `tenant.write`; reason required) clears `is_pilot` and `pilot_ends_at` together, writes one
  `account.status_changed` event with the status unchanged (so `crm_entitlement_pull` re-reads the snapshot and clears
  `crm.tenant.is_pilot`) and one `tenant.pilot_ended` audit row, in one transaction (`services/tenant_account/pilot.py`).
  A tenant that is not the pilot is a no-op 200 (`changed` false). Any status is accepted, so an offboarded pilot still
  frees the slot (`tenants_one_pilot_key`). Tenant 360 › Subscription shows "End pilot" next to the pilot badge (confirm
  dialog with the reason).
- **Pilot protections** (AA-969 part 2, root ADR 0019 d16): clean-up never touches the pilot tenant. The retention
  kinds of tenant rows (`retention_llm_call_log`, `retention_tenant_api_usage`) leave its rows out
  (`services/retention/policy.py` `NOT_PILOT`; the rollups still count them), and the E2E identity's "synthetic
  tenant" check (`api/core/e2e_staff.py`) is false for it whatever its canary flag. Not changed, with reasons in
  `docs/implementation-notes/AA-969-l1-aa969-d16.md`: the A1-A3 waves (AA's own catalogue, not tenant data), the
  tenant's own T2 / T7-T9 jobs, the ADR 0015 retirement propagation, `data_erasure` (approved DRY RUN process),
  the platform retention kinds (`retention_job`, `retention_decision_log`, `retention_cost_explorer_snapshot`).

Not yet: retiring `shared.tenant_ai_budget` into overrides, the billing tick (`past_due` -> `suspended` after the
grace), moving the limiter onto the entitlement (it still reads `shared.tenants.rate_limit_rpm`, mirrored by CIS 356).

### Tenant lifecycle (AA-797)

A tenant (`shared.tenants`) is in one of three states, derived from two columns
(`tenant_status()` in `api/routers/admin.py`):

- **active**: `is_active = true`, `cancelled_at IS NULL`.
- **suspended**: `is_active = false`, `cancelled_at IS NULL`. Reversible through
  `PATCH /admin/tenants/{id} {"is_active": ...}`. The rate-limit middleware returns 403 "Tenant is
  deactivated" to the tenant's JWT. The PATCH clears the 30 s Redis `tenant_meta:{id}` cache, so
  this applies at once.
- **offboarded**: `cancelled_at` set. Reached only through `POST /admin/tenants/{id}/offboard`,
  which needs a reason. That call sets `account_status = 'offboarding'` (the CIS 354 trigger keeps
  `is_active = false` and `cancelled_at` in step; AA-1036 dropped the old `REVOKED_` key marker), removes the
  tenant from the Jev allow-list (snapshotted in the audit row) and
  cancels its QUEUED `shared.job` rows (running jobs finish). It deletes no data or setting.

PATCH on an offboarded tenant returns 409. `POST /admin/tenants/{id}/restore`
(Nghiệp, 14/10/2026) brings it back whole: it clears the offboard markers, sets `account_status = 'active'`,
re-adds the snapshotted Jev allow-list row (no key is issued, AA-1036); plan and settings are unchanged, and cancelled jobs are listed, not re-enqueued. Tenant
data erasure is a separate step after offboarding plus a retention window, with explicit approval: the
`data_erasure` job (AA-839, see the job runner section) — DRY RUN by default, apply OFF behind
`ERASURE_APPLY_ENABLED`; after an apply the tenant cannot be restored.

Every admin write to a tenant (create, plan, status, key, brand identity, config, offboard,
restore, end pilot) is admin-role only. The BFF enforces this through `lib/rbac.ts` `API_WRITE_RULES`, and the
backend re-checks `x-admin-role`. Each write leaves one `acp_shared.audit_log` row
(`AdminTenantAuditAction`, actor `admin:{x-admin-user-id}`). `plan_id` (→ `shared.membership_plans`)
is set on create and on every plan change. `GET /admin/tenants` lists only active tenants unless
`include_inactive=true`.

**Tenant 360 (AA-666, AA-766)** — the inline panel on `/admin/tenants` (no separate page). Settings
edits plan / RPM (PATCH) and the planning config (`PUT /admin/tenants/{id}/config`: markets,
channels, posts/week). Admin-only read endpoints in `api/routers/admin_tenant360.py`:
`/cost` (month-to-date `llm_call_log` per stage, $/piece per channel, $/tour for T2, plan revenue
vs LLM cost → margin; same cost definition as billing), `/quality` (T10 gate verdicts per gate from
`content_piece.gate_ledger`, held reasons, tenant edit rate on T2 versions) and `/markets` (SEO
markets from `tenant_seo_config`, `segment_research_log` coverage over the places in the tenant's
tours, planning markets, the tenant's `unmapped_market_requests`). None of it is in a tenant API.

### Tenant DB role (`aa_app_user`, AA-817 / AA-859)

Tenant routes moved off the main pool run on the **tenant pool** (`acquire_scoped_conn`, per-route
Redis flag, fail closed). It logs in as `aa_app_user` (`aa-cis/dev/rds-app-user`) and sets
`app.tenant_id` for the transaction; isolation is RLS, not the query's own `WHERE tenant_id`.

- **RLS template** (229; also 237, 241, 262, 265): `ENABLE ROW LEVEL SECURITY` without FORCE (the owner `cis_owner` stays exempt; the
  main pool and the worker log in as `cis_app` since X1 S18 and read through `<table>_cis_app_all`, CIS 244), policy `tenant_isolation` `FOR ALL TO aa_app_user`, USING and WITH CHECK on
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (`''` = no rows, never an error). A table
  without a tenant column goes through its parent: `acp_shared.angle_gate_option` matches options of
  `angle_gate_request` rows of the tenant (241), `acp_shared.quarter_plan_version` versions of the tenant's
  `quarter_plan` rows (265). `acp_deliver.tenant_tour_pages.tenant_id` is text: its policy compares text without a cast,
  plus `aa_app_user_aa_master_read` (the AA master's public pages, 239 shape). `acp_contract.tour_atoms` (266) is
  `FOR SELECT` only for the tenant role: `atoms_platform_read` (`owner_scope = 'platform'`) and `tenant_isolation`
  (`owner_scope` = the GUC, text); `catalog_owner_read USING (true)` keeps the catalog views reading every atom (D9:
  no live tenant atom on a live master tour, guarded at apply); TripPlanner holds no grant on it (CIS 254).
- **No privilege without RLS** (241; v2 since 265): `aa_app_user` holds no privilege, table or column level, on any
  relation without RLS. Two exceptions: the cross-app catalog contract, `SELECT` on the `catalog.v_*` views owned
  by `catalog_owner` (225 / 228), which other apps read too; and the **platform read allowlist** (265,
  `PLATFORM_READ_ALLOWLIST`): `SELECT` only, table level on `acp_contract.route`, `atom_segment`,
  `atom_segment_member`, `atom_segment_alias`, `acp_shared.place_card_alias`, `atom_place_card`, and column level
  on `acp_shared.place_card` (`card_id`, `status`: the staff `verified_by`, `cost_usd`, `naming_note` and `facts`
  stay unreadable), relations with no tenant column and no `owner_scope` (265 guards it), AA's shared derivation.
  `acp_contract` became reachable by the tenant role in 265, which therefore refuses any PUBLIC grant on a relation
  there without RLS and any SECURITY DEFINER function there the tenant role can execute.
  The `acp_contract` views (`v_active_tour_atoms`, `v_trip_registry`, owner rights) stay ungranted: tenant-pool
  code reads atoms through the base table with the 203 rule inlined. A new table the tenant pool needs gets RLS
  and its grants in the same migration. Enforced by `NO_PRIVILEGE_WITHOUT_RLS_SQL_V2`
  (`tests/integration/_rls_fixture_helpers.py`), guard 5 of 265 / 266; v1 (`NO_PRIVILEGE_WITHOUT_RLS_SQL`, 241's
  guard 4 and 262's guard 5, kept byte for byte) holds only before 265.
- **Catalogue-wide RLS guard (X2):** `tests/integration/test_rls_catalogue_guard.py` runs the rules in
  `tests/integration/rls_catalogue_rules.py` on the replayed migrations in every PR's Integration Tests job.
  The rules: a `tenant_id` table needs RLS and an `app.tenant_id` policy; no policy `TO PUBLIC`; no
  `app.current_tenant`; `current_setting('app.tenant_id')` only inside `NULLIF`; no FK across owners; no grant to
  PUBLIC; grants to another owner's role only on the list; `USING (true)` only on the X1 parity policies. Each
  exception is an `ALLOWLIST` entry with a one-line reason. A `tenant_id` table the tenant pool can read is never
  allowlisted. AA-CIS-Infra runs a byte copy (`db/rls_catalogue_rules.py`, Copy Guard) on the nightly Dev dump
  and writes the result into `db-schema-reference.md`. If you change the file here, copy it to Infra.
- Write grants are column grants where a route writes only some columns (`tenant_integrations`
  connect / test, `angle_gate_option.chosen`), so a tenant cannot move a row to another tenant or
  channel even inside its own rows.
- **Routes on the tenant pool** (flag in `api/core/aa544_tenant_pool.py`, `fail_closed=True`, one log
  line per request with `used_tenant_pool=`). All ON on Dev since S240 (16/10/2026), **except GET
  `/v1/publish-log`**: `aa544:publish_log_list_pool` was pinned to `"false"` in Redis before the S241 D-12
  merge (#792, to keep its pre-D-12 OFF default) and never switched back, so that route runs on the admin pool
  (Logs Insights 22–24/10/2026, AA-874). To end the pin: `SET aa544:publish_log_list_pool true`, confirm
  `publish_log_listed` with `used_tenant_pool=true`, then drop `publish_log_listed` from
  `tenant_pool_known_rollback_events` in AA-CIS-Infra `accounts/aa365/tenant_pool_alarms.tf` and this note.
  **Flags fail closed (S241 D-12):** only an explicit OFF value puts a route back on the main pool;
  `true`, a missing key, any other value, a Redis error or a read slower than `FLAG_READ_TIMEOUT_S` (above the
  Redis client's own worst case, S282) all mean the tenant pool (`aa817_tenant_flag_read_failed` at ERROR on a
  failed read, at WARNING on a timeout since S282; the api warms its Redis client with one PING at start-up;
  `aa817_tenant_flag_unrecognized` at
  ERROR on a value that is neither ON nor OFF). The decision is read once per request (memoised on
  `request.state`), so a multi-block route (unpublish, disconnect) never switches pools mid-request.
  Rollback = `SET <key> false`, no deploy. Accepted OFF values (after trim + lower-case): `false` (what
  `aa859_flag.py` writes), `0`, `off`, `no`. `DEL` is not a rollback (it means ON). Confirm any rollback
  with `used_tenant_pool=False` in Logs Insights. **A Redis-only rollback does not survive a Redis node
  reboot / replacement / FLUSHALL** (single node, no persistence: every key is lost, so every route goes
  back ON with no log line). For an incident rollback that must stick, also add the key to the api env
  var `TENANT_POOL_FORCE_ADMIN_FLAGS` (comma-separated flag keys, ECS task definition, read once at
  process start; needs a task-definition deploy): a listed key is OFF whatever Redis says
  (`aa817_tenant_flag_forced_admin` INFO once per key per process; an unknown key logs
  `aa817_tenant_flag_force_admin_unknown_keys` at ERROR). A newly wired route is ON from its first deploy:
  to ship it dark, `SET <key> false` before the deploy, and add its constant to `ALL_TENANT_POOL_FLAGS`
  and to the `aa859_flag.py` ALLOWED set. `acquire_scoped_conn` defaults to `fail_closed=True`.
  The api's Redis client has 0.5 s socket connect / read timeouts and one retry
  (`api/core/redis_client.py`), so a hung Redis fails fast in the rate-limit middleware too.
  **Redis connection (AA-874).** Every client (api `app.state.redis`, worker resources, SEO cache) is built
  from `shared/cache/redis_conn.py` `redis_conn_args()`: `REDIS_HOST` (default `localhost`), `REDIS_PORT`
  (default 6379), `REDIS_TLS=true` → `rediss://` with the certificate and host name verified, and
  `REDIS_AUTH_TOKEN` (ECS secret from Secrets Manager) passed as `password=`, never in the URL. Without
  `REDIS_TLS` and token it is the plain `redis://<host>:6379` of the old node, so the image works on both; a
  token without TLS is dropped with one ERROR `aa874_redis_auth_without_tls`. The target node is an
  ElastiCache replication group with in-transit TLS (required), AUTH and at-rest encryption (AA-CIS-Infra).
  AA-CIS-Infra alarms (e-mail on ALARM and on OK) on any `aa817_tenant_flag_read_failed`,
  `aa817_tenant_flag_unrecognized`, `aa817_tenant_pool_unavailable`, `aa817_tenant_guc_not_applied` line and on
  any JSON line with `used_tenant_pool: false` (a route running on the admin pool, which RLS does not filter),
  except the events of routes rolled back on purpose listed in `tenant_pool_known_rollback_events` there (today
  `publish_log_listed`, the pin above). A deliberate rollback that is not listed holds that alarm in ALARM, and
  CloudWatch e-mails only on a state change: list it, or a second rollback goes unseen. Renaming any of these
  events silently disables its alarm.

  | Route(s) | Flag | Notes |
  |---|---|---|
  | GET `/v1/publish-log/pending` | `aa544:stage1:publish_pending_pool` | |
  | GET `/v1/content-writing/pieces/{id}` (+ `/export`) | `aa544:stage5:get_piece_pool` | |
  | GET `/v1/publish-log` | `aa544:publish_log_list_pool` | |
  | DELETE `/v1/integrations/{wordpress,facebook}` | `aa544:integration_disconnect_pool` | two short transactions around the Secrets Manager tombstone; a double click is 200/200 (`aa859_integration_disconnect_raced`) |
  | GET `/v1/content-writing/requests/{id}/latest-piece` | `aa817:content_piece_reads_pool` | |
  | PATCH `/v1/content-writing/pieces/{id}` (W2) | `aa859:piece_edit_pool` | audit after commit on MAIN |
  | POST `/v1/angle-gate/requests/{id}/choose`, `/reopen` (W3) | `aa859:angle_gate_decide_pool` | one transaction, request locked first (same order as the T8 worker); the response re-read follows `aa859:angle_gate_read_pool` (both on: same connection, same transaction); needs 241 |
  | DELETE `/v1/publish-log/{id}` (W4, unpublish) | `aa859:unpublish_pool` | three short blocks; Secrets Manager and the remote delete hold no connection |
  | GET `/v1/tours/pool`, `/my-versions`, `/versions/{id}` (W5a) | `aa859:t2_version_reads_pool` | job status from a second MAIN query keyed by job ids returned through RLS |
  | GET `/v1/tours/{id}/full` (tenant-JWT branch), `/{id}`, `/versions/{id}/export-docx` (D5b, S247) | `aa859:t2_version_detail_reads_pool` | own flag (not W5a's); the `/full` staff branch (X-Admin-Secret) stays on MAIN and reads no flag; export-docx audit on MAIN after the block; a master tour's raw / seo fields show only when those rows are the AA master tenant's (239), as in W5a |
  | PATCH `/v1/tours/versions/{id}` (W6) | `aa859:t2_version_write_pool` | `pg_advisory_xact_lock(859, hashtext(tenant:published_tour))` + `MAX + 1`; UNIQUE backstop → 409 `version_conflict`; AA-1144 fact lock reads the Master / raw base on the same connection (239), audit on MAIN after the block |
  | GET/POST `/v1/integrations/{wordpress,facebook}`, POST `.../test` (W9) | `aa859:integration_connect_pool` | Secrets Manager / remote HTTP hold no connection; the UPSERT writes only 241's column grants; needs 241 |
  | GET / POST `/admin/brand-identity` with a tenant Bearer (wave 3, S247) | `aa859:brand_identity_pool` | needs 262 (RLS on `tenant_brand_rules` / `tenant_seo_config`); POST = one transaction, `pg_advisory_xact_lock(859, hashtext(tenant:brand_identity))` + `MAX + 1`, UNIQUE backstop → 409 `version_conflict`; the X-Admin-Secret staff branch stays on MAIN and reads no flag; `check_saved_cta` on MAIN after commit |
  | POST `/v1/publish-log/{piece_id}/publish` (wave 3, S247) | `aa859:publish_pool` | three short blocks (piece + connection, `claim()` as a savepoint, `finish()`); Secrets Manager and the remote post hold no connection; daily-limit override read and audit row on MAIN; no new grant |
  | GET `/v1/angle-gate/requests/{id}` and the choose / reopen response re-read (wave 4, S247) | `aa859:angle_gate_read_pool` | needs 265; request + options + Subject header in one block; the t8_angles job status (`generation_failed`) on MAIN after it, only for `pending_goal` without a goal; with the decide flag on too the choose / reopen re-read runs on the decide's own connection inside its transaction (no second acquire that could 503 after a saved choice), otherwise its own sequential block. Choose / reopen fully off the tenant pool = BOTH keys false |
  | GET `/v1/slate` (wave 4) | `aa859:slate_reads_pool` | needs 265; enqueue (`request_slate_proposal`) and the tenant's channel config on MAIN first, last proposal on MAIN after; `POST /v1/slate/refresh` stays on MAIN |
  | POST `/v1/subjects/{id}/pick`, `/cut` (wave 4) | `aa859:subject_decide_pool` | needs 265 + 266; one transaction, the Subject row locked `FOR UPDATE` (pick) / one conditional UPDATE (cut); members read on the base table with the 203 rule; the pick audit on MAIN after commit, best-effort (flag off: inside the transaction, AA-559); a lock wait past the tenant pool's `lock_timeout` → 409 `subject_busy` |
  | GET `/v1/planning/trip-reallocation/suggest`, POST `.../confirm` (wave 4) | `aa859:trip_reallocation_pool` | needs 265 + 266; config on MAIN, one read block; confirm accept = one more transaction, `pg_advisory_xact_lock(859, hashtext(tenant:quarter_plan:year:quarter))`, save + approve together, UNIQUE backstop → 409 `version_conflict`, lock wait past `lock_timeout` → 409 `plan_busy`; audit on MAIN after the blocks for accept and reject, best-effort (flag off: audit first, as before) |
  | GET `/v1/marketplace` (wave 4) | `aa859:marketplace_pool` | needs 266; `posts_per_week` on MAIN first |
  | GET `/v1/content-writing/reviews`, `/requests/{id}/review` (wave 4) | `aa859:reviews_pool` | needs 265 + 266; one block each, no MAIN read; the trip page is a LATERAL (own page, else the AA master's) |
  | GET `/v1/notifications`, POST `/v1/notifications/read-all`, `/{id}/read` (AA-986 L3) | `aa986:tenant_notifications_pool` | needs 274 (RLS on `shared.notifications`: own rows written for tenants only; SELECT + UPDATE (is_read)) |

  A 241 rollback must `SET ... false` the two "needs 241" flags first (and list them in
  `TENANT_POOL_FORCE_ADMIN_FLAGS`), as 241's rollback comment STEP 0 says; a 262 rollback the same for
  `aa859:brand_identity_pool` (262's rollback comment STEP 0); a 265 / 266 rollback the same for the wave-4 flags
  that need it (their headers' STEP 0).

  **Wave-4 behaviour changes** (S247): the pick and the trip-reallocation confirm write their audit row on MAIN after
  the tenant transaction, best-effort, so a lost audit row no longer fails them (flag on only); confirm's save and
  approve are one transaction (a crash between the two no longer leaves an orphan pending version); the pick locks
  the Subject row, so a concurrent t7_slate proposal waits and then sees `picked`; the tenant trip query returns one
  row per tour with the tenant's own trip page, else the master's (it used to repeat a tour once per tenant with a
  page for it). The tenant route hub (`/v1/routes`, `/v1/hubs`, `/v1/route-picks`) is retired (0 callers, 0
  requests in 30 days); `acp_contract.route_pick` was quarantined (CIS 294) and dropped (CIS 296, AA-850).
- **MAIN exceptions** (code comment `# AA-859 MAIN exception: <reason>`): `write_audit_log`
  (`acp_shared.audit_log` has no RLS and is never granted; written after the tenant transaction
  commits, best-effort on the tenant path); enqueue-in-transaction routes (angle-gate goal, T2 rewrite /
  retry, content-writing write, slate refresh); `shared.job` status lookups (no tenant column); tenant
  self-config and usage reads (`/v1/quota`, `/v1/billing`, `shared.tenants` limits incl. publish's
  `daily_publish_limit`) keyed on the JWT `sub`; the tenant's planning config (`shared.tenants` /
  `acp_shared.tenant_config`, read first by the Slate and the trip-reallocation routes, D2) and the marketplace's
  `posts_per_week`; the angle-gate job status (`shared.job`, only for `pending_goal` without a goal);
  `check_saved_cta` → `notify_admins` after a brand save; staff branches (`/admin/brand-identity` with
  X-Admin-Secret, `/v1/tours/{id}/full` with X-Admin-Secret).
- **`/admin/atoms*` is staff only** (AA-859, S240): the AA-431 tenant-JWT branch was removed with no flag
  after 30 days of WAF logs showed 0 Bearer callers (1,041 calls, all `X-Admin-Secret`). A Bearer token
  gets 403 and logs `aa859_admin_atoms_tenant_refused`. No tenant route writes `acp_contract.tour_atoms`.
- **Sizing:** tenant pool `max_size` 2 per api task, api autoscaling max 7 (AA-CIS-Infra #156), so 14 ≤
  `aa_app_user` CONNECTION LIMIT 15. `aa859_tenant_pool_timing` (wait/hold ms) and the 60 s
  `aa859_main_pool_gauge` line are the inputs for any later resize. Since S247 the main pool is
  instrumented too (`api/core/aa859_main_pool_gauge.py::instrument_main_pool`): the gauge line carries the
  interval's `acquires` / `peak_in_use` / `waited` / `max_wait_ms` / `timeouts`, an acquire that waited
  >= 50 ms logs `aa859_main_pool_wait` (capped per interval), and an acquire with no timeout of its own gives
  up after 5 s (C2.3g) with ERROR `aa859_main_pool_acquire_timeout` (a 500, instead of hanging to the API Gateway
  30 s limit). Main pool size: the literal in `api/main.py` (source of truth, read by the Infra connection
  budget check). Per-role connections are checked by the api's ops sampler (`rds_role_connections`, > 80 % of the
  limit -> admin bell + alert e-mail; S263, was the AA-832 `AA/RDS RoleConnectionLimitPct` alarms).
