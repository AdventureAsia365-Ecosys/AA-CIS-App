# AA-816 — remove the temporary E2E staff exception (root ADR 0006a A10)

Scope: the A10 part of AA-816 only. The S2 part (API legacy flag off) stays open with AA-911.
The work is split into two CIS PRs. PR-A1 (`test/aa-816-e2e-company-address`) only switches the E2E user, so
nothing is deployed. PR-A2 (`fix/aa-816-remove-e2e-exception`) removes the exception: code plus migration 252.
The Infra side (trigger revert, the byte-identical copy of 252, the delete script) is AA-CIS-Infra
`docs/implementation-notes/AA-816-a10-removal.md`.

## Decisions

- **How E2E signs in afterwards.** The 247 CHECK (`^[^@[:space:]]+@adventure[.]asia$`, no sub-domains) means
  the E2E user must have a company address. Its mail reaches the SES mailbox only through a forward-only
  address in the adventure.asia mail host (DreamHost, MX MailChannels):
  `e2e-cognito@adventure.asia` → `e2e@e2e-mail.lumiguides.it.com`. This is the S242/S243 option A.
  - The SES receipt rule matches the envelope recipient, and that stays the mailbox after forwarding.
  - `isAddressedTo` already reads To / Cc / Delivered-To / X-Original-To / X-Forwarded-To / Envelope-To with
    a case-insensitive substring match. A forwarded mail keeps To = the company address, so it matches.
    No change to the parser was needed.
- **The new user is an ordinary reviewer**, created on the Staff page with send_invite on (Nghiệp, H2). The
  invite mail is the second end-to-end check of the forward path. The helper skips it because it has no code
  (the `invite-no-code.eml` fixture).
- **A1 before A2.** A1 is test-only, so the E2E can go green as the new user while the old exception still
  exists. Rollback is then a plain revert. A2 (code and migration) follows only after that green run.
- **A1 also rewrites the `e2e-cognito.yml` header** ("Prerequisites"), because A1 is the PR that switches the
  user.
- **A1 changes the unit test** `test_aa816_e2e_test_mail_exception.py` (the helper half of
  `test_e2e_helper_and_migration_250_name_the_same_address`). It now asserts that the helper names an
  `@adventure.asia` address that `require_staff_email` accepts. Without this change CI goes red. A2 deletes the
  whole file.
- **Migration 252, not an edit of 250.** Any edit to 250 would force a new Infra copy, and 250 is already
  applied. 252 re-asserts the 247 definition with `DROP CONSTRAINT IF EXISTS … ADD CONSTRAINT`, so it is
  idempotent. It RAISEs while any row still has the old address or breaks the 247 rule, so the row has to be
  deleted first (Infra run-script).
- **The guard on the E2E account: detective, in the E2E spec only.** `cognito-login.spec.ts` step 4a asserts
  that the signed-in CIS principal is `reviewer` (the server-set `cis_role` display cookie from the verified
  `/auth/verify-admin` answer) and that the admin-only `/admin/overview` sends it to `/admin/review` (proxy.ts
  checks the verified token role). The existing Booking `no_booking_role` check covers Booking. Any widening of
  this account fails the next run. No special case goes back into production code: a preventive pin (for
  example in the WORKFORCE pre token trigger) would re-create the exception this work removes, so it is left
  as Nghiệp's call (open question).

## Changed

- PR-A1:
  - `tests/e2e/helpers/mailbox.ts`: `E2E_COGNITO_EMAIL = 'e2e-cognito@adventure.asia'`, and the header
    comment and recipient-header comment describe the forwarder path.
  - Fixtures `cognito-qp-b64.eml`, `html-only-b64.eml`, `invite-no-code.eml` are in the forwarded shape
    (To = the company address, Delivered-To / X-Forwarded-To = the mailbox). `html-only` keeps a mixed-case
    display-name To. `other-recipient.eml` is unchanged.
  - `mailbox-parse.spec.ts` has a new case: a message sent straight to the mailbox (the pre-A10 shape) is not
    the E2E user's. A stray code for the old address can never be typed.
  - `cognito-login.spec.ts`: header comment, and the new step 4a detective guard (CIS role must be reviewer).
  - The `e2e-cognito.yml` header and prerequisites (comments only; still a CI file, so it gets a quick
    independent look before merge).
  - The unit test half, as above.
- PR-A2: see the A2 section below (added on that branch).

## Tradeoffs

- **A company address that CI can read.** Its codes land in the CI-readable mailbox, as before. The address is
  public through this public repo, so spam can reach the mailbox through DreamHost. The helper only ever uses a
  new message addressed to that address that yields a code. Any third-party verification mail sent to this
  company address can also be read by CI. Never sign this address up for anything.
- **Two outside dependencies stay.** The adventure.asia mail host (the forwarder) and `lumiguides.it.com` (the
  zone and MX of the mailbox). Losing either one breaks the E2E. It does not grant anyone access: the codes
  are only valid for a reviewer with no Booking access.
- **TLS.** The SES receipt rule requires TLS. If the DreamHost/MailChannels relay does not use TLS, the
  fallback is Infra PR-I0 (`tls_policy = "Optional"`). It weakens the guard, so it needs Nghiệp's OK.

## Should know

- **Hard gate.** Nothing here may be pushed until the forwarder exists and check 1 shows a forwarded mail in
  `inbound/` (H1). If H1 cannot happen, leave the exception live and park backlog row 12 under
  "Waiting on Nghiệp: DreamHost forwarder".
- **Order (one CIS deploy; E2E stays green at every step):**
  1. H1 forwarder (DreamHost) + a test send from `no-reply@lumiguides.it.com` (the Cognito sender) to
     `e2e-cognito@adventure.asia`; the object must appear under `inbound/`.
  2. H2 create the new user on the Staff page (reviewer, Booking none, invite on); the invite is the second
     mailbox proof. No DB Migrate for a few minutes after (invite job 120 s delay, DM-11.4).
  3. A1 merge, then dispatch `e2e-cognito.yml` on main: green as the new user, Booking refusing.
  4. H3 disable the old row (never re-enable or sync it again), H4 delete the old Cognito user.
  5. Infra I1a merge (trigger revert, delete script DRY RUN, aa680 constant, tf comment) + Terraform Apply
     from main; run-script DRY RUN, flip PR, run-script apply. Keep step 4 and 5 close together.
  6. A2 (rebased with `git rebase --onto origin/main 85d3fd8c`) merge + deploy api and worker.
  7. Infra I1b (byte-identical copy of 252) merge, DB Migrate check + apply
     `cis/252_admin_users_e2e_exception_removed.sql`, merge the `bot/db-schema-reference` PR.
  8. E2E again.
- **Local runs (A1):**
  - `npx playwright test --project=cognito tests/e2e/cognito/mailbox-parse.spec.ts`: 7 passed.
  - `pytest tests/unit/test_aa816_e2e_test_mail_exception.py`: 32 passed.
  - `tsc --noEmit --strict` on the three touched .ts files: clean.
