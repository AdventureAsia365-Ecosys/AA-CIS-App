# AA-816 — temporary E2E test-mailbox exception in the staff directory (S243)

> **Removed (root ADR 0006a A10).** Migration 252 put `admin_users_email_chk` back to the 247 rule, the
> `staff_directory` constants and `require_e2e_test_scope` are gone, and the WORKFORCE trigger exception was
> reverted in AA-CIS-Infra. The E2E user is now the ordinary reviewer `e2e-cognito@adventure.asia`, forwarded by
> the adventure.asia mail host to the SES mailbox. See `AA-816-a10-removal.md`. This page is kept as history;
> migration 250 itself is never edited (an applied migration is history; its Infra copy is byte-identical).

Decision (Nghiệp, 19/10/2026, option B): the Cognito email-code E2E test signs in as a staff user whose address is
the SES test mailbox itself, `e2e@e2e-mail.lumiguides.it.com`, instead of a company address forwarded to it
(option A needed a forwarder in the adventure.asia mail host panel). The exception lives until the E2E work is
finished, then it is removed ("dọn dẹp khi xong"). Root ADR 0006a records the exception and its removal.

## Decisions

- **One exact address, no pattern.** `E2E_TEST_STAFF_EMAIL` in `api/services/staff_directory.py` is the only
  non-company address `require_staff_email` accepts; the comparison is on the normalised (trimmed, lower-cased)
  value, so `E2E@E2E-MAIL.lumiguides.it.com` is the same address. Every other off-domain address (another local
  part on the same sub-domain, the apex `lumiguides.it.com`, a look-alike) is still refused with `email_domain`.
- **Reviewer only, no Booking grant, never an owner.** `require_e2e_test_scope` refuses (400 `e2e_test_scope`)
  a create with any other CIS role or with a Booking role, a roles change to another CIS role or to a Booking
  grant, and a sync of a row that somehow drifted (another role or a grant). The DB CHECK says the same:
  migration 250 accepts the address only with `role = 'reviewer'`, `is_owner = false` and `password_hash IS NULL`
  (a Cognito-only row: no legacy password login under this address). The WORKFORCE pre token trigger (Infra,
  same exception) issues a token for it only with exactly the `cis-reviewer` group.
- **Migration 250 is stricter than the task text.** The task said "same rule OR email = the address"; the role,
  owner and password conditions were added to the OR branch so the DB itself enforces the scope (defence in
  depth; the code checks first and answers 400, the CHECK would answer a 500). The constraint keeps its name
  `admin_users_email_chk` (the schema reference and 247's guard know that name).
- **No invitation e-mail.** `staff_invite.is_staff_address` still refuses the address (the invite job would
  skip it anyway). The E2E user is created with `send_invite=false`; it needs no invitation, the test reads the
  sign-in code from the mailbox.
- **No frontend change.** The Staff page keeps its `@adventure.asia` client-side check, so the account cannot be
  created from the UI by accident; it is created once, after the merge, by an ECS exec script that calls
  `staff_directory.create_staff` as the owner (the same code path as the UI, audited as `staff.created`).

## Changed

- `api/services/staff_directory.py`: `E2E_TEST_STAFF_EMAIL`, `E2E_TEST_STAFF_ROLE`, `is_e2e_test_email`,
  `require_e2e_test_scope`; `require_staff_email` accepts the exact address; `create_staff`, `change_roles` and
  `sync_staff` enforce the scope.
- `api/migrations/250_admin_users_e2e_test_email.sql`: replaces `admin_users_email_chk` (one `ALTER TABLE`:
  drop + add), comments the constraint as temporary, guards the result, registers 250.
- E2E: `tests/e2e/helpers/mailbox.ts` `E2E_COGNITO_EMAIL` = the test address (no forwarder); spec comment,
  workflow header and the mailbox-parse fixtures follow.
- Tests: `tests/unit/test_aa816_e2e_test_mail_exception.py`, `tests/integration/test_aa816_migration_250.py`.

## Tradeoffs

- A code constant, not an env var: an env var could widen the exception without a review; the constant is one
  reviewed line, removed with the cleanup PR.
- Keeping the forwarder plan (option A) would have needed no exception, but a manual mail-host change outside
  our control; option B keeps everything in our repos and is reversible by code.

## Should know

- Removal (when the E2E work is done): disable the user in the Staff page or via `disable_staff`, delete the row
  (DRY RUN procedure) and the Cognito user (console, by hand: the writer may not call `AdminDeleteUser`), then a
  new migration restores the 247 CHECK (it RAISEs while a row still uses the address), the constant and the
  scope checks go, the Infra trigger exception goes, and ADR 0006a gets a closing line.
- `tests/integration/test_aa854_admin_users_identity.py` undoes and re-applies 247, which re-creates the CHECK
  without the exception; the 250 test module re-applies 250 itself, so the order of the modules does not matter.
- The helper's recipient filter now matches every message in the mailbox (the SES rule accepts only this one
  recipient). What keeps a stray message out is the baseline snapshot (keys present before the code is requested)
  and the LastModified window; the mailbox-parse fixture for "another recipient" now uses an unrelated address.
- Local test run: unit suite green; integration suite green except `test_007_rls_isolation.py`, which needs its own
  `CIS_TEST_DB_URL` database (aa_cis_admin / aa_app_user logins) and errors the same way without this change.
