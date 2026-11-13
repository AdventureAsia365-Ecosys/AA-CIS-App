# AA-1086 lane l16: pilot rehearsal prerequisites G1 (CIS part) and G2 (S279)

The pilot rehearsal plan (root `docs/ops/runbooks/pilot-rehearsal.md`) listed gaps that block steps 1, 2, 7-11 (G1:
no rehearsal fixture) and 12b (G2: nothing clears the pilot marker). This lane closes the CIS side of both. The CRM side
of G1 and G9 are in AA-Partner, the E2E user list in AA-CIS-Infra, the runbook status in the root repo.

## Decisions

- **G2 route lives in `api/routers/admin.py`** next to offboard / restore (the tenant lifecycle routes), with the logic
  in `services/tenant_account/pilot.py`. Path `POST /admin/tenants/{tenant_id}/pilot/end`, POLICY `tenant.write`
  (administrator only), re-checked by `_require_tenant_admin` like every AA-797 tenant write. No E2E identity reaches
  it (not on the A10b allowlist).
- **Idempotent by state, not by key:** a tenant that is not the pilot answers 200 `changed: false` and writes nothing
  (no audit row, no event). A second click or a retry after a timeout is harmless.
- **Any account status is accepted**, an offboarded tenant included. Offboarding keeps the marker today, so a pilot
  offboarded first would otherwise hold the Dev slot for ever.
- **CRM follows through the existing feed:** the route writes one `account.status_changed` event whose `status` equals
  `previous_status` (the StatusChange body of `tenant.entitlements.v1`; the shape allows it). The CRM's
  `crm_entitlement_pull` re-reads the snapshot of every tenant the feed names and clears `crm.tenant.is_pilot`
  (crm-044) at its next 2-minute slot instead of the hourly reconcile. No new event type, so no contract version bump
  and no consumer change.
- **Reason bound 500 characters** like the override reasons of Tenant 360 (`REASON_MAX`); this is a field length, not a
  policy threshold.
- **G1 tenant id = the CRM seed's id** for the agency key `pilot-rehearsal-01` (UUIDv5 of the CRM's synthetic
  namespace, as `register-crm-agency` does since AA-1087). The saga's `register_id` (Dev, `demo` only) carries it, so
  CIS, Booking (`bk_tenant_pull`) and the CRM name one tenant without copying ids between repos by hand.
- **`--run` goes into the request key** (`aa1086-pilot-rehearsal-01-<run>`): a failed job can be retried with a new run;
  an existing tenant (by id) is reported with its subscriptions and never created twice; a slug held by another id is
  refused (exit 3).

## Changed

- New: `services/tenant_account/pilot.py`, the route + `EndPilotRequest` / `EndPilotResponse`, POLICY row, audit action
  `tenant.pilot_ended` (staff-only: not in the tenant Activity allowlist, its details hold the staff reason).
- `services/tenant_account/e2e_fixtures.py`: subcommand `rehearsal-tenant --run <run> [--apply]`.
- Tenant 360 › Subscription: "End pilot" button next to the pilot badge (only `is_pilot` and `can(role, "tenant.write")`),
  kit `ConfirmModal` with a required reason; toast on success / no change.
- Snapshots regenerated with the pinned fastapi / pydantic: `docs/api/openapi.json`, `aa882_staff_route_policy.json`,
  `aa1035_role_route_matrix.json`. CONTEXT.md: "Leaving pilot mode" bullet and the fixture sentence.

## Tradeoffs

- **G2a not done here.** The New tenant modal still calls the synchronous `POST /admin/tenants` (no kind, pilot or
  add-ons). Moving it to the async saga needs job polling, three new fields and their own UI evidence; add-ons can
  already be added from Tenant 360 › Subscription after the create, and the pilot is a once-per-environment API call.
  Left as a follow-up.
- No new setting: the route adds no threshold, cap or limit.

## Should know

- The route does not change `kind`: on Dev a non-pilot `agency` is not allowed long term (root ADR 0019 d16), so the
  rehearsal teardown offboards the marker tenant right after ending its pilot.
- `services/jobs/tenant_backup_job.py` (nightly pilot backup) and the retention `NOT_PILOT` filter read `is_pilot`, so
  both stop treating the tenant as the pilot at once.
- Live: `rehearsal-tenant --run <run>` (DRY RUN) through ECS Exec on `aa-cis-dev-api`; the apply is the orchestrator's,
  at the start of the rehearsal and **after** the Infra `e2e-users` run and the AA-Partner seed of
  `pilot-rehearsal-01` (review fix): the CRM's `crm_entitlement_pull` skips feed events for an agency it does not hold
  yet and would project it only at its hourly reconcile.
- Review fix on the CRM side (AA-Partner): ending a pilot clears `is_pilot`, so the CRM now also sets its agency's
  `is_synthetic` false for good when it first sees the marker; the end-pilot route here needs no change (CIS already
  reads a former pilot as real: `is_canary` false). Ending before offboarding stays allowed.
