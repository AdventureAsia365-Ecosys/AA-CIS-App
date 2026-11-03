# AA-1028 R1 slice 3 (AA-CIS-App) — Plans & add-ons, Tenant 360 Subscription, trial tick, PLAN_LIMITS removal, fail-closed AI budget, Dev pilot guard

Lane l9-r1-slice3 of S266 (pulled ahead from S268). Root ADR 0019 d1 / d2 / d16; design: root
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §3.2-3.7; slices 1-2: `AA-1028-l1-r1-control-plane.md`,
`AA-1028-l8-r1-slice2.md`. One code PR, **no migration**: cis_app already holds every grant this needs (CIS 353:
INSERT / UPDATE on `shared.plan`, all on `plan_feature`, `tenant_subscription`, `tenant_entitlement_override`, SELECT
on the resolver; `shared.tenants` writes).

## Decisions

- **Plan catalogue API** (`api/routers/admin_plans.py`, rules `services/tenant_account/plans.py`): list (with live
  subscriber counts), feature catalogue, create, edit, retire. `plan_key`, `product` and `tier` are fixed at create
  (the tier links `shared.tenants.plan_tier` to its plan through the 355 bridge). **Versioned** = every write bumps
  `shared.plan.version` and names the version it was made from (409 `version_conflict` on a stale one); the audit
  row (`plan.created` / `plan.updated` / `plan.retired`, resource_type `plan`, tenant NULL) holds before / after.
  An edit changes every subscriber at once (the CIS 353 `plan_feature` trigger re-syncs and writes
  `entitlement.changed`, actor `admin:<id>` through `aa.actor`).
- **Retire = no new subscription, current subscribers keep it** (the CIS 353 subscription guard refuses a retired
  plan on INSERT / plan change; the resolver does not look at `retired_at`). A retired plan is frozen. Retiring the
  live plan of a tier frees the tier for a new plan (one live plan per tier, the 353 unique index); until one exists,
  creating / moving a tenant to that tier is refused.
- **A content plan with a tier must give `content.rate_limit_rpm`** (the limiter column and the tenant create read it).
- **Feature keys stay a migration-owned catalogue** (`shared.entitlement_feature`, cis_app SELECT only): a key is code
  (apps check it), so the Plans page picks from the catalogue and never invents a key.
- **Overrides** (`api/routers/admin_tenant_subscription.py`, `services/tenant_account/subscription.py`): PUT / DELETE
  per (tenant, feature key), reason required, optional `expires_at`, audited (`tenant.entitlement_override_set` /
  `_removed`, before / after, `applies`, the resolved value). `enabled = false` is the revocation path that
  replaced the CRM's L1 entitlement write (l8 re-review minor[1]); a flag takes no limit; a disabled key takes no
  limit. The answer says whether the override applies now (a crm / tripplanner key needs that add-on live).
- **Saga** (`services/tenant_account/provision.py`):
  - new mode `detach` (`POST /admin/tenants/{id}/provision/cancel`, `products`): the subscription row stays, status
    `canceled`; its plan keys and its product's overrides stop (resolver); the CRM / Booking pulls follow the feed.
  - **trial back**: `trial_days` 1-60 starts the account in `trial`, the content subscription (and every add-on of the
    same request) `trialing` with one `trial_ends_at`. The 422 `trial_not_supported_yet` is gone because the tick
    ships in the same PR.
  - **no `acp_quota_ledger` row** any more (the ACPv1 S2/S3/S4 10/10/50 ledger has had no reader since 13/07/2026:
    `git grep` finds only writers); same in `POST /admin/tenants`.
