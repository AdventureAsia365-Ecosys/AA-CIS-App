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
- PR-A2 (`fix/aa-816-remove-e2e-exception`, stacked on A1):
  - `api/services/staff_directory.py`: `E2E_TEST_STAFF_EMAIL`, `E2E_TEST_STAFF_ROLE`, `is_e2e_test_email`,
    `require_e2e_test_scope` and their three call sites (create, roles, sync) are gone, and so is the early
    return in `require_staff_email`. Module and function docstrings say there is no exception.
  - `api/migrations/252_admin_users_e2e_exception_removed.sql`: one BEGIN … COMMIT, `lock_timeout 5s`, no role
    switch. Guard 0 (table + 247 column exist, runner is a member of the table owner), guard A (RAISE with counts
    only while a row has the old address in any case, or any row breaks the 247 rule), then
    `DROP CONSTRAINT IF EXISTS … ADD CONSTRAINT admin_users_email_chk` with the 247 definition, a COMMENT naming
    247 and 252, guard 1 (validated, names `adventure[.]asia`, names neither `lumiguides` nor `reviewer`), and the
    registry row. No `-- migrate:` directive. Rollback = the 250 definition in a new migration.
  - Tests: `tests/integration/test_aa816_migration_252.py` replaces `test_aa816_migration_250.py` (re-applies
    250, inserts the old row, proves 252 RAISEs with counts only and changes nothing, deletes the row, applies
    252 twice; every variant of the old address refused; company addresses keep every role and flag; comment
    set; guard 0 refuses a non-owner; teardown leaves the 247 rule). `tests/unit/test_aa816_e2e_exception_removed.py`
    replaces `test_aa816_e2e_test_mail_exception.py` (names gone, no production module names the old address,
    400 `email_domain`, create refused before any write or Cognito call, a stored old row can still be disabled,
    the helper names a company address, the 252 text).
  - Docs: `CONTEXT.md` (Cognito E2E paragraph and Staff roles), and a "removed / updated" note on top of
    `AA-816-e2e-exception.md` and `AA-816-e2e.md`. Migration 250 is not edited (lesson 92).

- **Not changed:** `enable_staff` and `sync_staff` still do not re-validate a stored e-mail (only create
  validates). After 252 no row can hold an off-domain address, so a re-check there would be dead code. Until the
  old row is deleted, it must not be re-enabled or synced (H3).

## Tradeoffs

- **A company address that CI can read.** Its codes land in the CI-readable mailbox, as before. The address is
  public through this public repo, so spam can reach the mailbox through DreamHost. The helper only ever uses a
  new message addressed to that address that yields a code. Any third-party verification mail sent to this
  company address can also be read by CI. Never sign this address up for anything.
- **Two outside dependencies stay.** The adventure.asia mail host (the forwarder) and `lumiguides.it.com` (the
  zone and MX of the mailbox). Losing either one breaks the E2E. It does not grant anyone access: the codes
  are only valid for a reviewer with no Booking access.
- **TLS.** The SES receipt rule requires TLS. If the DreamHost/MailChannels relay does not use TLS, the
  fallback is a one-line change in Infra I1a (`tls_policy = "Optional"`). It weakens the guard, so it needs Nghiệp's OK.

- **The A10 code guards are gone.** The E2E account is an ordinary reviewer whose codes sit in a CI-readable
  mailbox. Mitigations: the ADR policy line (never give this account another role; remove the forwarder at once
  if `lumiguides.it.com` is lost) and the E2E step 4a detective check. A preventive production-code guard would
  bring the special case back, so it is Nghiệp's call.
- **No RDS snapshot for the row delete.** It is one test row; the `staff.deleted` audit details (username, role,
  created_at) written by the Infra delete script are the restore record.

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
  6. A2 (rebased with `git rebase --onto origin/main <A1 tip>`, see Stacking) merge + deploy api and worker.
  7. Infra I1b (byte-identical copy of 252) merge, DB Migrate check + apply
     `cis/252_admin_users_e2e_exception_removed.sql`, merge the `bot/db-schema-reference` PR.
  8. E2E again.
- **Stacking.** A2 sits on the A1 tip (two A1 commits). On 21/10 both were rebased onto CIS main #888; the A1
  tip is then `71abf34a`. After A1 is squash-merged: `git rebase --onto origin/main <A1 tip>`
  (`git log --oneline origin/main..HEAD` shows the A1 commits under the A2 one), and take the base SHA again after
  every rebase.
- **Migration number.** 252 was free on 21/10/2026 (`git ls-tree origin/main api/migrations` ends at 251; the
  schema reference shows CIS 251). Re-check before the PR: AA-844 / 876 / 911 / 925 / 926 / 927 run in parallel.
- **252 RAISEs while the old row exists** (intended). Apply only after the Infra run-script delete. Any later edit
  of 252 needs a new byte-identical Infra copy at once (lesson 92).
- **Done-when (A10 part) for the Linear issue; AA-816 itself stays In Progress for S2 with AA-911):**
  - The forwarder is live: an S3 object from `no-reply@lumiguides.it.com` to `e2e-cognito@adventure.asia` (H1),
    and the H2 invite object.
  - `admin_users_email_chk` on Dev is the 247 rule again (CIS 252 applied; the schema reference shows it).
  - No `shared.admin_users` row and no WORKFORCE user has `e2e@e2e-mail.lumiguides.it.com`: run-script output +
    the `staff.deleted` audit id, and an empty Cognito list-users result.
  - CIS main has no `E2E_TEST_STAFF_EMAIL` / `require_e2e_test_scope`; the deployed api/worker SHA includes A2.
  - The directory refuses the address with 400 `email_domain`: unit test + the live DB CHECK (a live API call
    needs Nghiệp's owner Bearer; the machine secret gets 403 from staff_policy and the Staff page validates on
    the client).
  - The WORKFORCE triggers have no exception: Infra I1a merged, Terraform Apply done, new Lambda versions behind
    `live`, unit tests deny the address.
  - `e2e-cognito.yml` green on main as `e2e-cognito@adventure.asia` (run id): role reviewer (step 4a), Booking
    refuses with `no_booking_role`.
  - ADR 0006a A10 has its closing line; backlog row 12 is closed.
- **Local runs (A1):**
  - `npx playwright test --project=cognito tests/e2e/cognito/mailbox-parse.spec.ts`: 7 passed.
  - `pytest tests/unit/test_aa816_e2e_test_mail_exception.py`: 32 passed.
  - `tsc --noEmit --strict` on the three touched .ts files: clean.
- **Local runs (A2):**
  - `pytest tests/unit/test_aa816_e2e_exception_removed.py tests/unit/test_aa816_staff_directory_writes.py
    tests/unit/test_aa816_staff_reconcile.py`: 111 passed.
  - `pytest tests/integration/test_aa816_migration_252.py` (local Postgres, non-superuser test user): 18 passed,
    1 skipped (guard 0 non-owner case needs CREATEROLE; CI's superuser runs it and the NOSUPERUSER runner path).
  - flake8 on the touched Python files: clean.
- **Re-run after the rebase onto #888 (21/10):** the A2 unit file (14 passed), staff_directory writes + invite +
  reconcile (134 passed), test_252 (18 passed, 1 skipped as above), `mailbox-parse.spec.ts` (7 passed),
  `playwright test --project=cognito --list` (both specs compile), flake8 on `staff_directory.py` and the
  resource-ID check: clean. 252 is still the next free CIS number (origin/main ends at 251; a parallel lane
  holds 253).
