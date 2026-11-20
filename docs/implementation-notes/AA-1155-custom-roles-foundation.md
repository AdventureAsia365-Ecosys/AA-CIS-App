# AA-1155 — custom staff roles, CIS migration (S286, lane l4-custom-roles-foundation)

Spec: root `.kiro/specs/AA-1155-custom-staff-roles/` (task T3). Decision record: root ADR 0006a A14 (Q2 option A).
This PR is migration-only (MIG-4); the CIS code (task T6..T9 of the spec) follows the AA-1150 CIS PR.

## Decisions
- One contract file, CIS 462: `shared.staff_role` + `shared.staff_role_capability` (CIS custom roles only),
  `admin_users.custom_role` (FK, RESTRICT) and `admin_users.is_business_owner` (partial unique index: at most one),
  `staff_app_grant_role_chk` widened to Booking custom codes, the A10f / A10g E2E branches of `admin_users_email_chk`,
  and the key-only view `shared.v_staff_booking_grant` granted to `booking` only.
- Custom role codes share one pattern in both apps: `^custom_[a-z][a-z0-9_]{1,46}$`. They can never equal a built-in
  role, and the CIS CHECK on `staff_app_grant` accepts a Booking code without a cross-schema FK (Booking validates it
  against `booking.staff_role`, bk-062).
- `based_on` is the built-in CIS role a holder's `admin_users.role` keeps (NOT NULL column). Never `admin`, `viewer`
  or `reviewer`, and `admin_users_custom_role_chk` refuses a custom holder whose role is one of them: a custom holder
  can never pass an "is admin" test in code that still reads `role`.
- Approve class in the DB too: `staff_role_capability_not_approve_chk` refuses `*.approve`, `*.approve_<x>`,
  `staff.write`, `staff.admin`. The code check (unknown capability = refused) stays the main gate.
- Seed `custom_content` "Content" = `content_lead` (ROLE_PERMS on main, 20/11/2026) minus `content.settings.write`,
  plus `tenant.read` (Q7), 18 capabilities incl. `docs.read`. `content.publish` stays: Q7 says "content_lead minus
  settings", and the two-person publish switch (CIS 458) is the gate for publishing.
- The view lists active rows with a Cognito sub that hold a Booking grant, `is_owner` or `is_business_owner`.

## Changed
- The view stays in `shared` (as the issue says), not `catalog`: catalog is the content read model owned by
  `catalog_owner` with its own recipe; this is a staff-directory contract for one consumer.
- Staff view-as columns (AA-1157) are not in this file: that design is not final.

## Tradeoffs
- `GRANT USAGE ON SCHEMA shared TO booking` is new. No `shared` function is SECURITY DEFINER, tripplanner and
  aa_app_user already hold USAGE, and the probe keeps `shared.admin_users` / `staff_app_grant` / `staff_role` at 42501.
- A role-in-use cannot be deleted (RESTRICT); retire it with `archived`.

## Should know
- Renumber if 462 is taken at merge: `git grep '462_'` and `'462'` here, in the integration test, AA-CIS-Infra
  `db/owners.yaml` / `db/isolation_probe.py` (expected_from 462), root ADR 0006a A14 and the spec.
- DM-7.1 rows for the two tables are in the root PR of this lane (merged before the Infra copy).
