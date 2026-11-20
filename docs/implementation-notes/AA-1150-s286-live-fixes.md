# AA-1150 — S286 live check F2 / F3: the one-sign-in spec and the E2E account's Booking group

Lane l9 of S286 (hotfix). Evidence: AA-Ecosys `docs/audits/2026-11-20-S286-session/live/evidence.md` (F2, F3).

## Decisions

1. **F2: step 2 reopens the page the sign-in landed on** (the role's `landingPage`), not bare `/admin`: `/admin` has no
   page rule, so `proxy.ts` fails closed to `/login`. The spec also saves hops and the trace from an `afterEach`, so a
   failed run keeps them (the S286 reference copy).
2. **F3 root cause: not the reconcile, not Sync.** The group was removed on 14/11/2026 13:21Z by the A10e-2 revoke step:
   after the AA-1091 synthetic price run the tech lead set the account's Booking role back to None on the Staff page
   (AA-1091 Linear export: "Booking role removed by the tech lead (Staff & access shows Booking role None)"). That is
   `change_roles(booking_role=None)`, which runs AdminRemoveUserFromGroup(booking-contracting) from the CIS task role,
   exactly the CloudTrail event. The reconcile is report only; Sync keeps the group while the row has the grant.
3. **No directory override for E2E identities.** Making the reconcile / Sync treat a "fixed set" as authoritative would
   contradict root ADR 0006a A10e-2: the A10d account has two allowed states, and the ADR has the owner remove the
   preset after each run. The directory already agrees with the defined sets (`require_e2e_test_scope`,
   `KNOWN_E2E_STAFF.other_groups`). The spec now handles both states instead:
   - `EXPECT_BOOKING=role` (default): needs the Pricing team preset held; on the `reason=forbidden` landing it fails at
     once with a message naming the missing preset.
   - `EXPECT_BOOKING=refused`: the resting state; success = Booking `/login?reason=forbidden`, the "refused this
     account" text and no Booking session, reached with no second e-mail prompt.
4. **No data script.** A grant row written outside the Staff page would skip the owner and the audited `roles_changed`
   row the ADR requires; adding the group in Cognito alone is drift that the next Sync removes.

## Changed

- `tests/e2e/admin-evidence/one-sign-in.spec.ts`.
- `tests/unit/test_s286_e2e_cs_booking_grant.py`: the reconcile module and job call no Cognito write; Sync keeps
  booking-contracting while the A10d row holds the grant, removes it at rest, and still strips a real staff member's
  extra group; a Cognito-only contracting is reported as booking_role drift; both A10d states with an agreeing row are
  known exceptions with no drift.

## Tradeoffs

- Permanent contracting for the E2E account would make the one-sign-in proof run without the tech lead, but it widens
  the A10e-2 exposure (pricing writes on synthetic plans at any time). That is a tech lead decision, not a code fix.

## Should know

- A10f / A10g (S286) are not in `KNOWN_E2E_STAFF` or `DEV_ONLY_E2E_STAFF_ROLES` yet; the AA-1155 lane adds them (spec
  task T8). Until then the reconcile does not list them as known E2E exceptions.
