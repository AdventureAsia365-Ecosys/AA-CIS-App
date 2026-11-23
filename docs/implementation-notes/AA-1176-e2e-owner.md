# AA-1176 — Dev-only E2E owner (root ADR 0006a A10h), AA-CIS-App part

Tech lead decision S288: the orchestrating agent gets "an admin like me" for live checks only an owner can run. One new
WORKFORCE address, `e2e-owner@e2e-mail.lumiguides.it.com`, Dev only, groups exactly `cis-admin` + `booking-admin`.

## Decisions
- **Not read-only, own flag.** The address is NOT in `e2e_staff.DEV_ONLY_E2E_STAFF_ROLES` (that map means "read-only
  cap"). The identity carries a new flag `e2e_owner`; `e2e_read_only` stays False and `is_owner` is the row's. Every
  existing A10b exception keys on `e2e_allowlisted_writes` and none of them fires for it.
- **Exact shape or 403.** `resolve_staff` routes a row or token whose e-mail lower-cases to the address into
  `_check_e2e_owner`: outside `ENVIRONMENT == "dev"` → 403 `e2e_not_allowed_here`; otherwise 403 `e2e_owner_scope`
  unless row AND token are both exactly the ASCII address, the token's groups are exactly `{cis-admin, booking-admin}`,
  the resolved role is `admin` (not custom) and the row is `admin` without a custom role. A row/token mix with any
  other address, a look-alike, or a wider/narrower group set is refused, never capped.
- **MFA exemption.** `staff_mfa.is_admin_session` returns False for `e2e_owner`. Reason: the e-mail code lands in the
  mailbox prefix `inbound-admin/`, which only the MFA admin IAM role can read; that read is the second factor.
- **D5 limits (Staff page).** As actor it never changes an owner, the business owner or its own row (roles, disable,
  enable, sign-out, sync, resend invite: 403 `e2e_owner_scope`); never sets or clears the business-owner marker; never
  counts as "another active owner" (`SQL_OTHER_ACTIVE_OWNERS` excludes it), so the last real owner stays protected. It
  may create staff and change non-owner staff (the invite of `info@adventure.asia` as Content is one of the live steps).
  `_require_owner` now returns the actor row (with `email`) and refuses the E2E owner actor outside Dev.
- **Erasure approval.** `e2e_tenant_writes.refuse_any_e2e` (erasure approve / new round: "only a person approves an
  erasure", A10b-5) refuses the E2E owner too (403 `e2e_owner_scope`).
- **Directory scope.** `_E2E_TEST_STAFF_ROLES[owner] = "admin"`, `DEV_ONLY_E2E_BOOKING_ROLES[owner] = "admin"`, and the
  Dev-only environment set is `DEV_ONLY_E2E_ADDRESSES` (read-only set + owner). So the Staff page may create / sync it
  on Dev only, with exactly CIS admin, Booking admin (or none), no CRM role. `is_e2e_test_email` is True for it, so
  `staff_custom_roles._may_hold_marker` refuses it as business owner.
- **Owner marker data path.** No product route sets `is_owner`; the marker comes from the AA-CIS-Infra master run-script
  `db/scripts/aa1176_e2e_owner_row.py` (DB Migrate `mode=run-script`, dry run first). A migration seed was rejected:
  it would put an owner row on every future environment.
- **Test learner / tenant assignment.** `learn.py` marks it `is_test` (refused outside Dev) and the tenant assignment
  route keeps it on synthetic tenants, via the new `e2e_staff.is_dev_only_e2e_address`.
- **Readiness** requires 473: without it the CHECK refuses the row the Staff page would write.

## Changed
- `api/core/e2e_staff.py`: A10h constants, `is_e2e_owner` (ASCII raw value, exact), `is_e2e_owner_like`,
  `is_dev_only_e2e_address`, `DEV_ONLY_E2E_ADDRESSES`, `E2E_OWNER_SCOPE`.
- `api/core/staff_identity.py`: `StaffIdentity.e2e_owner`, rule 5f, `_check_e2e_owner`.
- `api/core/staff_mfa.py`, `api/middleware/staff_bearer.py` (log field `e2e_owner`), `api/core/e2e_tenant_writes.py`.
- `api/services/staff_e2e_owner.py` (new: the D5 limits, kept out of `staff_directory.py`, which the code-health
  file-size baseline does not let grow), `api/services/staff_directory.py` (scope, environment, calls into it),
  `api/services/staff_custom_roles.py` (marker refusal).
- `api/routers/learn.py`, `api/routers/admin_tenant360.py` (Dev-only address helper).
- `services/staff_reconcile.py`: known E2E entry A10h.
- `api/core/readiness.py`: 473.
- Tests: `tests/unit/test_aa1176_e2e_owner.py`; pinned lists in `test_aa1091_e2e_cs_pricing_preset.py`,
  `test_aa816_staff_reconcile.py`. Migration test in `tests/integration/test_aa1176_migration_473.py` (migration PR).
- Spec `tests/e2e/admin-evidence/owner-live.spec.ts` (agent-only, `E2EOWNER=1`), `tests/e2e/helpers/mailbox.ts`.

## Tradeoffs
- The MFA exemption on an owner-level identity is the real widening. It rests on: Dev only in four layers (trigger
  flag + Terraform precondition, CIS, Booking, CRM), the code readable only by the MFA admin role, exact groups.
- D5 is stricter than the tech lead asked ("an admin like me"); it removes the worst outcome (the agent demoting or
  disabling the tech lead) at no cost to the live steps.
- The weekly security report's owner count still counts the E2E owner: `STAFF_SQL` is pinned to read no e-mail column
  (test_aa1003_r4p2_reports), and the reconcile already lists it as a known E2E identity.
- Maker-checker (D6): the account can technically be the checker of a real person's proposal (settings registry,
  Booking price / policy versions). Kept as an operating rule in the ADR (maker yes, checker no); a code guard is a
  follow-up if the tech lead wants one.

## Should know
- Sign in to CIS FIRST: the first sign-in links the Cognito sub, and Booking reads the owner marker only for linked
  rows (`shared.v_staff_booking_grant`).
- Live order: CIS 473 applied → this PR + Booking + Partner deployed → Infra trigger/recipient applied → Cognito user
  (tool) → run-script dry run, flip, run → `owner-live.spec.ts`.
- Removal: a real owner disables the row on Platform › Staff at any time (allowed; D5 never blocks a real owner). This
  is the only immediate removal: Disable also disables the Cognito user and signs it out, and Booking and the CRM trust
  the token's groups, so a database-only change (the Infra run-script's REMOVE mode, which refuses an active row) is
  cleanup after Disable, never instead of it (review finding, S288). Full
  removal with the A10 family (AA-1054): constants, guards, MFA exemption, spec, reconcile entry, the row (DRY RUN) and
  the Cognito user, a new migration restoring the 462 CHECK.
