# AA-1036 R3 remainder — add-on requests (CIS side)

Root ADR 0019 d7 ("Add-ons open per entitlement or after a Request to the account manager"), clarifications TL-7 step 4.
Partner side (agency app page, sidebar, CRM AI pause banner, invited-user test): AA-Partner
`docs/implementation-notes/AA-1036-rest.md`.

## Decisions

- **One table in CIS (374, `shared.tenant_addon_request`).** CIS owns the tenant account and the R1 control plane
  (subscriptions, entitlements, overrides), so the request lives next to them, not in the CRM: Trip planner requests
  need no CRM agency, and the approval path is the control plane's own saga.
- **Approve = the existing control-plane path.** An approval enqueues a `tenant_provision` attach job (the same saga
  as Tenant 360 "Add an add-on") with request key `addon-request:<request id>`, in the SAME transaction that marks the
  request approved (the queue's `enqueue()` takes the connection). No approved request without its job, one job per
  request, and the saga writes the subscription, entitlements, feed events and `tenant.subscription_changed` audit rows
  as for any attach. The response has the provision job shape, so the admin UI reuses `runSaga` and waits for it.
- **Who asks:** the Content studio owner. `/v1/account` writes are owner-only already (tenant role middleware,
  `ACCOUNT_WRITE_PREFIXES`); a "view as tenant" session is read-only. The actor is `membership:<mid>` from the token,
  never an e-mail.
- **Who decides:** admin (`tenant.write`) or the tenant's account manager (customer_success, `tenant.book.write`,
  kept to its own book by `require_tenant_scope`; clarifications TL-7 step 4 says the account manager approves),
  re-checked in the route with `_require_tenant_book_write`. Support and finance never decide. Review fix (S268): the
  first cut was admin only.
- **The approve drawer polls the request, not the job route.** `GET /admin/tenants/provision/{job_id}` needs
  `tenant.read` (admin only), which the account manager does not hold, so the drawer polls the new
  `GET /admin/tenants/{tenant_id}/addon-requests/{request_id}` (same provision job shape + the request; Subscription
  read permission, tenant-scoped). `runSaga` takes a `pollPath`.
- **An approval never changes a live add-on (review fix, S268).** Changing the plan or seats of a live subscription
  is a `tenant.write` / `billing.write` decision; approving a request is `tenant.book.write` (the account manager).
  So: (1) `approve()` checks `LIVE_SQL` for the request's product inside its transaction and refuses 409
  `already_subscribed` before anything is written (the request stays pending; staff decline it); (2) the approval's
  saga request carries `only_new`: the saga inserts the add-on or turns on a row that is not live (canceled, paused,
  unpaid, an ended trial), and leaves a live row exactly as it is (step `<product>_already_on`), so an add-on that
  goes live between the approval and the job is not changed either. `only_new` comes only from the stored job
  payload of an approval; the admin provision bodies (`ProvisionAttachIn`) cannot carry it. (3) Every attach saga
  (Tenant 360 "Add an add-on" included) closes the tenant's pending requests of the products that are live after it:
  `approved`, `decided_by` the saga actor `job:tenant_provision:<job id>`, the plan the subscription has, note "The
  add-on was turned on by Adventure Asia.", `provision_job_id` the saga's job, one `tenant.addon_request_approved`
  audit row each (before the saga's own row). Guarded by `to_regclass` so the saga works before 374 is applied.
- **Lock order.** `approve()` now takes the tenant row `FOR SHARE` BEFORE the request row `FOR UPDATE`, the saga's
  order (tenant `FOR UPDATE`, then the pending request rows it closes): the two never deadlock, and no attach of
  the tenant can commit between approve's live check and its commit.
- **Not in service = no approval.** `approve()` reads the tenant row (`FOR SHARE`) before the request lock and refuses
  409 `tenant_not_in_service` unless `account_status` is trial / active / past_due AND `is_active` AND no
  `cancelled_at` (`in_service()`, also used by the request and the agency's list), before the request is marked or the
  saga enqueued. A decline still works.
- **Setup state.** An approved request carries `setup` from its job (LEFT JOIN `shared.job`): `in_progress`
  (queued / running), `done` (succeeded), `failed` (failed / cancelled / stopped_budget: the add-on is NOT on), null
  when the job row is gone (retention). Tenant 360 shows "Approved, setting up" / "Approved, setup failed"; the agency
  API carries the same `setup` (optional field, no job id).
- **Ordering:** the audit row is the last write of each transaction (QRY-4: the audit chain trigger holds a
  transaction lock), as `provision.py` does; the request's staff bell row is written before it.
- **Bell:** event `tenant.addon.requested`, target roles admin + customer_success. The bell's role scope already
  narrows customer_success to the tenants of their book (S268 fix round 2). Title "Add-on requested", message
  "<agency> (<slug>) asks for the <add-on> add-on: "<note>"".
- **Repeat requests:** a second request while one is pending answers the pending one (200, `created: false`, no second
  bell row); at most 10 requests per tenant in 24 hours (429); refused when the add-on is live (409
  `already_subscribed`) or the account is not in service (409 `tenant_not_in_service`).
- **Tenant Activity** (Account › Activity) lists "Requested an add-on", "Add-on request approved / declined" with the
  add-on name only (never the plan key, the job id or the staff note): `api/services/tenant_account.py` allowlist.
- **Declines carry a reason** (required, at most 500 characters) that the agency sees in its add-ons page; an approval
  may carry an optional note.

## Changed (vs. the lane brief)

- The staff list is per tenant (Tenant 360 › Subscription › Add-on requests, pending first, a "N waiting" badge); there
  is no cross-tenant queue page. Staff find new requests through the bell. A cross-tenant list is a follow-up if the
  volume needs it.
- No tenant-facing notification on a decision: the agency app's add-ons page shows the state and the decline reason.
  The studio bell renders tour events only today; adding a decision event there is a follow-up.

## Tradeoffs

- No RLS on the table (cis_app only, rule a allowlist), like the other R1 control-plane tables: the tenant id comes from
  the verified token or the staff path, and the routes run on the main pool. The cost: the file is contract (MIG-4),
  not expand.
- `provision_job_id` has no FK (shared.job rows are pruned by retention; the request outlives them).
- Readiness requires 374 (`REQUIRED_CIS_ABOVE_GAP`); the routes also answer 503 while the table is missing, so a code
  deploy before the apply degrades to "not available yet" instead of a 500.

## Should know

- Cross-tenant: every decision and the single read look the request up by (request id, tenant id from the path), so
  another tenant's id answers 404 `request_not_found` and leaves its row as it was (integration test).

- Migration-only PR first (374, contract, applied by hand: the CIS auto-apply waits behind 336), then this code PR.
- `docs/api/openapi.json` regenerated with the pinned FastAPI 0.161.1 / Pydantic 2.14.1; the staff route policy snapshot
  regenerated (3 new admin routes).
- Retention row (DM-7.1): root data standards Part B section 7, root PR merged before the Infra copy of 374.
- View-as (review fix, S268): `/v1/account/add-ons` is in `ROUTE_CAPS` (`tenant.read` | `tenant.book.account.read`)
  and in the frontend mirror `VIEW_AS_ROUTE_ROLES` (admin, customer_success): it carries each add-on's subscription
  status and the request history with the staff decision notes, which support's own staff pages never show (the
  Subscription tab and `…/addon-requests` need `tenant.read` | `tenant.book.account.read` | `billing.read`).
  Support's view-as gets 403 `view_as_not_for_role` on it (logged); no view-as page reads it today.
