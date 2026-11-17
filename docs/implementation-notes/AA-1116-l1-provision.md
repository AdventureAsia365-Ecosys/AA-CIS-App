# AA-1116 — a new tenant's CRM agency and owner invite with no hand step (S283 lane l1, CIS part)

S282 audit gaps G5 / G11: after the `tenant_provision` saga a tenant with a `crm` add-on still needed Platform ›
Agencies "Create agency" and "Invite first owner" by hand (pilot rehearsal runbook steps 1-2; S282 also attached the
CRM add-on by hand on Tenant 360). The pricing parts of this card are in `AA-1116.md`; contract 1.10 (discount column)
in `AA-1116-l1-discount.md`.

## Decisions

- **The relay runs in the staff request, not in the job.** crm.ops.v1 takes `POST /tenants` and
  `POST /tenants/{id}/owner-invite` only from a signed-in staff member's WORKFORCE token in group
  `crm-platform-admin`; the saga job has no token and CIS has no M2M client towards the CRM (and must not store a
  staff token). AA-Partner is out of this lane, so no new CRM route: the provisioning routes call
  `crm_onboarding.onboard()` with `relay_authorization(request)` right after enqueueing the saga.
- **The route runs the saga's transaction itself** (`pv.provision(pool, req, job_id=<the job>)`) to learn the new
  tenant id. Safe by the saga's own design: one transaction, an advisory lock per request key and the audit row naming
  the key, so the job's later run finds it done and returns it `resumed` (integration test). A refused saga
  (`slug_taken`, ...) reports `crm_onboarding.status = skipped` and the job is refused the same way; a lost
  connection reports `provision_pending` (the job retries; `POST …/crm-onboarding` finishes later).
- **Agency on every crm add-on, invite only with an address.** `crm_owner_email` is optional; without it the agency is
  still created (`crm_owner_not_invited`). An address without a crm add-on, or a malformed one, is 422 before anything
  is enqueued, so a typo never leaves a half-made tenant.
- **The invite stays the tenant's own.** The address goes to the CRM only for the agency whose id is that CIS tenant;
  the CRM refuses a second owner (`owner_exists`), a second pending invite (`owner_invite_pending`) and an agency
  that holds travellers (`has_travellers`). CIS never stores, logs, audits or returns the address (audit:
  `owner_invite` sent / pending / owner_present / not_sent and `owner_email_given`; no hash either: the audit chain
  outlives an erasure).
- **Idempotent.** `GET /tenants/{id}` first; 404 → create; `tenant_exists` on a race = exists; `owner_invite_pending` /
  `owner_exists` = done. A second run changes nothing and writes no audit row.
- **Feed nudge.** When the agency was just created, one `account.status_changed` with the status unchanged (as
  `pilot.end_pilot`), because the saga's events may have reached `crm_entitlement_pull` before the agency existed
  ("unknown agency", skipped until the hourly reconcile).
- **is_synthetic = Dev.** The CRM takes synthetic agencies only on Dev and real ones only elsewhere
  (`agencies.provision_agency`), whatever the CIS `kind`.
- **Ticket ref**: the caller's (`crm_ticket_ref`, crm.ops.v1 pattern) or `CISONB-<unix time>`.

## Changed

- New `services/tenant_account/crm_onboarding.py`; audit action `tenant.crm_onboarded` (admin-only, not in
  `TENANT_VISIBLE_ACTIONS`).
- `api/routers/admin_tenant_provision.py`: `crm_owner_email` / `crm_ticket_ref` on the create and attach bodies (never
  in the job payload), `crm_onboarding` on the response, new `POST /admin/tenants/{tenant_id}/crm-onboarding`
  (`tenant.write`, admin role; not in the A10b E2E write allow-list, so the E2E identities are refused).
- Snapshots: route policy, role matrix, `docs/api/openapi.json`.

## Tradeoffs

- The CRM calls make the provisioning request slower when a crm add-on is in it (three CRM round trips, 10 s write
  timeout each, the owner invite mails inside the CRM transaction). Accepted: one staff click, no hand step.
- A staff member without `crm-platform-admin` gets the tenant and the subscription but `crm_onboarding.status`
  `failed` with the CRM's 403 code; Platform › Agencies stays the fallback.
- The New tenant modal (`frontend/app/admin/tenants/page.tsx`) still calls the synchronous `POST /admin/tenants`
  without add-ons; the Tenant 360 add-on drawer is the UI path that runs the saga today. Moving the modal onto the
  saga with a CRM option and an owner address field is the follow-up UI slice.

## Should know

- No migration. The audit action is free text in `acp_shared.audit_log`.
- Live check after deploy: on a synthetic tenant, `POST /admin/tenants/{id}/provision` with a crm add-on and an E2E
  mailbox address returns `crm_onboarding.status = done`; Platform › Agencies lists the agency; within one
  `crm_entitlement_pull` slot its plan is the crm plan.
