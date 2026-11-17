# AA-1116 — the New tenant dialog shows the saga outcome (S283 lane l12, CIS part)

The lane brief said the Platform › Tenants "New tenant" dialog still called the old `POST /admin/tenants`. It does not:
the l1 review fix in #1556 (`AA-1116-l1-provision.md`, "Changed") already moved it to `POST /admin/tenants/provision`
through `runSaga`, with the CRM and TripPlanner add-on choices and the optional owner e-mail. What the dialog did not
show was the outcome: the result was one sentence (created + a CRM note). This lane adds the line-by-line outcome.

## Decisions

- **One row per thing the request set up**, read from the job's saga steps (`provision.py`) and the POST's
  `crm_onboarding` (`crm_onboarding.py`): tenant account (`tenant_created`); Content studio subscription
  (`content_trialing` -> "trial", `content_active` -> "active"); each add-on the request named (`<product>_subscribed`
  / `_already_on` / `_unchanged`; `<product>_seats_raised_to_N` -> "seats raised to the minimum, N"); for a crm
  add-on the CRM agency (`crm_agency_created` / `crm_agency_exists`) and the owner invitation (`crm_owner_invited`
  "sent", `crm_owner_invite_pending` "already pending", `crm_owner_present` "owner already in",
  `crm_owner_not_invited` "Not sent (no e-mail)"). A CRM refusal shows "Needs a look" with the CRM's code, as sent.
- **States**: done, still setting up (the worker has not reached the step within the dialog's 30 s wait), not sent,
  needs a look. No list while the job has no steps yet (the "being set up" note is enough).
- **Pure helper** `provisionOutcome(job, products)` in `lib/tenantPlans.ts` (unit tested); the dialog component
  `OutcomeList` lives next to `CreateTenantModal` and the result dialog of `page.tsx` renders it. A restore's result
  has no outcome (unchanged).
- **The old `POST /admin/tenants` stays** for API callers (`api/routers/admin.py`, unchanged); only the UI uses the saga.

## Changed

- `frontend/lib/tenantPlans.ts`: `OutcomeRow`, `provisionOutcome`.
- `frontend/app/admin/tenants/_components/CreateTenantModal.tsx`: `TenantNotice.outcome`, `OutcomeList`.
- `frontend/app/admin/tenants/page.tsx`: the result dialog renders the outcome.
- `frontend/app/admin/tenants/messages.ts`: the outcome copy, English and Vietnamese.
- `frontend/lib/tenantPlans.test.mjs`: the outcome cases (content only, crm + tripplanner with the seat minimum, no
  owner address, CRM refused, job still running).

## Tradeoffs

- The rows reuse the saga's step names, so a new saga step that should show needs a line here; unknown steps are
  ignored rather than shown raw.

## Should know

- No backend change, no migration, no openapi change.
- Live check: Platform › Tenants › New tenant on a synthetic tenant with a CRM add-on and an E2E mailbox address shows
  six "Done" lines; without an address the owner line reads "Not sent (no e-mail)". The CRM lines need a staff session
  in `crm-platform-admin` (the E2E admin is refused, so they read "Needs a look" with the CRM's code).