- **Trial-end tick** = job kind `tenant_account_tick` (`services/jobs/tenant_account_tick_job.py`, rules
  `services/tenant_account/lifecycle.py`), queued once per UTC hour by the api's ops metrics sampler (idempotency key
  `tenant_account_tick:<YYYY-MM-DDTHH>`, `TENANT_ACCOUNT_TICK=off` stops it). Next state of an ended trial:
  `active` when finance recorded `paid_through >= today` (billing v1 is manual, §3.7), else **`past_due`** (the
  README's grace state: service goes on, finance sees it). The account leaves `trial` the same way once no trial of
  it runs. Suspending a `past_due` account after the grace is the billing tick's job (`tenant_billing_tick`, not in
  this slice). The tick also calls `shared.tenant_entitlement_sync_due()` (expired overrides; nothing called it
  before) and reports a pilot past `pilot_ends_at` (log + result, no change: ending a real agency's service is the
  tech lead's call). Per-tenant transactions, `SKIP LOCKED`; not deploy-blocking, not background.
- **PLAN_LIMITS / PLAN_OPTIONS gone** (d2): a tier's starting RPM is its live content plan's `content.rate_limit_rpm`
  (`plans.tier_rate_limit_rpm`; seeded 60 / 300 / 1,000 / internal 60 = the old dict, so nothing changes for any
  existing tenant), the PATCH / create 400 lists the assignable tiers from the catalogue; the billing view's per-plan
  RPM reads the plans; the UI tier choices (`useContentTiers`) are the live **public** content plans with a tier
  (Starter / Growth / Business today, as before) plus the tenant's own tier. The rewrite quota reads the tenant's
  resolved `content.rewrite` first (seeded 50 / 200 / 500 = `membership_plans`), the old `membership_plans` read stays
  only as the fallback for a tenant with no numeric key (Enterprise / internal: no rewrite limit), so no number moves.
- **Fail-closed AI budget** (moved from AA-1024 A8): `shared/cost_guard.py` reads the amount from the resolver
  (`ai.budget_usd_monthly`: override > the AA-1016 `tenant_ai_budget` row > the plan; NULL = unlimited, e.g. AA itself)
  and the thresholds from the AA-1016 row when the tenant has one, else **alert 80 %, approval 100 %, hard stop on**
  (the included spend is the ceiling). A tenant with no resolved key keeps its AA-1016 row; before CIS 353 the old
  read alone (fallback on UndefinedTable). The gateway treats `adhoc_<issue>` stages like T-stages
  (`tenant_budget.is_tenant_spend_stage`): a hand-run script bound to a tenant spends that tenant's budget, which is
  what the fail-closed proof exercises. Platform (A-stage) calls stay outside tenant budgets.
- **Dev guard of the pilot exception** (d16, `services/tenant_account/dev_guard.py`): with `ENVIRONMENT` exactly
  `dev`, a new tenant without `kind` is `demo` (synthetic: `is_canary` true) in both create paths, an `agency` is
  refused (`dev_real_tenant_refused`) unless the saga creates it as the one pilot (`pilot_weeks` 1-8: `is_pilot`,
  `pilot_ends_at`; a second one `pilot_taken`). Elsewhere nothing changes (default `agency`, the pilot marker is
  refused). Existing Dev rows keep their `kind` (the guard decides new tenants).

### Review fix pass (S266, eight findings, all real)

- **Plan RPM edits follow live** (`plans.update_plan` → `_follow_rpm`, `FOLLOW_RPM_SQL`): the limiter and the Lambda
  authorizer enforce `shared.tenants.rate_limit_rpm`, copied from the plan at create. An edit of a content plan's
  `content.rate_limit_rpm` now moves that column, in the same transaction, for every tenant on the plan (content
  subscription trialing / active / past_due) whose column still equals the old plan value; a tenant with its own RPM
  (PATCH) keeps it. The CIS 356 trigger re-syncs each one (source `plan`). Count in the audit row and the response
  (`rate_limit_followed`). The RPM must be a whole number (`rate_limit_whole`: the column is an integer). A plan that
  gains or drops the key moves no one.
- **Overrides no reader enforces are refused** (`subscription.set_override`, all 422):
  `use_rate_limit_patch` for `content.rate_limit_rpm` (the column outranks an override in the 356 resolver and is
  what the limiter reads: PATCH /admin/tenants/{id}); `limit_cannot_be_disabled` for `enabled = false` on any key
  that is not a flag (the resolver drops a disabled key, and "no row" means no limit, so "off" opened the limit:
  the AI budget failed open; 0 is the way to stop a limit); `limit_required` for "no limit" on `content.rewrite` (the
  quota is published as a number by `GET /v1/quota` and the Partner portal meter; changing that contract to allow
  null was out of scope). The snapshot carries `override_rules` and the drawer follows them: the RPM key is not
  offered, a limit offers "Set a limit" / "No limit" (not "Turn off"), the rewrite quota only "Set a limit".
- **Fail closed in depth** (`shared/cost_guard.py`): an active disabled `ai.budget_usd_monthly` override that exists
  anyway (written before the rule, or by hand) is served as a 0 USD budget with the hard stop on, and the tenant is in
  `budgeted_tenants`. **A 0 USD budget now stops the first call** (`TenantAIBudget.used_pct` is 1000 % for a 0 budget
  even before any spend; before, the first call went through and only spend > 0 stopped): "0 = no AI" is exact. The
  AA-1016 tile's `budget_state` matches (0 budget = `approval` / `over`).
- **Rewrite quota honours overrides** (`v1_tours._get_tenant_plan_limit`): an override of 0 was already honoured
  (COALESCE keeps 0); an active disabled override counts as 0 (never the plan's quota).
- **Tenant 360 › Cost tile shows what the gateway applies** (`tenant_ai_budget.read_one` → `effective`: source
  `subscription_override` / `budget_row` / `plan` / `none`, amount, thresholds, state, from
  `cost_guard.tenant_budget_check`; None before CIS 353). The tile shows the effective amount and state, names the
  source (amber when a Subscription-tab override wins over the row edited here), and the copy says "remove" falls back
  to the plan's included spend and "empty" means no budget at all (the row's NULL outranks the plan).
- **Fail-closed proof script** (`.tmp-session/S266/l9/failclosed.py`, not in the repo): the 0.01 USD override has
  `expires_at = now + 30 min` (ends by itself if the exec session dies; the live resolver ignores it at once), the
  restore re-applies a saved override only while it is still active (else removes the proof override, a missing one
  is fine), and a failed restore prints `RESTORE FAILED` with the exact call to run and exits 3.
- `plans.LIMIT_MAX` is written `Decimal(10) ** 12 - Decimal("0.01")`: the literal (twelve nines) tripped the public
  repo resource-id check (`.github/scripts/check_resource_ids.py`) as an account id.

## Changed (vs. the issue / design text)

- Enterprise becomes assignable from the admin PATCH / create (its seeded plan sets RPM 1,000, so the data says it
  is); it is not public, so the UI does not offer it, and the saga still offers Starter / Growth / Business only.
- `POST /admin/tenants` gains an optional `kind` (agency / demo).
- `shared.tenant_ai_budget` is not retired here: it keeps acting as the `ai.budget_usd_monthly` override (and holds the
  per-tenant thresholds). Retiring it (move its rows to overrides + thresholds elsewhere) is a later migration.

## Tradeoffs

- Hard stop at 100 % of the plan's included AI spend for every priced tenant, from deploy: correct per d2 / BO-19, but
  a Dev E2E tenant that already spent more than its plan's 15 / 40 / 80 USD this month has its T-stage jobs held for
  approval (`stopped_budget`, the bell names it). The live step below checks the spend per tenant before the deploy.
- Trial end -> `past_due` rather than `paused` / `suspended`: no loss of service without a person deciding, at the
  cost of a state that needs the billing tick to move on.
- No `GET /admin/plans/{key}` history table: versions are the audit rows (before / after); a history table is a
  migration for a later slice if staff need a timeline view.

## Should know

- Review fix pass: no new route, no response-model change (openapi.json unchanged); the AA-1016 `budget_state` /
  `TenantAIBudget` change of the 0 budget is deliberate (two AA-1016 test cases updated). Existing Dev rows: a
  disabled numeric override (if any) keeps failing closed through cost_guard / the quota reader; staff edit it to a
  limit in the drawer (it shows "Turn off" for such a stored row so it can be seen and changed).

- Order: this PR alone (no migration, no readiness raise). Deploy api + worker together (the worker runs the new
  kind; the api's sampler queues it once the kind is registered in that process).
- `tests/unit/conftest.py` has a `seeded_plans` fixture (the tier readers answered with the 355 numbers) used by the
  mocked-connection tenant tests (`test_aa309`, `test_aa496`, `test_aa766`, `test_aa797`).
- `docs/api/openapi.json` regenerated with fastapi 0.161.1 / pydantic 2.14.1 (the pins).
- `tests/unit/snapshots/aa882_staff_route_policy.json` regenerated; the router sample list has the two new routers.
- The fail-closed proof script (not in the repo; the orchestrator runs it inside the api container) needs this PR
  deployed: it sets the E2E tenant's override to 0.01 USD, calls `nova-lite` through `LLMClient.generate()` with stage
  `adhoc_aa1028` until the gateway refuses, logs every billed call with `**billed_fields(resp)`, restores the override.
