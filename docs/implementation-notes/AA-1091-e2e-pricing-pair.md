# AA-1091 — A10e-2 pricing pair (CIS side)

Lane l29 of S279; root ADR 0006a A10e-2. The tech lead approved running the AA-1091 live check with SYNTHETIC prices
on Dev with E2E identities (runbook option G1). The maker is the A10d customer-success E2E account after an owner gives
it the Staff page "Pricing team" preset; the checker is a new Dev-only Booking address outside the CIS directory.

## Decisions

- **The A10d account may hold Booking `contracting`, Dev only.** `require_e2e_test_scope` accepts no Booking grant
  (as before) or exactly the one grant `e2e_staff.DEV_ONLY_E2E_BOOKING_ROLES` names for the address, and only where
  `ENVIRONMENT` is exactly `dev` (`e2e_staff.e2e_booking_role_allowed`). Every other E2E address keeps "no Booking
  role"; any other Booking role for the A10d address stays 400 `e2e_test_scope`. The preset itself is unchanged.
- **No CIS migration.** The lane brief asked for a migration relaxing the CIS 378 CHECK. That CHECK constrains
  `shared.admin_users` (address, CIS role, owner flag, password) only; the Booking grant is a `shared.staff_app_grant`
  row, which has no E2E rule and no trigger (schema reference; migrations 244-364). The preset keeps the CIS role
  `customer_success`, which 378 already allows, so nothing in the database refuses the grant: a migration would change
  nothing. No MIG-4 chain.
- **The CIS session stays read-only.** `staff_identity` keys `e2e_read_only` on the address, not on groups, so the
  account with `booking-contracting` keeps no write allow-list in CIS (tests prove it).
- **Reconcile.** `KnownE2E.other_groups` lets the A10d entry also match exactly `cis-customer_success` +
  `booking-contracting`; the pricing checker is a new known exception (clause `ADR 0006a A10e-2`, `booking-contracting`
  only, no directory row).

## Changed

- `api/core/e2e_staff.py`: `E2E_CIS_CS_PRICING_BOOKING_ROLE`, `E2E_BOOKING_PRICING_CHECKER_TEST_STAFF_EMAIL`,
  `DEV_ONLY_E2E_BOOKING_ROLES`, `e2e_booking_role_allowed`, `e2e_booking_rule`.
- `api/services/staff_directory.py`: `require_e2e_test_scope` uses them (net zero lines: the file is at its
  code-health size baseline).
- `services/staff_reconcile.py`: `other_groups` / `allows`, the checker entry.
- Tests: `tests/unit/test_aa1091_e2e_cs_pricing_preset.py`; `test_aa816_staff_reconcile.py` and
  `test_aa1035_a10d_e2e_cs.py` adjusted.

## Tradeoffs

- The rule lives in `api/core/e2e_staff.py` (not the directory) so the directory stays under its size baseline and
  every E2E constant sits in one module.
- Accepting the A10d group set with or without `booking-contracting` (rather than only the pair) keeps the AA-1035
  A10d checks working after the preset is removed at cleanup.

## Should know

- Live order: Infra (trigger + checker + mailbox) applied, then this deploy, then the owner grants the preset on the
  Staff page (`PATCH /admin/platform/staff/{id}/roles {"preset": "pricing_team"}`), then the runbook steps.
- Cleanup: `PATCH …/roles {"booking_role": null}`; removed with the A10 family (AA-1054).
